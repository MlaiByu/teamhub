"""RBAC 领域：Role / Permission / RolePermission / UserRole。

★ 设计要点（7.3-2）：
    permissions 是**全局字典**（不带 tenant_id），权限点由平台维护，
    租户只能组合不能新增，避免权限体系失控。
    其余三张表都是租户级。

★ 数据范围绑在 Role 上（5.1），用户多角色时取最宽的那个
    （计算收敛在 core/constants.widest_scope）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.constants import DataScope
from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import TenantScopedMixin


class Permission(Base, TimestampMixin):
    """全局权限字典。如 project:create、task:assign、audit:read。"""

    __tablename__ = "permissions"

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 所属功能模块，便于前端分组展示
    module: Mapped[str] = mapped_column(String(64), nullable=False)


class Role(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "roles"
    __table_args__ = (
        # 唯一约束必须含 tenant_id，否则两个租户不能使用相同角色编码（7.1）
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_code"),
    )

    id: Mapped[int] = pk_column()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 数据范围：SELF / DEPT / ALL
    data_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DataScope.SELF, server_default="SELF"
    )


class RolePermission(Base, TenantScopedMixin, TimestampMixin):
    """角色-权限绑定。关联表也带 tenant_id（泄露后门）。"""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "role_id", "permission_id", name="uq_role_permissions_triple"
        ),
        Index("ix_role_permissions_tenant_role", "tenant_id", "role_id"),
    )

    id: Mapped[int] = pk_column()
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("roles.id"), nullable=False)
    permission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("permissions.id"), nullable=False
    )


class UserRole(Base, TenantScopedMixin, TimestampMixin):
    """用户-角色绑定。同一用户在一个租户内可绑多个角色。"""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "role_id", name="uq_user_roles_tenant_user_role"),
        Index("ix_user_roles_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("roles.id"), nullable=False)
