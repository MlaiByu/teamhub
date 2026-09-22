"""项目仓储。`Project` 是租户级表（且带 DataScopedMixin）→ `TenantAwareRepository`。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select

from app.models import Project
from app.repositories.base import TenantAwareRepository


class ProjectRepository(TenantAwareRepository[Project]):
    model = Project

    def filtered_select(self, *, status: str | None = None) -> Select:
        """带守卫与筛选的查询起点。

        ★ 只接 `status`，不接 tenant_id / dept_id / owner_id：
          租户过滤由钩子注入；数据范围过滤也由钩子按当前 SELF/DEPT/ALL 注入。
          在这里手写任何一个都会与钩子重复，并让 bypass 场景冲突。
        """
        stmt = self.base_select()
        if status is not None:
            stmt = stmt.where(Project.status == status)
        return stmt.order_by(Project.id.desc())

    async def paginate_filtered(
        self, *, status: str | None = None, page: int = 1, page_size: int = 20
    ) -> tuple[Sequence[Project], int]:
        """分页 + 筛选。返回 (items, total)。

        ★ total 用「同一个 stmt 去掉 order_by 再套 count」算，
          而不是另写一条 count 查询——后者一旦漏掉某个筛选条件，
          total 与 items 就会对不上（前端分页最典型的 bug）。
        """
        stmt = self.filtered_select(status=status)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self.session.execute(count_stmt)).scalar_one())

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        result = await self.session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().unique().all(), total

    async def get_by_code(self, code: str) -> Project | None:
        """按项目编码查（租户内）。用于创建前的重名预检。"""
        stmt = self.base_select().where(Project.code == code)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()
