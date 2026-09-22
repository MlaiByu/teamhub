"""角色路由（PROJECT-PLAN 8.4）。

    GET  /roles   列角色（需登录，用于分配角色的界面）
    POST /roles   创建角色（TENANT_ADMIN，data_scope 在此设置）

权限点（Permission）由平台维护为全局字典，不提供创建接口——
租户只能组合权限点，不能新增（PROJECT-PLAN 7.3-2）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, require_roles
from app.core.constants import TENANT_ADMIN_ROLE
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.rbac import RoleCreateRequest, RoleOut
from app.services import rbac_service

router = APIRouter(prefix="/roles", tags=["RBAC"])


@router.get(
    "",
    response_model=Envelope[list[RoleOut]],
    summary="列角色",
    description="列出当前租户的全部角色。分配角色、展示权限时需要。",
)
async def list_roles(
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    return ok([RoleOut.model_validate(r) for r in await rbac_service.list_roles(session)])


@router.post(
    "",
    response_model=Envelope[RoleOut],
    status_code=status.HTTP_201_CREATED,
    summary="创建角色",
    description=(
        "在当前租户下创建角色，`data_scope` 决定其成员能看到多大范围的数据"
        "（SELF 只看自己的 / DEPT 看本部门 / ALL 看全租户）。\n\n"
        "需要 `TENANT_ADMIN` 角色。\n\n"
        "同一租户内角色编码必须唯一（唯一约束含 tenant_id——"
        "两个租户可以用同一个编码，互不冲突）。"
    ),
)
async def create_role(
    payload: RoleCreateRequest,
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(TENANT_ADMIN_ROLE)),
) -> dict:
    role = await rbac_service.create_role(
        session,
        code=payload.code,
        name=payload.name,
        data_scope=str(payload.data_scope),
    )
    return ok(RoleOut.model_validate(role), message="已创建")
