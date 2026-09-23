"""限流测试（第 8 周步骤 2）。

★★ 核心是「**配额按租户独立**」（PROJECT-PLAN 风险 10）：
  限流 key 不带租户前缀的后果不是「读到别人的数据」，
  而是**一个租户把全平台的配额吃光**——A 租户刷爆配额后，
  B 租户的正常请求也被 429。这同样是隔离失效，只是表现形式不同。

★ 另一个容易漏的点：**无租户上下文的请求不该参与租户配额**。
  `/health`、登录、注册不属于任何租户，把它们算进某个租户的配额
  会让匿名接口把租户额度吃掉。

★ 超限响应的形状也要钉住：必须是 `429` + 业务码 `42900` + 统一信封。
  中间件里如果靠抛异常，这些全会丢（handler 在中间件内层接不到）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.constants import BizCode
from app.core.rate_limit import build_key, reset_usage

PASSWORD = "a-long-enough-passphrase"


@pytest.fixture(autouse=True)
def _fresh_redis():
    """每个用例用干净的限流计数。

    ★ fakeredis 是进程内单例——上个用例的计数会残留，
      下个用例可能在没发几次请求时就撞上限额（表现为「单独跑绿、一起跑红」）。
    """
    from app.core.redis import reset_client_for_tests

    reset_client_for_tests()
    yield
    reset_client_for_tests()


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def small_quota(monkeypatch):
    """把配额压到 3 次/分钟，便于快速触发。"""
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    return 3


# ======================================================================
# key 构造
# ======================================================================
def test_rate_limit_key_has_tenant_prefix():
    """★ 限流 key 必须带租户前缀，否则一个租户能吃掉全平台配额。"""
    assert build_key(7) == "ratelimit:tenant:7:global"
    assert build_key(1) != build_key(2)


# ======================================================================
# 正常放行 + 响应头
# ======================================================================
async def test_request_passes_and_reports_quota_headers(client, small_quota):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]

    resp = await client.get("/api/v1/projects", headers=_h(token))

    assert resp.status_code == 200, resp.text
    assert resp.headers["X-RateLimit-Limit"] == str(small_quota)
    assert resp.headers["X-RateLimit-Remaining"] == str(small_quota - 1)


# ======================================================================
# 超限
# ======================================================================
async def test_exceeding_quota_returns_429_with_biz_code(client, small_quota):
    """★ 超配额必须返回 429 + 业务码 42900，且信封完整。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]

    # 用满配额
    for _ in range(small_quota):
        resp = await client.get("/api/v1/projects", headers=_h(token))
        assert resp.status_code == 200, "用满配额前都应放行"

    # 第 N+1 次被拒
    resp = await client.get("/api/v1/projects", headers=_h(token))

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["code"] == BizCode.RATE_LIMITED == 42900
    assert body["request_id"], "限流响应也要带 request_id（便于从日志定位）"
    assert resp.headers["X-RateLimit-Remaining"] == "0"


# ======================================================================
# ★★ 配额按租户独立
# ======================================================================
async def test_quota_is_per_tenant(client, small_quota):
    """★★ 一个租户用尽配额，不该影响另一个租户。

    修复前的风险：限流 key 若不带租户前缀，A 刷满后 B 直接被 429。
    """
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = alice["token"]["access_token"]
    bob_token = bob["token"]["access_token"]

    # alice 用满自己的配额
    for _ in range(small_quota):
        await client.get("/api/v1/projects", headers=_h(alice_token))
    limited = await client.get("/api/v1/projects", headers=_h(alice_token))
    assert limited.status_code == 429, "alice 应已被限流"

    # bob 完全不受影响
    resp = await client.get("/api/v1/projects", headers=_h(bob_token))
    assert resp.status_code == 200, f"bob 不该被 alice 的用量影响：{resp.text}"
    assert resp.headers["X-RateLimit-Remaining"] == str(small_quota - 1)


async def test_reset_of_one_tenant_does_not_affect_another(client, small_quota):
    """★ 清一个租户的计数不该动到别的租户（key 前缀隔离的直接验证）。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = alice["token"]["access_token"]
    bob_token = bob["token"]["access_token"]

    await client.get("/api/v1/projects", headers=_h(alice_token))
    await client.get("/api/v1/projects", headers=_h(bob_token))

    # 只清 alice
    await reset_usage(alice["tenant"]["id"])

    # alice 的剩余额度回到满额，bob 的仍是被消耗过后的值
    resp_a = await client.get("/api/v1/projects", headers=_h(alice_token))
    resp_b = await client.get("/api/v1/projects", headers=_h(bob_token))
    assert resp_a.headers["X-RateLimit-Remaining"] == str(small_quota - 1)
    assert resp_b.headers["X-RateLimit-Remaining"] == str(small_quota - 2)


# ======================================================================
# 无租户上下文不参与配额
# ======================================================================
async def test_health_is_not_rate_limited(client, small_quota):
    """★ `/health` 不属于任何租户，不该被租户配额拦。

    否则监控探针会把某个租户的额度吃光（或者自己被 429 误判为不健康）。
    """
    for _ in range(small_quota + 5):
        resp = await client.get("/health")
        assert resp.status_code == 200, "健康检查不该被限流"
    assert "X-RateLimit-Limit" not in resp.headers, "无租户上下文不该带配额头"


async def test_login_is_not_rate_limited_by_tenant_quota(client, small_quota):
    """未登录请求（无租户上下文）同样不消耗租户配额。"""
    await _register(client, "guotao")
    for _ in range(small_quota + 3):
        resp = await client.post(
            "/api/v1/auth/login", json={"username": "guotao", "password": PASSWORD}
        )
        assert resp.status_code == 200, f"登录不该被租户配额拦：{resp.text}"


# ======================================================================
# 滑动窗口
# ======================================================================
async def test_window_slides_so_old_requests_expire():
    """★ 窗口会滑动：窗口外的旧记录不再计入。

    ★ 为什么直接测 `check_rate_limit` 而不走 HTTP：
      `window_seconds` 是**默认参数**，Python 在函数定义时就求值了，
      所以 monkeypatch 模块变量 `WINDOW_SECONDS` 对它无效。
      直接传参才能把窗口压到 1 秒（无需真等 60 秒）。
    """
    import asyncio

    from app.core.rate_limit import check_rate_limit

    tenant_id = 999
    kwargs = {"limit": 2, "window_seconds": 1}

    assert (await check_rate_limit(tenant_id, **kwargs))[0] is True
    assert (await check_rate_limit(tenant_id, **kwargs))[0] is True
    assert (await check_rate_limit(tenant_id, **kwargs))[0] is False, "配额用尽应拒绝"

    await asyncio.sleep(1.2)

    allowed, _remaining = await check_rate_limit(tenant_id, **kwargs)
    assert allowed is True, "窗口滑过后应重新放行（证明是滑动窗口而非永久封锁）"


async def test_limit_of_one_allows_exactly_one():
    """边界：配额 1 时只放行一次（防止 off-by-one 把配额放大）。"""
    from app.core.rate_limit import check_rate_limit

    assert (await check_rate_limit(1001, limit=1))[0] is True
    assert (await check_rate_limit(1001, limit=1))[0] is False


async def test_distinct_requests_are_counted_separately():
    """★ 同一时刻的多次请求必须各计一次。

    ZSET 的 member 若只用时间戳，同一毫秒内的请求会被 ZADD 去重，
    计数偏少 → 限流形同虚设。member 里带随机后缀正是为了避免这一点。
    """
    from app.core.rate_limit import check_rate_limit, current_usage

    for _ in range(5):
        await check_rate_limit(1002, limit=10)
    assert await current_usage(1002) == 5
