"""附件 / 通知 / 审计 —— 第 1 周先建表结构，功能在阶段 2–3 落地。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.mixins import TenantScopedMixin

# ★ 生产用 JSONB（支持 GIN 索引与 @> 查询），SQLite 降级为通用 JSON。
#   不能直接 `from sqlalchemy.dialects.postgresql import JSONB` 当类型用——
#   SQLite 的编译器渲染不了 JSONB，建表时直接 CompileError。
#   with_variant 让方言各取所需，本地零依赖模式才能跑（11.2）。
JSONColumn = JSON().with_variant(JSONB(), "postgresql")


class Attachment(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_tenant_biz", "tenant_id", "biz_type", "biz_id"),)

    id: Mapped[int] = pk_column()
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    biz_type: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Notification(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant_user_read", "tenant_id", "user_id", "read_at"),
    )

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base, TenantScopedMixin, TimestampMixin):
    """操作审计。异步落库（阶段 2），但表结构第 1 周就位。"""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[int] = pk_column()
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONColumn, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # bypass 调用强制标记，便于事后筛查敏感路径（4.5）
    is_bypass: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
