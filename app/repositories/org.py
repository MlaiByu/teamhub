"""组织（部门）仓储。

`Department` 是租户级表（继承 TenantScopedMixin）→ `TenantAwareRepository`。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import Department
from app.repositories.base import TenantAwareRepository


class DepartmentRepository(TenantAwareRepository[Department]):
    model = Department

    async def list_ordered(self) -> Sequence[Department]:
        """按物化路径排序返回全部部门。

        ★ path 排序的妙用：`/1/` < `/1/5/` < `/1/5/12/`——按字符串排序后
          **父节点一定排在子节点之前**，这是服务端建树的正确遍历顺序，
          而且只用一次查询、不用递归 CTE。
        """
        stmt = self.base_select().order_by(Department.path, Department.id)
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()

    async def sibling_named(self, *, name: str, parent_id: int | None) -> Department | None:
        """查同一上级下是否已有同名部门（受守卫保护）。"""
        stmt = self.base_select().where(
            Department.name == name,
            Department.parent_id == parent_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()
