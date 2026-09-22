"""通知路由（PROJECT-PLAN 8.4）。

    GET  /notifications?unread=true&page=&page_size=   通知列表
    GET  /notifications/unread-count                   未读数（前端角标）
    POST /notifications/{id}/read                      标记单条已读
    POST /notifications/read-all                       全部标记已读

★ 为什么未读数单独一个接口：角标需要「轻量、可高频轮询」。
  复用列表接口的话，前端为了拿一个数字要拉一整页 payload。
  它也是 WebSocket 断线时的兜底——连不上推送时靠轮询这个接口补状态。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, get_current_user_id
from app.api.deps.pagination import pagination
from app.core.db.session import get_db
from app.core.exceptions import NotFoundError
from app.core.responses import Envelope, PageData, ok, paged
from app.schemas.common import PageParams
from app.schemas.notification import (
    NotificationOut,
    NotificationReadResult,
    UnreadCountOut,
)
from app.services import notification_service

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get(
    "",
    response_model=Envelope[PageData[NotificationOut]],
    summary="通知列表",
    description=(
        "当前用户的站内通知，按时间倒序。`unread=true` 只取未读。\n\n"
        "**双重过滤**：租户维度由查询层钩子注入（看不到别家公司），"
        "用户维度显式按 `user_id` 过滤（看不到同租户的同事）。"
        "两者是不同维度的越权，缺一不可——通知 payload 里含任务标题与评论摘要。\n\n"
        "`title` 是服务端按事件类型渲染的；结构见 `realtime/events.py`。"
    ),
)
async def list_notifications(
    unread: bool = Query(False, description="只看未读"),
    page_params: PageParams = Depends(pagination),
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    items, total = await notification_service.list_notifications(
        session,
        user_id=user_id,
        unread_only=unread,
        page=page_params.page,
        page_size=page_params.page_size,
    )
    return paged(items, total=total, page=page_params.page, page_size=page_params.page_size)


@router.get(
    "/unread-count",
    response_model=Envelope[UnreadCountOut],
    summary="未读通知数",
    description="前端角标用。也可作为 WebSocket 断线时的状态兜底。",
)
async def read_unread_count(
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    unread = await notification_service.count_unread(session, user_id=user_id)
    return ok(UnreadCountOut(unread=unread))


@router.post(
    "/read-all",
    response_model=Envelope[NotificationReadResult],
    summary="全部标记已读",
    description="把当前租户内该用户的全部未读标记为已读，返回受影响条数。",
)
async def mark_all_read(
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    updated = await notification_service.mark_all_read(session, user_id=user_id)
    return ok(NotificationReadResult(updated=updated), message="已全部标记为已读")


@router.post(
    "/{notification_id}/read",
    response_model=Envelope[NotificationReadResult],
    status_code=http_status.HTTP_200_OK,
    summary="标记单条已读",
    description=(
        "**幂等**：已读的再标一次返回 `updated=0`，不是错误。\n\n"
        "不属于当前用户 / 不存在 / 跨租户的通知统一返回 404——"
        "区分它们等于告知「这个 id 存在但归别人」，造成存在性泄露。"
    ),
)
async def mark_one_read(
    notification_id: int,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    result = await notification_service.mark_read(
        session, user_id=user_id, notification_id=notification_id
    )
    if not result.found:
        # 不存在 / 不属于该用户 / 跨租户 —— 统一 404，不区分，
        # 否则等于告知「这个 id 存在但归别人」，造成存在性泄露。
        raise NotFoundError("通知不存在或无权访问")

    if result.updated == 0:
        return ok(NotificationReadResult(updated=0), message="该通知已是已读状态")

    return ok(NotificationReadResult(updated=result.updated), message="已标记为已读")
