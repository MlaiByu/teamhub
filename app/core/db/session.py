"""engine / sessionmaker / get_db 会话依赖。

本地零依赖降级（PROJECT-PLAN 11.2）：
    DATABASE_URL=sqlite+pysqlite:///:memory: 时自动启用 StaticPool，
    让内存库能跨会话共享——这是隔离测试「用全新会话」可行的前提（坑 4）。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import settings

# 先 import 钩子所在模块，确保 do_orm_execute 事件在 engine 创建前已注册。
from app.core.db import tenant_hook  # noqa: F401
from app.core.logging import get_logger

logger = get_logger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": settings.db_echo, "future": True}
if _is_sqlite:
    # ★ StaticPool **只给内存库用**：
    #   内存库随连接生灭，不用 StaticPool 的话每个连接会看到**各自的空库**
    #   （隔离测试「用全新会话」正是靠它才可行，见坑 4）。
    #
    #   但**文件库绝对不能套 StaticPool**——那会让所有并发请求共用同一条
    #   SQLite 连接。压测实测到的后果是：
    #
    #       sqlite3.OperationalError: cannot commit transaction - SQL statements in progress
    #
    #   一个请求正在连接上执行语句时，另一个请求的 commit 就会撞上它
    #   → 混合读写场景下 8%~14% 的请求直接 500。
    #   （功能测试全绿也发现不了：单线程串行执行永远不会「语句在进行中」。）
    _file_db = ":memory:" not in settings.database_url
    if not _file_db:
        _engine_kwargs["poolclass"] = StaticPool
    _engine_kwargs["connect_args"] = {
        "check_same_thread": False,
        # ★ timeout：SQLite 同一时刻只允许一个写者。文件库下并发写会遇到
        #   锁竞争，默认行为是**立刻抛 `database is locked`**；
        #   给一个等待窗口，让写操作排队而不是直接失败。
        #   （真正的并发写入能力要靠 PostgreSQL，SQLite 只用于降级模式。）
        "timeout": 30,
    }
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # 提交后仍可读属性，避免 MissingGreenlet（风险 5）
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。"""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def safe_rollback(session: AsyncSession, *, context: str = "") -> None:
    """尽力回滚，**绝不抛异常**。

    ★ 为什么需要它（这是第 10 周压测发现的真实问题）：

      `await session.rollback()` 在**连接已经不可用**时会自己抛异常。
      压测中实际看到的形态是：

          sqlite3.OperationalError: cannot commit transaction - SQL statements in progress

      ——SQLite 在并发下共享连接时会撞上这个；连接随即处于坏状态，
      此时再去 rollback，**rollback 自己也炸**。

      后果有两种，都很隐蔽：

        1. 本该「吞掉并降级」的路径（审计 `record`、通知 `emit`）
           变成一个逃出 except 块的新异常 → 接口 500，
           正好违背这两个函数「绝不抛异常」的承诺。
        2. 本该「重新抛出语义化异常」的路径（如 `ConflictError` → 409）
           被清理动作的异常**替换**掉，用户看到 500 而不是 409。

      根因是把「业务错误处理」和「资源清理」混在一个 except 里：
      清理动作必须与业务异常**隔离**——它失败只记日志。

    ★ 调用顺序：先 `logger.exception` 记下**原始**异常，再调本函数。
      反过来的话，日志里就只剩清理失败了，真正的原因反而丢失。
    """
    try:
        await session.rollback()
    except Exception:
        logger.warning("session_rollback_failed", context=context)
