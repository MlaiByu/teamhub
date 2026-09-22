"""租户领域：Tenant（隔离的根）与 TenantMember。

★ Tenant 不继承 TenantScopedMixin——它自己就是隔离的根（7.3-1）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import MemberStatus, TenantStatus
from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import TenantScopedMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TenantStatus.ACTIVE, server_default="ACTIVE"
    )
    max_members: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=50, server_default="50"
    )


class TenantMember(Base, TenantScopedMixin, TimestampMixin):
    """用户与租户的成员关系。

    这是「关联表也必须带 tenant_id」的落地点——关联表是泄露后门（7.1 规范）。
    """

    __tablename__ = "tenant_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_members_tenant_user"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MemberStatus.ACTIVE, server_default="ACTIVE"
    )
    # 成员在本租户内所属部门。DEPT 数据范围以此为准。
    dept_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
