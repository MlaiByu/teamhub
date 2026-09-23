"""附件仓储。`Attachment` 是租户级表 → `TenantAwareRepository`。

★ 附件表**不带** `DataScopedMixin`，所以本层的查询只保证租户隔离，
  **不保证数据范围隔离**。数据范围由调用方（service）负责——两者分工：

      带 biz_type + biz_id  → service 先校验该实体可见，再调 list_for_biz
      不带（或只带一个）    → service 先算出「可见实体 id 集合」，再调 list_for_scope

  这个分工有意写在方法名上（`list_for_biz` vs `list_for_scope`），
  提醒调用方「前者需要你自己先校验过」。为什么附件表不带 DataScopedMixin
  见 `list_for_scope` 的注释。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, and_, or_

from app.core.constants import AttachmentBizType
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
        """列出某个归属实体的附件。

        ★ **调用方必须先校验该实体对当前身份可见**（走实体自己的守卫查询）。
          本方法只保证租户隔离，不做数据范围过滤。
        """
        result = await self.session.execute(self.filtered_select(biz_type=biz_type, biz_id=biz_id))
        return result.scalars().unique().all()

    async def list_for_scope(
        self, *, project_ids: set[int], task_ids: set[int]
    ) -> Sequence[Attachment]:
        """只返回挂靠实体落在给定可见集合内的附件。

        ★ 为什么不直接给 Attachment 加 `DataScopedMixin`：
          附件的可见性语义是「**继承挂靠实体**」而不是「继承上传者」。
          给附件表加 owner_id/dept_id 过滤会把语义变成「上传者自己的范围」——
          那既不对（A 传附件到 B 负责的任务，B 应该能看到），
          又要为附件补 dept_id 列与迁移。

          所以这里显式把「可见实体 id 集合」传进来做 IN 过滤。集合由 service
          用**实体自己的守卫查询**算出（自动带上租户 + 数据范围两层过滤），
          因此本查询虽然只显式写了 IN，实际已间接应用了完整的两层过滤。

        ★ 两个集合都为空时，`or_()` 的每个分支都恒假，结果为空——正确。
          （SQLAlchemy 对 `col.in_([])` 会渲染成恒假表达式。）
        """
        stmt = (
            self.base_select()
            .where(
                or_(
                    and_(
                        Attachment.biz_type == str(AttachmentBizType.PROJECT),
                        Attachment.biz_id.in_(project_ids),
                    ),
                    and_(
                        Attachment.biz_type == str(AttachmentBizType.TASK),
                        Attachment.biz_id.in_(task_ids),
                    ),
                )
            )
            .order_by(Attachment.id)
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()
