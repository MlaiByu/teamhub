"""Redis 客户端（真实 Redis / fakeredis 降级）。

★ 降级语义与数据库的 SQLite 内存库一致（PROJECT-PLAN 11.2）：
  `USE_FAKEREDIS=true` 时用内存版 Redis，不需要任何外部服务即可跑起整套功能。
  对调用方完全透明——它们只看 `get_redis()` 返回的客户端接口。

★ 为什么是**惰性初始化**（而不是只在 lifespan 里建）：

  测试与脚本不会跑 FastAPI 的 lifespan。如果客户端只能在 lifespan 里创建，
  任何直接调 service/repository 的测试都会拿到 None。
  惰性创建让「第一次用到时自动建」成立，同时保留显式的 init/close 供
  lifespan 管理连接生命周期——两种用法都工作。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Any = None


def _create_client() -> Any:
    if settings.use_fakeredis:
        from fakeredis import aioredis

        # decode_responses=True：直接拿 str，省去每处 .decode()。
        # 缓存用 JSON 序列化，返回 bytes 会让调用方多一层处理。
        logger.warning(
            "redis_using_fakeredis",
            note="零依赖降级模式：使用进程内 Redis（数据不跨进程、重启即失）",
        )
        return aioredis.FakeRedis(decode_responses=True)

    from redis.asyncio import Redis

    logger.info("redis_connected", url=settings.redis_url)
    return Redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> Any:
    """取 Redis 客户端（首次调用时惰性创建）。"""
    global _client
    if _client is None:
        _client = _create_client()
    return _client


async def init_redis() -> None:
    """启动时显式初始化（供 lifespan 用；不调也能靠惰性创建工作）。"""
    get_redis()


async def close_redis() -> None:
    """关闭连接。进程退出/测试收尾时调用，避免连接泄漏。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def reset_client_for_tests() -> None:
    """把模块级客户端置空，让下次 `get_redis()` 重建。

    ★ 测试为什么需要它：fakeredis 是**进程内单例语义**——
      上一个用例写的 key 会留到下一个用例，造成用例间互相污染
      （表现为「单独跑绿、一起跑红」）。测试里配合 `_schema` 一起重置即可。
    """
    global _client
    _client = None
