"""pytest 全局夹具。

★ 三个关键设计（PROJECT-PLAN 4.3 坑 4 / 10.2 / 11.2）：

1. **StaticPool + 内存 SQLite**
   内存库默认是「每连接一个空库」。要跨会话共享，必须 StaticPool。
   否则隔离测试里「用全新会话验证」会查到一个空库，测试永远绿——假通过。

2. **每个用例独立建表 / 删表（`function` 作用域）**
   隔离测试的核心断言是「A 看不到 B」。如果数据跨用例残留，
   断言失败时你分不清是隔离有问题还是脏数据。宁可慢一点。

3. **`db_session()` 工厂夹具**
   隔离测试**必须**能用全新会话重查（避开 identity map 缓存）。
   提供工厂而不是单个 session，就是逼着测试显式跨会话。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable

# 必须在 import app.* 之前设置，因为 settings 是模块级单例
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("USE_FAKEREDIS", "true")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

# 导入所有模型，确保 Base.metadata 完整（漏导入会安静地少建表）
import app.models  # noqa: E402, F401
from app.core.constants import DataScope  # noqa: E402
from app.core.db.base import Base  # noqa: E402
from app.core.db.context import RequestContext, reset_context, set_context  # noqa: E402
from app.core.db.session import SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402


@pytest_asyncio.fixture(scope="function")
async def _schema() -> AsyncGenerator[None, None]:
    """每用例一套干净的表结构。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        reset_context()


@pytest_asyncio.fixture
async def db_session_factory(_schema: None) -> AsyncGenerator[Callable, None]:
    """返回「开新会话」的工厂。隔离测试跨会话验证时必须用它。

    用法：
        s1 = db_session_factory()
        ...
        await s1.close()
        s2 = db_session_factory()   # 全新会话，identity map 为空
    """
    opened = []

    def _make():
        s = SessionLocal()
        opened.append(s)
        return s

    yield _make

    for s in opened:
        await s.close()


@pytest_asyncio.fixture
async def db(db_session_factory) -> AsyncGenerator:
    """默认会话，普通用例直接用这个。"""
    async with db_session_factory() as session:
        yield session


@pytest.fixture
def tenant_ctx():
    """设置租户上下文的小工具。

    用例里显式写 `tenant_ctx(tenant_id=1, user_id=10, scope=DataScope.ALL)`
    比在每个测试里手写 contextvar 更不容易写漏——尤其是不容易
    「忘记设置 data_scope」导致 SELF/DEPT 用例假通过。
    """

    def _set(
        *,
        tenant_id: int | None,
        user_id: int | None = None,
        dept_id: int | None = None,
        scope: DataScope = DataScope.SELF,
        bypass: bool = False,
    ) -> None:
        set_context(
            RequestContext(
                tenant_id=tenant_id,
                user_id=user_id,
                dept_id=dept_id,
                data_scope=scope,
                bypass=bypass,
            )
        )

    yield _set
    reset_context()


@pytest_asyncio.fixture
async def client(_schema: None) -> AsyncGenerator[AsyncClient, None]:
    """ASGI 直连客户端，不需要起 uvicorn。"""
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
