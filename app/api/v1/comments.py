"""评论路由（PROJECT-PLAN 8.4）。

    GET  /tasks/{task_id}/comments   评论列表（时间正序）
    POST /tasks/{task_id}/comments   发表评论（含 @提及解析与通知触发）

放在 `/tasks` 前缀下而不是独立的 `/comments`：评论只存在于任务语境中，
`/tasks/{id}/comments` 比 `/comments?task_id=` 更准确地表达了层级关系，
也让「先校验任务可见性」这一步在路径上就看得见。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, get_current_user_id
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.comment import CommentCreateRequest, CommentOut
from app.services import comment_service

router = APIRouter(prefix="/tasks", tags=["评论"])


@router.get(
    "/{task_id}/comments",
    response_model=Envelope[list[CommentOut]],
    summary="评论列表",
    description=(
        "按时间正序列出任务的评论。\n\n"
        "**可见性继承任务**：接口先校验任务本身是否可见（租户 + 数据范围），"
        "再取评论。所以 SELF 范围的成员读不到别人任务的讨论，"
        "而不是「能读评论但读不到任务」这种错位状态。"
    ),
)
async def list_comments(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    comments = await comment_service.list_comments(session, task_id=task_id)
    return ok([CommentOut.model_validate(c) for c in comments])


@router.post(
    "/{task_id}/comments",
    response_model=Envelope[CommentOut],
    status_code=http_status.HTTP_201_CREATED,
    summary="发表评论",
    description=(
        "发表评论并按规则触发通知。\n\n"
        "**@提及**：正文里写 `@用户名` 即可提及成员。解析在服务端完成，"
        "且**只认当前租户的 ACTIVE 成员**——不接受客户端传「要通知谁」，"
        "否则任何成员都能给全公司发任意通知。\n\n"
        "通知规则：\n"
        "- 被 @ 的人收到「提及」通知\n"
        "- 任务执行人收到「新评论」通知，但**若他同时被 @ 则只收提及那一条**\n"
        "- 评论作者自己不会收到通知\n\n"
        "`@` 一个不存在或非本团队的成员不会报错，静默忽略。"
    ),
)
async def create_comment(
    task_id: int,
    payload: CommentCreateRequest,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    comment = await comment_service.create_comment(
        session, task_id=task_id, content=payload.content, author_id=user_id
    )
    return ok(CommentOut.model_validate(comment), message="已发表")
