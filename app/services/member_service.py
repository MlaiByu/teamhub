"""成员服务：列成员 / 加成员 / 绑角色。

成员是「租户内的人」。所有操作都要求已设租户上下文（由路由依赖保证）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MemberStatus
from app.core.db.context import current_tenant_id
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import UserRole
from app.repositories.org import DepartmentRepository
from app.repositories.rbac import RoleRepository, UserRoleRepository
from app.repositories.tenant import TenantMemberRepository, TenantRepository
from app.repositories.user import UserRepository
from app.schemas.tenant import MemberOut

logger = get_logger(__name__)


def _to_out(member, user) -> MemberOut:
    """把 TenantMember + User 组装成对外的 MemberOut（两表字段，手工挑选）。"""
    return MemberOut(
        id=member.id,
        user_id=member.user_id,
        username=user.username,
        email=user.email,
        status=member.status,
        dept_id=member.dept_id,
        created_at=member.created_at,
    )


async def list_members(session: AsyncSession, *, dept_id: int | None = None) -> list[MemberOut]:
    """列出当前租户的成员（可按部门过滤）。"""
    rows = await TenantMemberRepository(session).list_with_users(dept_id=dept_id)
    return [_to_out(member, user) for member, user in rows]


async def add_member(session: AsyncSession, *, username: str, dept_id: int | None) -> MemberOut:
    """把已注册的用户加入当前租户。

    校验顺序故意从「便宜」到「贵」：先查部门、再查用户、最后才算配额。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise NotFoundError("当前请求没有租户上下文")

    member_repo = TenantMemberRepository(session)

    if dept_id is not None and await DepartmentRepository(session).get(dept_id) is None:
        # 走守卫 + 钩子过滤：别的租户的部门 ID 会返回 None → 404
        raise NotFoundError("部门不存在或无权访问")

    user = await UserRepository(session).get_by_username(username)
    if user is None:
        raise NotFoundError("用户不存在")
    if not user.is_active:
        raise ConflictError("该账号已被禁用，无法加入")

    if await member_repo.get_membership(user.id) is not None:
        raise ConflictError("该用户已是本团队（或曾加入后被移除）的成员")

    # 配额校验：到上限就拒，给出当前上限便于管理员扩容
    tenant = await TenantRepository(session).get(tenant_id)
    if tenant is not None:
        count = await member_repo.count_members()
        if count >= tenant.max_members:
            raise ConflictError(f"成员数已达上限（{tenant.max_members} 人），请先扩容")

    member = member_repo.create(
        user_id=user.id,
        status=str(MemberStatus.ACTIVE),
        dept_id=dept_id,
    )
    await session.flush()
    await session.commit()

    logger.info("member_added", user_id=user.id, tenant_id=tenant_id)
    return _to_out(member, user)


async def assign_role(session: AsyncSession, *, member_id: int, role_id: int) -> UserRole:
    """给成员绑定角色（当前租户内）。

    ★ 两个 id 都走守卫：member_id 与 role_id 属于别的租户时，get() 都会
      返回 None → 404。所以「把 A 租户的角色绑给 B 租户的成员」这类
      跨租户绑定在这里被自然拦下。
    """
    member = await TenantMemberRepository(session).get(member_id)
    if member is None:
        raise NotFoundError("成员不存在或无权访问")

    role = await RoleRepository(session).get(role_id)
    if role is None:
        raise NotFoundError("角色不存在或无权访问")

    repo = UserRoleRepository(session)
    existing = await repo.find_binding(user_id=member.user_id, role_id=role_id)
    if existing is not None:
        # 幂等：重复绑定同一角色返回现有绑定，而不是撞 UNIQUE 约束报错。
        # 对管理员来说「已经绑过了」是正常状态，不是错误。
        return existing

    binding = await repo.bind(user_id=member.user_id, role_id=role_id)
    await session.commit()

    logger.info("role_assigned", user_id=member.user_id, role_id=role_id, member_id=member_id)
    return binding
