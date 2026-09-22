"""项目领域。

★ 第 4 周的真实教训在这里落地：项目与任务几乎无法独立演进
（查任务必然 join 项目），所以放在横向层里用 project_id 外键关联，
而不是各自建 domains/ 目录（PROJECT-PLAN 6.4）。
"""

from __future__ import annotations

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import ProjectStatus
from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import DataScopedMixin, TenantScopedMixin


class Project(Base, TenantScopedMixin, DataScopedMixin, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        # 唯一约束含 tenant_id：租户 A 和 B 可以用同一个项目编码
        UniqueConstraint("tenant_id", "code", name="uq_projects_tenant_code"),
        # 索引以 tenant_id 打头
        Index("ix_projects_tenant_dept_status", "tenant_id", "dept_id", "status"),
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProjectStatus.PLANNING, server_default="PLANNING"
    )
