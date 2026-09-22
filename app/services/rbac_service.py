"""RBAC 授权计算。

★ 本模块负责回答两个问题（PROJECT-PLAN 5.1）：

    1. 这个用户在**当前租户**里有哪些角色码？（决定「能不能操作」）
    2. 这些角色里最宽的数据范围是什么？（决定「能看到哪些数据」）

  两个答案都会写进 access token 的声明，供中间件在后续请求里直接使用。
  这样「鉴权时判断的权限」与「查询时执行的过滤」同源，不会错位。

★ 所有方法都要求**调用前已设好租户上下文**——
  Role / RolePermission / UserRole 都是租户级表，受 do_orm_execute 钩子过滤。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    DEPT_MANAGER_ROLE,
    MEMBER_ROLE,
    TENANT_ADMIN_ROLE,
    DataScope,
    widest_scope,
)
from app.core.db.context import current_tenant_id
from app.core.exceptions import TenantContextMissingError
from app.core.logging import get_logger
from app.repositories.rbac import RoleRepository, UserRoleRepository

logger = get_logger(__name__)

# 新租户开通时自动创建的角色 → 数据范围。
# ★ 数据范围绑在角色上（5.1），用户多角色取最宽（widest_scope 集中计算）。
DEFAULT_ROLE_SCOPES: dict[str, DataScope] = {
    TENANT_ADMIN_ROLE: DataScope.ALL,
    DEPT_MANAGER_ROLE: DataScope.DEPT,
    MEMBER_ROLE: DataScope.SELF,
}

DEFAULT_ROLE_NAMES: dict[str, str] = {
    TENANT_ADMIN_ROLE: "租户管理员",
    DEPT_MANAGER_ROLE: "部门主管",
    MEMBER_ROLE: "普通成员",
}


@dataclass(frozen=True)
class UserAuthorities:
    """一个用户在当前租户内的授权快照。"""

    roles: list[str] = field(default_factory=list)
    data_scope: DataScope = DataScope.SELF

    def is_empty(self) -> bool:
        return not self.roles


async def get_user_authorities(session: AsyncSession, user_id: int) -> UserAuthorities:
    """算出用户在**当前租户**内的角色码与最宽数据范围。

    ★ 两条查询，不是 N+1：先取 role_id 列表，再批量取角色。
      这条路径在每次登录 / 刷新 / 需要重新鉴权时都会跑，必须保持两次往返。

    ★ 没有任何角色时返回最小权限（SELF + 空角色列表），而不是放行。
      「默认宽松」在多租户系统里是最危险的默认值。
    """
    role_ids = await UserRoleRepository(session).list_role_ids_for_user(user_id)
    if not role_ids:
        return UserAuthorities(roles=[], data_scope=DataScope.SELF)

    roles = await RoleRepository(session).list_by_ids(role_ids)
    if not roles:
        # role_id 指向了不存在的角色（数据不一致）。按最小权限处理并留下线索。
        logger.warning("orphan_user_role_binding", user_id=user_id, role_ids=list(role_ids))
        return UserAuthorities(roles=[], data_scope=DataScope.SELF)

    scopes = [DataScope(r.data_scope) for r in roles]
    return UserAuthorities(
        roles=sorted(r.code for r in roles),
        # 取最宽收敛在 core/constants.widest_scope 一处算——
        # 散在各 service 里各算一遍，早晚有一处算错（PLAN 十四）
        data_scope=widest_scope(scopes),
    )


async def provision_default_roles(session: AsyncSession, *, tenant_id: int) -> dict[str, int]:
    """为新租户创建默认角色，返回 {角色码: role_id}。

    ★ 幂等：已存在的角色码会复用，不重复创建。
      这样「注册建租户」与「平台管理员补建租户」两条路径可以共用它。

    ★ `tenant_id` 不参与写入（`create()` 从上下文注入），它的作用是
      **自检**：传入的租户与当前上下文不一致时立刻报错。否则一旦调用点
      忘了设上下文、或者设成了别的租户，角色会被静默创建到错误的租户下——
      这类错误在测试环境看不出来，只有线上才会发现权限错乱。
    """
    current = current_tenant_id.get()
    if current is None:
        raise TenantContextMissingError(
            "provision_default_roles 需要租户上下文：Role 是租户级表，"
            "缺少上下文会被 repository 守卫拒绝。"
        )
    if current != tenant_id:
        raise TenantContextMissingError(
            f"租户上下文不一致：入参 tenant_id={tenant_id}，当前上下文={current}"
        )

    repo = RoleRepository(session)
    created: dict[str, int] = {}

    for code, scope in DEFAULT_ROLE_SCOPES.items():
        existing = await repo.get_by_code(code)
        if existing is not None:
            created[code] = existing.id
            continue
        role = repo.create(
            code=code,
            name=DEFAULT_ROLE_NAMES[code],
            data_scope=str(scope),
            # tenant_id 由 repository.create() 从上下文注入（坑 2）
        )
        await session.flush()
        created[code] = role.id

    return created


async def bind_role(session: AsyncSession, *, user_id: int, role_id: int) -> None:
    """绑定角色到用户（当前租户内）。"""
    await UserRoleRepository(session).bind(user_id=user_id, role_id=role_id)
    await session.flush()
