"""成员路由（PROJECT-PLAN 8.4）。

GET  /members                列成员（可按部门过滤）
POST /members                加入成员（TENANT_ADMIN）
POST /members/{id}/roles     绑定角色（TENANT_ADMIN）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, get_current_user_id, require_roles
from app.core.constants import TENANT_ADMIN_ROLE
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.tenant import AssignRoleRequest, MemberAddRequest, MemberOut
from app.services import member_service

router = APIRouter(prefix="/members", tags=["成员"])


@router.get(
    "",
    response_model=Envelope[list[MemberOut]],
    summary="列成员",
    description=(
        "列出当前租户的成员，含用户名与邮箱（join 全局 users 表）。\n\n"
        "可用 `department_id` 按部门过滤。"
    ),
)
async def list_members(
    department_id: int | None = Query(None, description="按部门过滤"),
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    return ok(await member_service.list_members(session, dept_id=department_id))


@router.post(
    "",
    response_model=Envelope[MemberOut],
    status_code=status.HTTP_201_CREATED,
    summary="加入成员",
    description=(
        "把**已注册**的用户按用户名加入当前租户。\n\n"
        "需要 `TENANT_ADMIN` 角色。\n\n"
        "校验：用户存在且未禁用、不是已有成员、未超过 max_members 配额。"
        "（邀请未注册的人属于邀请流，阶段 5 再做。）"
    ),
)
async def add_member(
    payload: MemberAddRequest,
    session: AsyncSession = Depends(get_db),
    actor_id: int = Depends(get_current_user_id),
    _: dict = Depends(require_roles(TENANT_ADMIN_ROLE)),
) -> dict:
    member = await member_service.add_member(
        session,
        username=payload.username,
        dept_id=payload.dept_id,
        actor_id=actor_id,
    )
    return ok(member, message="已加入")


@router.post(
    "/{member_id}/roles",
    response_model=Envelope[dict],
    status_code=status.HTTP_201_CREATED,
    summary="给成员绑定角色",
    description=(
        "给某个成员绑定一个角色。`member_id` 是 `GET /members` 返回的成员 id。\n\n"
        "需要 `TENANT_ADMIN` 角色。\n\n"
        "**幂等**：重复绑定同一角色返回现有绑定，不会报错。\n\n"
        "成员与角色属于别的租户时返回 404（不是 403），避免存在性泄露。"
    ),
)
async def assign_role(
    member_id: int,
    payload: AssignRoleRequest,
    session: AsyncSession = Depends(get_db),
    actor_id: int = Depends(get_current_user_id),
    _: dict = Depends(require_roles(TENANT_ADMIN_ROLE)),
) -> dict:
    binding = await member_service.assign_role(
        session, member_id=member_id, role_id=payload.role_id, actor_id=actor_id
    )
    return ok(
        {"id": binding.id, "user_id": binding.user_id, "role_id": binding.role_id},
        message="已绑定",
    )
