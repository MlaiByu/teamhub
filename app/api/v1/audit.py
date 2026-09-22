"""审计查询路由（PROJECT-PLAN 8.4）。

    GET /api/v1/audit-logs?entity_type=&entity_id=&action=&user_id=&page=

★ 只对 TENANT_ADMIN 开放（PROJECT-PLAN 562：`GET /audit-logs  ADMIN`）。
  审计日志含操作细节（谁在何时对什么做了什么），普通成员没有理由查看；
  放开等于把「谁改了任务状态」这类信息泄露给全公司。

★ 租户隔离由查询层钩子天然保证——`AuditLog` 是租户级表，
  管理员也只能看到**自己租户**的审计，看不到别的公司。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import require_roles
from app.api.deps.pagination import pagination
from app.core.constants import TENANT_ADMIN_ROLE
from app.core.db.session import get_db
from app.core.responses import Envelope, PageData, paged
from app.repositories.audit import AuditLogRepository
from app.schemas.audit import AuditLogOut
from app.schemas.common import PageParams

router = APIRouter(prefix="/audit-logs", tags=["审计"])


@router.get(
    "",
    response_model=Envelope[PageData[AuditLogOut]],
    summary="审计日志列表",
    description=(
        "按时间倒序列出当前租户的操作审计日志。**仅 TENANT_ADMIN 可访问**。\n\n"
        "过滤维度：`entity_type`（实体类型）、`entity_id`、`action`、`user_id`，可组合。\n\n"
        "租户隔离由查询层钩子自动注入——管理员也只能看到自己租户的日志。"
    ),
)
async def list_audit_logs(
    entity_type: str | None = Query(
        None, description="实体类型：task / project / member / role / comment / attachment"
    ),
    entity_id: int | None = Query(None, description="实体 id"),
    action: str | None = Query(None, description="动作：如 task.status_changed"),
    user_id: int | None = Query(None, description="操作者 user_id"),
    page_params: PageParams = Depends(pagination),
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(TENANT_ADMIN_ROLE)),
) -> dict:
    items, total = await AuditLogRepository(session).paginate_filtered(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        user_id=user_id,
        page=page_params.page,
        page_size=page_params.page_size,
    )
    return paged(
        [AuditLogOut.model_validate(log) for log in items],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )
