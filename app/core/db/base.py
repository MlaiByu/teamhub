"""DeclarativeBase 与通用列约定。

★ 结构硬约束（PROJECT-PLAN 4.2）：
    base.py / mixins.py / session.py / tenant_hook.py 必须同处 core/db/。
    因为 Mixin 被所有领域 model 引用，钩子又引用 Mixin——
    分开放会形成 core → models → core 循环导入，启动即报错。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全项目唯一的声明基类。

    注意：不要在这里放 tenant_id。租户维度由 TenantScopedMixin 显式声明，
    这样「哪些表是租户级」在代码里是一眼可见的，而不是隐式继承来的。
    """


class TimestampMixin:
    """created_at / updated_at / created_by / is_deleted 通用列（PROJECT-PLAN 7.1）。

    ★ `updated_at` 为什么用 Python 侧 callable 而不是 server-side 的
      `onupdate=func.now()`：

      前者（server_default / onupdate 都用 func.now()）的值由**数据库**生成，
      SQLAlchemy 在 UPDATE 之后会把这类列标记为「过期」，下次访问需要回库取。
      同步代码里那只是一次隐式 SELECT，但异步下隐式 IO 会直接抛
      `MissingGreenlet`——所以每个 UPDATE 接口都得多写一行 `await session.refresh()`，
      漏一个就是一个运行期 bug（已经漏过两次）。

      改成 `onupdate=lambda: datetime.now(UTC)` 后，SQLAlchemy 自己就知道
      新值、不会标记过期，UPDATE 后直接能读，`refresh()` 的负担整个消失。

      代价（**有意的取舍**）：时间戳来源从 DB 时钟变成**应用时钟**。
      影响：
      - 多实例部署时，各实例时钟漂移会让 updated_at 有微小偏差。
        对「协作平台展示「最后更新于」」这个用途而言可接受；
        若将来需要严格单调/审计级时间，再回到 DB 时钟并接受 refresh 负担。
      - `created_at` 仍用 server_default=func.now()：INSERT 不受 MissingGreenlet
        影响（插入时顺带取回 server_default），所以保留 DB 时钟，两者各取所需。

    ★ 时区一致性（踩过坑）：
      `onupdate` 的 Python 值必须与 `func.now()` 在**各后端**返回的类型对齐。
      SQLite 的 `CURRENT_TIMESTAMP` 返回 **naive** datetime（它不存时区），
      若 Python 侧返回 `datetime.now(UTC)`（aware），插入后 created_at 是 naive、
      更新后 updated_at 是 aware，两者比较会直接 `TypeError: can't compare
      offset-naive and offset-aware datetimes`。所以这里统一返回 naive UTC
      （`.replace(tzinfo=None)`），与 SQLite 行为一致；PostgreSQL 的
      `DateTime(timezone=True)` 会存带时区的 timestamptz，两边也都相容。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC).replace(tzinfo=None),
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
