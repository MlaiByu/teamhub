"""RBAC 仓储。

`Permission` 是**全局字典**（不带 tenant_id）→ `BaseRepository`
`Role` / `RolePermission` / `UserRole` 都是租户级 → `TenantAwareRepository`

★ 这个分流不是形式主义：权限点（`project:create` 之类）由平台统一维护，
  租户只能组合不能新增（PROJECT-PLAN 7.3-2）。所以查权限点不该受租户过滤，
  查角色绑定则必须受。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import Permission, Role, RolePermission, UserRole
from app.repositories.base import BaseRepository, TenantAwareRepository


class PermissionRepository(BaseRepository[Permission]):
    """全局权限字典。"""

    model = Permission

    async def get_by_code(self, code: str) -> Permission | None:
        return await self.find_one_by(code=code)

    async def get_by_codes(self, codes: Sequence[str]) -> Sequence[Permission]:
        stmt = self.base_select().where(Permission.code.in_(codes))
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()


class RoleRepository(TenantAwareRepository[Role]):
    model = Role

    async def get_by_code(self, code: str) -> Role | None:
        stmt = self.base_select().where(Role.code == code)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def list_by_ids(self, role_ids: Sequence[int]) -> Sequence[Role]:
        """批量取角色。

        ★ 刻意不写成 `for rid in role_ids: await repo.get(rid)`——
          那是 N+1 查询。授权计算在每次登录/刷新/鉴权路径上都会跑，
          循环查库会把这条热路径拖成 O(角色数) 次往返。
        """
        if not role_ids:
            return []
        stmt = self.base_select().where(Role.id.in_(role_ids))
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()


class UserRoleRepository(TenantAwareRepository[UserRole]):
    """用户-角色绑定。授权计算的数据来源。"""

    model = UserRole

    async def list_role_ids_for_user(self, user_id: int) -> Sequence[int]:
        stmt = self.base_select().where(UserRole.user_id == user_id)
        result = await self.session.execute(stmt)
        return [row.role_id for row in result.scalars().unique().all()]

    async def bind(self, *, user_id: int, role_id: int) -> UserRole:
        """绑定角色。重复绑定会撞 UNIQUE(tenant_id, user_id, role_id)。"""
        return self.create(user_id=user_id, role_id=role_id)


class RolePermissionRepository(TenantAwareRepository[RolePermission]):
    model = RolePermission

    async def list_permission_ids_for_roles(self, role_ids: Sequence[int]) -> Sequence[int]:
        if not role_ids:
            return []
        stmt = self.base_select().where(RolePermission.role_id.in_(role_ids))
        result = await self.session.execute(stmt)
        return [row.permission_id for row in result.scalars().unique().all()]
