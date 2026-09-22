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

_is_sqlite = settings.database_url.startswith("sqlite")

_engine_kwargs: dict = {"echo": settings.db_echo, "future": True}
if _is_sqlite:
    # StaticPool：所有会话共用同一条内存连接，否则每个连接一个空库。
    _engine_kwargs["poolclass"] = StaticPool
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
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
