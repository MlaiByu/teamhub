"""任务仓储。`Task` 是租户级表（且带 DataScopedMixin）→ `TenantAwareRepository`。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select

from app.models import Task
from app.repositories.base import TenantAwareRepository


class TaskRepository(TenantAwareRepository[Task]):
    model = Task

    def filtered_select(
        self,
        *,
        project_id: int | None = None,
        assignee_id: int | None = None,
        status: str | None = None,
    ) -> Select:
        """带守卫与筛选的查询起点。

        ★ 只接业务筛选字段。tenant_id / owner_id / dept_id 一律交给钩子——
          租户过滤与数据范围过滤都在那里注入。
        """
        stmt = self.base_select()
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        if assignee_id is not None:
            stmt = stmt.where(Task.assignee_id == assignee_id)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        return stmt.order_by(Task.id.desc())

    async def paginate_filtered(
        self,
        *,
        project_id: int | None = None,
        assignee_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[Task], int]:
        """分页 + 过滤。`total` 与 `items` 同源（同一条 stmt 套 count）。"""
        stmt = self.filtered_select(project_id=project_id, assignee_id=assignee_id, status=status)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self.session.execute(count_stmt)).scalar_one())

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        result = await self.session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().unique().all(), total
