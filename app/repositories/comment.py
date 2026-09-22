"""任务评论仓储。

★ `TaskComment` 是租户级表但**不带** `DataScopedMixin`。这是有意的：

  评论的可见性**继承所属任务**，而不是自己算一套数据范围。
  实现方式是不提供「按评论查评论」的入口——所有读取都先取任务
  （`TaskRepository.get_or_404`，受租户 + 数据范围双重过滤），
  再按 task_id 取评论。这样 SELF 范围的成员看不到别人任务的评论，
  不需要给评论表重复实现一套 owner_id/dept_id 逻辑。

  代价：本模块的每个方法都必须由调用方先完成任务的可见性校验。
  这一点写在各方法 docstring 里。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import TaskComment
from app.repositories.base import TenantAwareRepository


class TaskCommentRepository(TenantAwareRepository[TaskComment]):
    model = TaskComment

    async def list_for_task(self, task_id: int) -> Sequence[TaskComment]:
        """按任务取评论，时间正序（讨论顺序）。

        ★ 调用方必须**先**通过 `TaskRepository.get_or_404` 校验任务可见性。
          本方法只保证租户隔离，不保证数据范围——评论表没有 owner_id/dept_id。
        """
        stmt = self.base_select().where(TaskComment.task_id == task_id).order_by(TaskComment.id)
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()
