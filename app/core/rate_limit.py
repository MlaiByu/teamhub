"""限流算法（滑动窗口）。

★★ 为什么**不用 Lua**（方案原本建议的 ZSET + Lua 原子脚本）：

  项目的硬要求之一是「零依赖降级模式」（PROJECT-PLAN 11.2）——
  `USE_FAKEREDIS=true` 时不需要任何外部服务就能跑起整套功能。
  而 **fakeredis 不支持 Lua**（实测 `EVAL` 直接返回
  `unknown command 'eval'`，除非额外引入 `lupa` 这个 C 扩展依赖）。

  两者冲突时必须选一个。这里选**保住零依赖**，改用
  **Redis 事务（MULTI/EXEC）+ ZSET** 达到原子性——只用基础命令，
  真实 Redis 与 fakeredis 都支持，且**语义仍是滑动窗口**
  （不是退化成固定窗口，所以没有「窗口边界双倍突发」的问题）。

★ 两步而非一步，以及这个取舍为什么安全：

  `MULTI/EXEC` 里读不到中间结果（`ZCARD` 的值要 EXEC 之后才知道），
  所以流程必然是「先看计数 → 再决定写」。两步之间理论上存在竞态：

      多个并发请求同时读到 count = limit - 1 → 都放行 → 实际略微超限

  但误差方向是**偏保守的少量超限**（几个请求），不是**放行失控**。
  对「按租户 600/分钟」这种粗粒度限流，这个误差完全可接受；
  如果真要求严格不超，就得回到 Lua，代价是零依赖模式不可用。

★ 顺带一提：这与「批量写漏租户条件」是**同一类问题**的另一个面——
  隔离/配额机制总有一个「没被框架覆盖到的存储层」，
  缓存 key 与限流 key 都属于这类。所以 key 一律经 `build_key()` 构造。
"""

from __future__ import annotations

import time
from uuid import uuid4

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

RATE_LIMIT_KEY_PREFIX = "ratelimit"

# 窗口长度（秒）。方案要求「每分钟 N 次」，所以窗口固定 60s。
WINDOW_SECONDS = 60


def build_key(tenant_id: int, identity: str = "global") -> str:
    """集中构造限流 key，杜绝手写漏租户前缀。

    形如 `ratelimit:tenant:1:global`。

    ★ `identity` 预留给「按接口/按用户分别限流」的场景，
      当前按租户整体限流（方案 8.4：按租户配额）。
    """
    return f"{RATE_LIMIT_KEY_PREFIX}:tenant:{tenant_id}:{identity}"


async def check_rate_limit(
    tenant_id: int,
    *,
    limit: int,
    window_seconds: int = WINDOW_SECONDS,
    identity: str = "global",
) -> tuple[bool, int]:
    """滑动窗口计数。返回 `(是否放行, 剩余额度)`。

    实现要点：
      · ZSET 的 score = 请求发生时刻（秒，浮点），member = 唯一标识
      · member 必须唯一（时刻 + 随机后缀）：同一时刻的两个请求若 member 相同，
        ZADD 会去重，导致计数偏少 → 限流失效
      · `EXPIRE` 不能省：否则 ZSET 永久驻留，被限流过的租户会留下一个
        永不消失的 key（内存泄漏）
    """
    redis = get_redis()
    now = time.time()
    key = build_key(tenant_id, identity)
    cutoff = now - window_seconds

    # 第一步（原子事务）：清掉窗口外的记录，并统计窗口内的数量
    pipe = redis.pipeline(transaction=True)
    pipe.zremrangebyscore(key, 0, cutoff)
    pipe.zcard(key)
    results = await pipe.execute()
    current = int(results[1])

    if current >= limit:
        return False, 0

    # 第二步（原子事务）：记录本次请求，并续期
    member = f"{now}:{uuid4().hex[:8]}"
    pipe = redis.pipeline(transaction=True)
    pipe.zadd(key, {member: now})
    pipe.expire(key, window_seconds)
    await pipe.execute()

    return True, limit - current - 1


async def current_usage(
    tenant_id: int, *, window_seconds: int = WINDOW_SECONDS, identity: str = "global"
) -> int:
    """查看当前窗口内已用次数（排查/展示用，不计入）。"""
    redis = get_redis()
    key = build_key(tenant_id, identity)
    cutoff = time.time() - window_seconds
    pipe = redis.pipeline(transaction=True)
    pipe.zremrangebyscore(key, 0, cutoff)
    pipe.zcard(key)
    results = await pipe.execute()
    return int(results[1])


async def reset_usage(tenant_id: int, *, identity: str = "global") -> None:
    """清空计数（测试/运维用）。"""
    await get_redis().delete(build_key(tenant_id, identity))
