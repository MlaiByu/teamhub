"""两个标记 Mixin：区分「租户级」与「需要数据权限」的表。

用法：
    class Task(Base, TenantScopedMixin, DataScopedMixin, TimestampMixin):
        ...

租户根表 Tenant 不继承 TenantScopedMixin（PROJECT-PLAN 7.3-1）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class TenantScopedMixin:
    """所有租户级表必带 tenant_id。

    - NOT NULL 兜底：INSERT 不经过钩子，漏赋值时数据库直接拒绝（坑 2）
    - index=True 且作为复合索引首列（7.1 约定）
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(
            BigInteger,
            ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )


class DataScopedMixin:
    """需要 SELF / DEPT / ALL 三级数据范围过滤的表。

    约定：
    - owner_id 是「数据归属人」，SELF 范围用它比较
    - dept_id 是「数据归属部门」，DEPT 范围用它比较
    两者都必须非空，否则 DEPT/SELF 过滤会静默放行 NULL 行。
    """

    @declared_attr
    def owner_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(BigInteger, nullable=False, index=True)

    @declared_attr
    def dept_id(cls) -> Mapped[int | None]:  # noqa: N805
        # 允许为空：平台级/跨部门数据没有归属部门。
        # 此时 DEPT 范围下该行不可见——这是有意的保守行为。
        return mapped_column(BigInteger, nullable=True, index=True)
