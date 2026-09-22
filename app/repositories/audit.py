"""审计仓储。`AuditLog` 是租户级表 → `TenantAwareRepository`。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, select

from app.models import AuditLog
from app.repositories.base import TenantAwareRepository


class AuditLogRepository(TenantAwareRepository[AuditLog]):
    model = AuditLog

    def filtered_select(
        self,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        action: str | None = None,
        user_id: int | None = None,
    ) -> Select:
        stmt = self.base_select()
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        return stmt.order_by(AuditLog.id.desc())

    async def paginate_filtered(
        self,
        *,
        entity_type: str | None = None,
        entity_id: int | None = None,
        action: str | None = None,
        user_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[AuditLog], int]:
        stmt = self.filtered_select(
            entity_type=entity_type, entity_id=entity_id, action=action, user_id=user_id
        )
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self.session.execute(count_stmt)).scalar_one())

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        result = await self.session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().unique().all(), total
