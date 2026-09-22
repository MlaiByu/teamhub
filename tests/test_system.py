"""第 1 周系统级测试：应用能起来、/health 通、错误信封统一。

对应第 1 周验收标准：「项目能起来，/health 通，CI 雏形」。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "healthy"


async def test_request_id_header_present(client):
    """每个响应都带 X-Request-ID，方便排查（11.4）。"""
    resp = await client.get("/health")
    assert resp.headers.get("X-Request-ID")


async def test_request_id_is_reused_from_upstream(client):
    """上游传来的 request_id 必须复用，才能跨服务串联。"""
    resp = await client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert resp.headers["X-Request-ID"] == "trace-abc"


async def test_context_endpoint_shows_isolation_off_without_token(client):
    """不带 token 时隔离未激活——这是预期行为，不是 bug。

    诊断接口的价值就在这里：手工就能看到「钩子当前在不在工作」，
    不用写代码猜。
    """
    resp = await client.get("/api/v1/system/context")
    data = resp.json()["data"]
    assert data["tenant_id"] is None
    assert data["isolation_active"] is False


async def test_unknown_route_returns_unified_envelope(client):
    """404 也要走统一响应信封，不能吐 FastAPI 默认的 {"detail": ...}。"""
    resp = await client.get("/api/v1/not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == 40400
    assert "request_id" in body
