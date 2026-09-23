"""缓存与 Redis 测试（第 8 周步骤 1）。

★★ 本文件的核心是「**缓存不跨租户**」（PROJECT-PLAN 风险 10）。

  缓存是隔离机制里最容易被漏掉的一层：数据库有 NOT NULL 与查询钩子兜底，
  而缓存就是个裸 KV——**key 写错了不会报错**，只会让 B 租户读到 A 租户的数据。

  对策是结构性的：所有 key 只能经 `cache.cache_key()` 构造，
  且每个公开函数的第一个参数都是 `tenant_id`（漏了就少一个必填参数）。
  本文件把这条性质钉住。
"""

from __future__ import annotations

import pytest

from app.core import cache
from app.core.redis import get_redis, reset_client_for_tests


@pytest.fixture(autouse=True)
def _fresh_redis():
    """每个用例用干净的 fakeredis。

    ★ 必须做：fakeredis 是**进程内单例**语义，上个用例写的 key 会留到下一个，
      造成「单独跑绿、一起跑红」。重置模块级客户端即可拿到新实例。
    """
    reset_client_for_tests()
    yield
    reset_client_for_tests()


# ======================================================================
# key 构造：租户前缀
# ======================================================================
def test_cache_key_has_tenant_prefix():
    key = cache.cache_key(7, "notifications", "unread", 42)
    assert key == "cache:tenant:7:notifications:unread:42"
    assert key.startswith("cache:tenant:7:")


def test_cache_key_differs_by_tenant():
    assert cache.cache_key(1, "x") != cache.cache_key(2, "x")


# ======================================================================
# ★★ 跨租户隔离
# ======================================================================
async def test_same_key_different_tenant_does_not_collide():
    """两个租户写同名 key，必须互不可见。"""
    await cache.cache_set(1, "shared", value="tenant-1-value")
    await cache.cache_set(2, "shared", value="tenant-2-value")

    assert await cache.cache_get(1, "shared") == "tenant-1-value"
    assert await cache.cache_get(2, "shared") == "tenant-2-value"


async def test_delete_only_affects_own_tenant():
    """★ 清自己的缓存不能动到别的租户——否则一个租户能借「清缓存」打掉别人的。"""
    await cache.cache_set(1, "k", value="a")
    await cache.cache_set(2, "k", value="b")

    await cache.cache_delete(1, "k")

    assert await cache.cache_get(1, "k") is None
    assert await cache.cache_get(2, "k") == "b"


async def test_get_or_set_does_not_share_across_tenants():
    """★ 回源缓存同样不能跨租户复用。"""
    calls: list[str] = []

    def make_factory(label: str):
        async def factory() -> str:
            calls.append(label)
            return label

        return factory

    assert await cache.cache_get_or_set(1, "c", factory=make_factory("t1")) == "t1"
    assert await cache.cache_get_or_set(2, "c", factory=make_factory("t2")) == "t2"

    assert calls == ["t1", "t2"], "两个租户各回源一次（没有互相命中）"


# ======================================================================
# 基本行为
# ======================================================================
async def test_get_missing_returns_none():
    assert await cache.cache_get(1, "nope") is None


async def test_set_then_get_roundtrip_with_json_types():
    """dict / list / bool / None 都要能原样往返（JSON 序列化的意义）。"""
    payload = {"a": [1, 2, 3], "b": True, "c": None, "d": "中文"}
    await cache.cache_set(1, "complex", value=payload)
    assert await cache.cache_get(1, "complex") == payload


async def test_set_overwrites():
    await cache.cache_set(1, "k", value=1)
    await cache.cache_set(1, "k", value=2)
    assert await cache.cache_get(1, "k") == 2


async def test_get_or_set_calls_factory_only_once():
    calls: list[int] = []

    async def factory() -> dict:
        calls.append(1)
        return {"n": 42}

    first = await cache.cache_get_or_set(1, "computed", factory=factory)
    second = await cache.cache_get_or_set(1, "computed", factory=factory)

    assert first == {"n": 42}
    assert second == {"n": 42}
    assert len(calls) == 1, "第二次应命中缓存，不再回源"


async def test_corrupted_value_is_treated_as_miss():
    """★ 缓存内容坏了不该让请求失败——当成未命中，回源重建。"""
    await get_redis().set(cache.cache_key(1, "bad"), "not-json{{{")
    assert await cache.cache_get(1, "bad") is None


async def test_ttl_is_applied():
    await cache.cache_set(1, "k", value="v", ttl=60)
    ttl = await get_redis().ttl(cache.cache_key(1, "k"))
    assert 0 < ttl <= 60


# ======================================================================
# 零依赖降级
# ======================================================================
async def test_redis_client_is_usable_in_degraded_mode():
    """USE_FAKEREDIS=true 时客户端必须真的可用（否则整套功能跑不起来）。"""
    client = get_redis()
    await client.set("probe", "1")
    assert await client.get("probe") == "1"


async def test_client_is_reused_across_calls():
    """同一进程内应复用同一个客户端实例（不每次新建连接）。"""
    assert get_redis() is get_redis()
