"""通知仓储。`Notification` 是租户级表 → `TenantAwareRepository`。

★ 每个查询都**同时**按 `tenant_id`（钩子注入）与 `user_id`（显式）过滤：
  租户隔离保证看不到别家公司的通知，`user_id` 保证看不到**同租户里别人**的通知。
  两者是不同维度的越权，缺一不可——只靠钩子的话，同租户内你能读到
  同事的全部通知（任务标题、评论摘要都在 payload 里）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, update

from app.models import Notification
from app.repositories.base import TenantAwareRepository


class NotificationRepository(TenantAwareRepository[Notification]):
    model = Notification

    def _for_user(self, *, user_id: int, unread_only: bool) -> Select:
        stmt = self.base_select().where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return stmt.order_by(Notification.id.desc())

    async def paginate_for_user(
        self, *, user_id: int, unread_only: bool, page: int, page_size: int
    ) -> tuple[Sequence[Notification], int]:
        """分页取某用户的通知。`total` 与 `items` 同源（同一条 stmt 套 count）。"""
        stmt = self._for_user(user_id=user_id, unread_only=unread_only)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int((await self.session.execute(count_stmt)).scalar_one())

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        result = await self.session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().unique().all(), total

    async def count_unread(self, *, user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.is_deleted.is_(False),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def get_for_user(self, *, notification_id: int, user_id: int) -> Notification | None:
        """取某条属于该用户的通知。别人 / 别的租户的一律返回 None。

        ★ 有了它，「标记已读」才能干净地区分三种情况：
          不存在、不是你的、已经是已读。只靠 UPDATE 的 rowcount 分不清，
          会把「重复标记」误判成 404。
        """
        stmt = self.base_select().where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def mark_one_read(self, *, notification_id: int, user_id: int) -> int:
        """把某条通知标记为已读。返回受影响行数。

        ★ 必须带 `user_id` 条件：否则「知道别人的通知 id 就能把它标成已读」
          ——虽然读不到内容，但能篡改别人的未读状态（同样是越权）。
          带 user_id 后，别人的 id 受影响行数为 0，调用方据此返回 404。
        """
        stmt = (
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.is_deleted.is_(False),
            )
            .values(read_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)

    async def mark_all_read(self, *, user_id: int) -> int:
        """把当前租户内该用户的全部未读标记为已读，返回条数。"""
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
                Notification.is_deleted.is_(False),
            )
            .values(read_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)
