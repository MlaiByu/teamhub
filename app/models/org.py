"""组织领域：Department（部门树）。

树用 parent_id + path 双写：parent_id 便于挂接，path 便于子树查询
（避免递归 CTE）。path 形如 '/1/5/12/'。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import TenantScopedMixin


class Department(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "departments"
    __table_args__ = (
        # 索引以 tenant_id 打头（7.1 规范）
        Index("ix_departments_tenant_parent", "tenant_id", "parent_id"),
    )

    id: Mapped[int] = pk_column()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 物化路径，用于快速取子树。根节点形如 '/1/'。
    path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # 部门负责人，DEPT 数据范围下作为「上级可见」依据之一
    leader_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
