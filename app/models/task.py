"""任务领域：Task / TaskComment。

★ task.project_id 的外键只保证 id 存在，**不保证同租户**（7.1 规范第 4 条）。
    挂载时必须在 service 层校验 project.tenant_id == 当前租户，
    否则可以把 A 租户的任务挂到 B 租户的项目上。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import TaskPriority, TaskStatus
from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import DataScopedMixin, TenantScopedMixin


class Task(Base, TenantScopedMixin, DataScopedMixin, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_tenant_project_status", "tenant_id", "project_id", "status"),
        Index("ix_tasks_tenant_assignee", "tenant_id", "assignee_id"),
    )

    id: Mapped[int] = pk_column()
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000), nullable=True)

    # 执行人：通知与「我的任务」用它过滤
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.TODO, server_default="TODO"
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskPriority.MEDIUM, server_default="MEDIUM"
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskComment(Base, TenantScopedMixin, TimestampMixin):
    """任务评论。@提及解析后触发通知（阶段 2）。"""

    __tablename__ = "task_comments"
    __table_args__ = (Index("ix_task_comments_tenant_task", "tenant_id", "task_id"),)

    id: Mapped[int] = pk_column()
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(String(4000), nullable=False)
