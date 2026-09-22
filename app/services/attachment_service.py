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


async def list_attachments(
    session: AsyncSession, *, biz_type: str | None, biz_id: int | None
) -> list[Attachment]:
    return list(await AttachmentRepository(session).list_for_biz(biz_type=biz_type, biz_id=biz_id))


async def get_attachment(session: AsyncSession, attachment_id: int) -> Attachment:
    """取附件元数据。跨租户 / 超数据范围返回 404。"""
    return await AttachmentRepository(session).get_or_404(attachment_id)


async def read_attachment(
    session: AsyncSession, attachment_id: int, *, backend: LocalStorage | None = None
) -> tuple[Attachment, bytes]:
    """取附件元数据 + 字节。下载接口用。"""
    attachment = await get_attachment(session, attachment_id)
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
