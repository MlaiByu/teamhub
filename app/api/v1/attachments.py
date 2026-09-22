"""附件路由（PROJECT-PLAN 8.4）。

    POST /attachments?biz_type=&biz_id=           上传（multipart）
    GET  /attachments?biz_type=&biz_id=           列表
    GET  /attachments/{id}/download               下载

★ 下载为什么是接口而不是直接给 URL：
  `attachments.path` 是存储后端内部路径，**不回给客户端**（见 schema 注释）。
  客户端拿 id 走下载接口，后端才去读字节并流式返回——这样换存储后端、
  迁移位置都不破坏已有链接，也能在下载这一层做租户/数据范围的再次校验。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, get_current_user_id
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.attachment import AttachmentOut
from app.services import attachment_service

router = APIRouter(prefix="/attachments", tags=["附件"])


@router.post(
    "",
    response_model=Envelope[AttachmentOut],
    status_code=201,
    summary="上传附件",
    description=(
        "上传附件并挂到指定实体（`biz_type` + `biz_id`，如 `task` + 任务 id）。\n\n"
        "**归属校验**：挂靠的实体必须对当前租户可见（走实体自己的守卫查询），\n"
        "跨租户 / 不存在返回 404。\n\n"
        "**两道安全阀**（都在字节落盘之前）：\n"
        "- 大小上限（默认 10 MB），超限 409\n"
        "- 扩展名白名单，只允许图片/文档/压缩包，防止把 .html/.svg 这种\n"
        "  可执行内容当作附件存下（下载回浏览器时成为 XSS 载体）"
    ),
)
async def upload_attachment(
    file: UploadFile = File(..., description="文件内容"),
    biz_type: str = Form(..., description="归属类型：task / project"),
    biz_id: int = Form(..., description="归属实体 id"),
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    attachment = await attachment_service.upload(
        session, file=file, biz_type=biz_type, biz_id=biz_id, owner_id=user_id
    )
    return ok(AttachmentOut.model_validate(attachment), message="已上传")


@router.get(
    "",
    response_model=Envelope[list[AttachmentOut]],
    summary="附件列表",
    description="列出当前租户内的附件。可按归属过滤（`biz_type` + `biz_id`）。",
)
async def list_attachments(
    biz_type: str | None = Query(None, description="按归属类型过滤"),
    biz_id: int | None = Query(None, description="按归属实体过滤"),
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    items = await attachment_service.list_attachments(session, biz_type=biz_type, biz_id=biz_id)
    return ok([AttachmentOut.model_validate(a) for a in items])


@router.get(
    "/{attachment_id}/download",
    summary="下载附件",
    description=(
        "按 id 下载附件。跨租户 / 超数据范围返回 404。\n\n"
        "下载文件名由服务端生成（`attachment-<id>.<ext>`），不用原始文件名——\n"
        "原始文件名是用户输入，可能被用于 Content-Disposition 头注入。"
    ),
)
async def download_attachment(
    attachment_id: int,
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
):
    attachment, data = await attachment_service.read_attachment(session, attachment_id)
    filename = attachment_service.download_filename(attachment)

    from io import BytesIO

    return StreamingResponse(
        BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
