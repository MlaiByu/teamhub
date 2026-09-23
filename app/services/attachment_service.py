"""附件服务。

★ 本模块最该注意的一条：**归属校验**。

  附件的 `biz_type` + `biz_id` 指向它挂靠的实体（任务 / 项目）。如果不校验
  那个实体对当前租户可见，任何成员都能「凭空」给不存在的、或别的租户的实体
  挂附件——制造出谁都访问不到、或串到别人名下的孤立附件。

  做法与项目里其他跨实体引用一致：走该实体**自己的守卫查询**
  （`TaskRepository.get_or_404` / `ProjectRepository.get_or_404`），
  跨租户 / 超出数据范围自然返回 404。这里**不**需要像 `assignee_id` 那样
  额外写同租户校验，因为任务与项目都是租户级表，守卫已覆盖。

★ 上传的两道安全阀（都在字节落盘之前）：
  1. 大小上限：超 `max_upload_bytes` 直接 413，不落盘（别先写再删）
  2. 扩展名白名单：只允许 settings.allowed_upload_exts。附件会被下载回给
     浏览器，若允许 .html/.svg 就等于提供了可执行内容的存放点（XSS 载体）。
"""

from __future__ import annotations

from typing import BinaryIO

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import AttachmentBizType
from app.core.db.context import current_tenant_id
from app.core.exceptions import ConflictError, NotFoundError, ParamInvalidError
from app.core.logging import get_logger
from app.core.storage import LocalStorage, safe_ext, storage
from app.models import Attachment
from app.repositories.attachment import AttachmentRepository
from app.repositories.project import ProjectRepository
from app.repositories.task import TaskRepository
from app.services import audit_service

logger = get_logger(__name__)


async def _read_limited(file: BinaryIO, *, limit: int) -> bytes:
    """读上传内容，超限即抛。先校验大小再落盘，避免「写进去再删」的浪费。

    ★ `UploadFile.file` 是 `SpooledTemporaryFile`，它的 `read()` 是**同步**的，
      不是 async——所以这里不能 `await file.read(...)`。用 `anyio.to_thread`
      把同步读挪到线程池，避免阻塞事件循环；读的量很小（上限 10MB），
      开销可忽略。
    """
    import anyio

    data = await anyio.to_thread.run_sync(file.read, limit + 1)
    if len(data) > limit:
        raise ConflictError(f"附件不能超过 {limit // (1024 * 1024)} MB")
    return data


def _validate_ext(filename: str) -> str:
    ext = safe_ext(filename)
    if ext not in settings.allowed_upload_exts:
        raise ParamInvalidError(
            f"不允许的附件类型：.{ext or '(无扩展名)'}；"
            f"允许：{', '.join(sorted(settings.allowed_upload_exts))}"
        )
    return ext


async def _ensure_biz_visible(session: AsyncSession, *, biz_type: str, biz_id: int) -> None:
    """确认附件要挂靠的实体对当前租户可见。

    ★ 归属校验必须走实体自己的守卫查询——这是「评论可见性继承任务」
      同一思路的复用：附件可见性也继承它挂靠的实体。
    """
    try:
        bt = AttachmentBizType(biz_type)
    except ValueError as exc:
        raise ParamInvalidError(f"不支持的归属类型：{biz_type}") from exc

    if bt is AttachmentBizType.TASK:
        await TaskRepository(session).get_or_404(biz_id)
    elif bt is AttachmentBizType.PROJECT:
        await ProjectRepository(session).get_or_404(biz_id)


async def upload(
    session: AsyncSession,
    *,
    file: UploadFile,
    biz_type: str,
    biz_id: int,
    owner_id: int,
    backend: LocalStorage | None = None,
) -> Attachment:
    """上传附件。先校验归属、再校验大小与类型，最后落盘 + 落库。"""
    await _ensure_biz_visible(session, biz_type=biz_type, biz_id=biz_id)

    filename = file.filename or ""
    ext = _validate_ext(filename)

    data = await _read_limited(file.file, limit=settings.max_upload_bytes)

    active_backend = backend or storage
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise NotFoundError("当前请求没有租户上下文")

    # 先落盘再落库：落库前必须确保字节已持久化，否则库里会有指向不存在文件的记录。
    # 若落库失败，回删文件——不留孤儿文件。
    rel_path = await active_backend.save(tenant_id=tenant_id, data=data, ext=ext)
    try:
        attachment = AttachmentRepository(session).create(
            owner_id=owner_id,
            biz_type=biz_type,
            biz_id=biz_id,
            path=rel_path,
            size=len(data),
        )
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        await active_backend.delete(rel_path)
        raise

    logger.info(
        "attachment_uploaded",
        attachment_id=attachment.id,
        biz_type=biz_type,
        biz_id=biz_id,
        size=len(data),
    )
    await audit_service.record(
        session,
        action="attachment.upload",
        entity_type="attachment",
        entity_id=attachment.id,
        detail={"biz_type": biz_type, "biz_id": biz_id, "size": len(data)},
    )
    return attachment


async def _visible_biz_ids(session: AsyncSession) -> tuple[set[int], set[int]]:
    """算出当前身份**可见**的项目 id 与任务 id 集合。

    ★ 为什么用 `list_all()` 而不是直接 `select(Project.id)`：
      `select(Project.id)` 只选列、不选实体，`with_loader_criteria` 的数据范围
      过滤**不一定**会注入；而 `list_all()` 是实体查询，租户 + 数据范围两层
      过滤必然生效。宁可多取几列，也不要一个「看着像过滤了、其实没过滤」的查询
      ——那正是这个漏洞的成因。
    """
    project_ids = {p.id for p in await ProjectRepository(session).list_all()}
    task_ids = {t.id for t in await TaskRepository(session).list_all()}
    return project_ids, task_ids


async def list_attachments(
    session: AsyncSession, *, biz_type: str | None, biz_id: int | None
) -> list[Attachment]:
    """列出附件。**可见性继承挂靠实体**。

    ★ 这是修复越权的核心。附件表不带 `DataScopedMixin`，所以「只查附件表」
      会绕过数据范围——SELF 范围的成员能看到同租户别人任务的附件。两条路径：

      1. 同时给了 biz_type + biz_id：先校验该实体可见（跨范围 → 404），
         可见才去查它的附件。
      2. 没给全：先算出「可见实体 id 集合」，再让仓储按集合过滤。
         这条最容易漏——带参数时会想着校验，不带参数反而直接全表扫。
    """
    repository = AttachmentRepository(session)

    if biz_type is not None and biz_id is not None:
        await _ensure_biz_visible(session, biz_type=biz_type, biz_id=biz_id)
        return list(await repository.list_for_biz(biz_type=biz_type, biz_id=biz_id))

    project_ids, task_ids = await _visible_biz_ids(session)
    items = await repository.list_for_scope(project_ids=project_ids, task_ids=task_ids)
    if biz_type is not None:
        # 只给了类型没给 id：在可见集合的基础上再按类型筛一次
        items = [a for a in items if a.biz_type == biz_type]
    return list(items)


async def get_attachment(session: AsyncSession, attachment_id: int) -> Attachment:
    """取附件元数据。跨租户返回 404。

    ★ 注意这里**只保证租户隔离**。数据范围校验在 `read_attachment` 里做
      （需要它先查出 biz 才能校验），所以本函数不对外暴露为接口。
    """
    return await AttachmentRepository(session).get_or_404(attachment_id)


async def read_attachment(
    session: AsyncSession, attachment_id: int, *, backend: LocalStorage | None = None
) -> tuple[Attachment, bytes]:
    """取附件元数据 + 字节。下载接口用。

    ★ 下载必须校验数据范围：知道附件 id 不等于有权下载。
      校验方式是把它当「挂靠实体的一个子资源」——先确认挂靠实体可见。
    """
    attachment = await get_attachment(session, attachment_id)

    # ★ 关键一步：附件可见性继承挂靠实体。少了它，同租户任何成员
    #   只要猜到 attachment_id 就能下载别人的附件。
    await _ensure_biz_visible(session, biz_type=attachment.biz_type, biz_id=attachment.biz_id)

    active_backend = backend or storage
    try:
        data = await active_backend.open(attachment.path)
    except FileNotFoundError as exc:
        # 元数据在、文件没了——这是「库与磁盘不一致」，对客户端而言就是 404
        logger.error("attachment_file_missing", attachment_id=attachment_id, path=attachment.path)
        raise NotFoundError("附件文件不存在") from exc
    return attachment, data


def download_filename(attachment: Attachment) -> str:
    """生成安全的下载文件名。

    ★ 不用原始文件名（它是用户输入，可能含引号/换行，注入 Content-Disposition）。
      用 `attachment-<id>.<ext>` 这种服务端生成的名字，杜绝头注入。
    """
    ext = safe_ext(attachment.path)
    suffix = f".{ext}" if ext else ""
    return f"attachment-{attachment.id}{suffix}"


__all__ = ["upload", "list_attachments", "get_attachment", "read_attachment", "download_filename"]
