"""应用组装点（架构规则 1：只做组装）。

中间件注册顺序是**反向**的：后 add 的先执行。
期望的请求链路（PROJECT-PLAN 6.1）：

    RequestIDMiddleware  → 最早，保证之后所有日志都带 request_id
      └ TenantContextMiddleware → 写 contextvar
          └ RateLimitMiddleware → 依赖租户上下文
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.db.session import engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis, init_redis
from app.middleware import (
    RateLimitMiddleware,
    RequestIDMiddleware,
    TenantContextMiddleware,
)

configure_logging(level="DEBUG" if settings.debug else "INFO", json_output=not settings.debug)
logger = get_logger(__name__)


async def _bootstrap_local_schema() -> None:
    """零依赖本地模式：为 SQLite 内存库按模型建表。

    ★ 为什么必须有这一步：
      「零依赖降级模式」（PROJECT-PLAN 11.2）用的是 SQLite **内存库**。
      内存库随进程生灭，而 `alembic upgrade head` 是**另一个进程**——
      它迁移的是一个独立的、启动即空的内存库，对服务进程看到的库毫无影响。
      结果就是：本地起服务后表是空的，`/health` 能过（它故意不查数据库），
      但任何碰数据库的接口都报 `no such table`。
      这个缺口 2026-09-22 端到端跑真实服务时才暴露——测试与 CI 都自己建了表，
      所以全绿。

    ★ 为什么这里可以用 create_all 而不是跑迁移：
      内存库每次启动都是全新的，不存在「历史版本」需要演进，
      按当前模型建表就是它唯一合理的初始状态。
      持久化数据库（PostgreSQL / 文件 SQLite）**不走这条路**——
      它们的表结构必须由 Alembic 管理，否则本地与线上会漂移。

    ★ 边界：只在「local / test + SQLite 内存库」下执行。
      判断条件故意写得很窄：宁可本地少建一次表（用户会看到明确的报错），
      也不能在生产上悄悄按模型改结构。
    """
    if settings.app_env not in ("local", "test"):
        return
    url = settings.database_url
    if not url.startswith("sqlite") or ":memory:" not in url:
        return

    # 惰性导入：`core` 不许反向依赖业务，所以不能在 core/db/session.py 里
    # 引 app.models。放在组装点（main.py）且只在本地模式下执行，方向是对的。
    # 必须 import app.models，否则 Base.metadata 是空的（迁移也会漏表）。
    import app.models  # noqa: F401
    from app.core.db.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.warning(
        "local_schema_bootstrapped",
        tables=len(Base.metadata.tables),
        note="内存库按当前模型建表（非 Alembic）。持久化数据库请用 alembic upgrade head。",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("app_startup", env=settings.app_env, db=settings.database_url.split("://")[0])
    await _bootstrap_local_schema()
    await init_redis()
    try:
        yield
    finally:
        await close_redis()
        await engine.dispose()
        logger.info("app_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} API",
        version="1.0.0",
        description="多租户团队协作平台。共享库 + 行级隔离，查询层自动注入双层过滤。",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # 注意顺序：add_middleware 是栈式（后进先出）
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TenantContextMiddleware)
    app.add_middleware(RequestIDMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["系统"], summary="健康检查")
    async def health() -> dict:
        """第 1 周验收标准之一：项目能起来，/health 通。

        注意这里**故意不查数据库**——健康检查失败时若依赖 DB，
        会把「DB 连不上」和「服务没起来」混为一谈。
        """
        return {"code": 0, "message": "ok", "data": {"status": "healthy", "env": settings.app_env}}

    return app


app = create_app()
