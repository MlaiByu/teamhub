"""附件仓储。`Attachment` 是租户级表 → `TenantAwareRepository`。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select

from app.models import Attachment
from app.repositories.base import TenantAwareRepository


class AttachmentRepository(TenantAwareRepository[Attachment]):
    model = Attachment

    def filtered_select(self, *, biz_type: str | None, biz_id: int | None) -> Select:
        """按归属过滤。租户过滤由钩子注入，这里只加业务筛选。"""
        stmt = self.base_select()
        if biz_type is not None:
            stmt = stmt.where(Attachment.biz_type == biz_type)
        if biz_id is not None:
            stmt = stmt.where(Attachment.biz_id == biz_id)
        return stmt.order_by(Attachment.id)

    async def list_for_biz(
        self, *, biz_type: str | None, biz_id: int | None
    ) -> Sequence[Attachment]:
        """列出附件（可按归属过滤）。"""
        result = await self.session.execute(self.filtered_select(biz_type=biz_type, biz_id=biz_id))
        return result.scalars().unique().all()
