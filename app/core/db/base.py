"""DeclarativeBase 与通用列约定。

★ 结构硬约束（PROJECT-PLAN 4.2）：
    base.py / mixins.py / session.py / tenant_hook.py 必须同处 core/db/。
    因为 Mixin 被所有领域 model 引用，钩子又引用 Mixin——
    分开放会形成 core → models → core 循环导入，启动即报错。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全项目唯一的声明基类。

    注意：不要在这里放 tenant_id。租户维度由 TenantScopedMixin 显式声明，
    这样「哪些表是租户级」在代码里是一眼可见的，而不是隐式继承来的。
    """


class TimestampMixin:
    """created_at / updated_at / created_by / is_deleted 通用列（PROJECT-PLAN 7.1）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    # 操作人，用于审计。系统写入时允许为空。
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 软删除。租户过滤钩子注入时必须同步排除 is_deleted。
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )


def pk_column() -> Mapped[int]:
    """BIGSERIAL 主键。

    SQLite 下 BIGINT 不会自增（只有 INTEGER PRIMARY KEY 才会），
    所以这里用 with_variant 做方言适配，保证本地零依赖模式能跑。
    生产 PostgreSQL 仍是 BIGSERIAL。
    """
    return mapped_column(
        BigInteger().with_variant(__import__("sqlalchemy").Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
