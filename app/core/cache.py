"""缓存封装。

★★ 缓存 key **必须**带租户前缀（PROJECT-PLAN 风险 10）。

  这是与「批量写漏 tenant_id」同一类问题的**另一个面**：
  隔离机制有一个没覆盖到的存储层。数据库那边至少有 NOT NULL 约束和
  查询钩子兜底；**缓存就是个裸 KV，key 写错了不会有任何报错**，
  只会让 B 租户读到 A 租户的数据。

  对策（结构性，不靠自觉）：**key 只能由本模块构造**，
  且每个公开函数的第一个参数都是 `tenant_id`——
  业务代码**没有机会**漏掉租户这一维，因为漏了就少一个必填参数。

★ 值统一用 JSON 序列化：缓存里存的往往是 dict / list，
  直接 str() 会得到 Python 字面量（单引号、True/None），
  跨语言/跨版本读取时解析不了。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

CACHE_PREFIX = "cache"


def cache_key(tenant_id: int, *parts: str | int) -> str:
    """构造缓存 key，形如 `cache:tenant:1:notifications:unread:42`。

    ★ 所有缓存 key 都必须经这里生成。不要在任何地方手写缓存 key 字符串——
      手写就会漏租户前缀，而那是个**静默**的跨租户泄露。
    """
    suffix = ":".join(str(p) for p in parts)
    return f"{CACHE_PREFIX}:tenant:{tenant_id}:{suffix}"


async def cache_get(tenant_id: int, *parts: str | int) -> Any | None:
    raw = await get_redis().get(cache_key(tenant_id, *parts))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 缓存内容坏了不该让请求失败——当成未命中，让它回源重建。
        logger.warning("cache_value_corrupted", key=cache_key(tenant_id, *parts))
        return None


async def cache_set(tenant_id: int, *parts: str | int, value: Any, ttl: int = 60) -> None:
    await get_redis().set(cache_key(tenant_id, *parts), json.dumps(value), ex=ttl)


async def cache_delete(tenant_id: int, *parts: str | int) -> None:
    await get_redis().delete(cache_key(tenant_id, *parts))


async def cache_get_or_set[T](
    tenant_id: int,
    *parts: str | int,
    factory: Callable[[], Awaitable[T]],
    ttl: int = 60,
) -> T:
    """读缓存，未命中则调 `factory` 回源并写入。

    ★ 不做「并发合并」（single-flight）：多个请求同时未命中时会各自回源一次。
      对当前的热点接口（一次 COUNT 查询）而言，多几次回源比引入
      分布式锁的复杂度划算；真需要时再补。
    """
    cached = await cache_get(tenant_id, *parts)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    value = await factory()
    await cache_set(tenant_id, *parts, value=value, ttl=ttl)
    return value
