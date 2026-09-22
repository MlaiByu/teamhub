"""认证领域：User（全局表）与 RefreshToken（租户级）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import TenantScopedMixin


class User(Base, TimestampMixin):
    """用户是全局表：同一手机/邮箱可加入多个租户。

    所以 username 全局唯一，而**不是** (tenant_id, username)（7.2）。
    User 也不继承 TenantScopedMixin。
    """

    __tablename__ = "users"

    id: Mapped[int] = pk_column()
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class RefreshToken(Base, TenantScopedMixin, TimestampMixin):
    """refresh token 落库，支持撤销与复用检测（8.2 / 7.3-3）。

    不落库的后果：登出后旧 token 仍然有效，这是常见漏洞。
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (UniqueConstraint("jti", name="uq_refresh_tokens_jti"),)

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    jti: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 复用检测：记录该 token 是被哪一次 refresh 换掉的，用于定位泄露链
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
