"""WebSocket 端点测试（第 4 周步骤 5）。

★ 两层验证，各管一段：

  1. **本文件**：握手逻辑的确定性单测。直接调用 `_serve()` 并注入独立的
     `ConnectionManager` 与假连接——握手涉及令牌验签、租户校验、连接注册/清理，
     这些分支用真实网络跑既慢又难覆盖失败路径。

  2. **真实服务器的端到端验证**：起真的 uvicorn，用 `websockets` 客户端连上去，
     断言「REST 触发的事件真的推到了连接上」。**握手正确 ≠ 推送能通**，两者都要证。

★ 为什么测试要 new 一个 ConnectionManager 而不是用模块级单例：
  单例里的 `asyncio.Lock` 会绑定到第一个使用它的 event loop。测试与生产、
  或不同测试之间跑在不同 loop 上时，复用单例会抛「attached to a different loop」。
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.v1.ws import _serve
from app.core.security import create_access_token, create_refresh_token
from app.realtime.manager import ConnectionManager

USER_ID = 42
TENANT_ID = 7


class FakeWebSocket:
    """假连接。

    ★ `receive_text` 在消息队列空时**挂起**而不是立刻抛断连——
      否则 `_serve` 会在握手后瞬间退出，测试根本来不及断言「连接已登记」。
      真实的 WebSocket 也是这个语义：没消息就一直等着。
      要模拟客户端断开就调 `release()`。
    """

    def __init__(self, *, headers: dict[str, str] | None = None, incoming: list[str] | None = None):
        self.headers = headers or {}
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.sent: list[dict] = []
        self._incoming = list(incoming or [])
        self._release = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_json(self, data) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        await self._release.wait()
        raise WebSocketDisconnect(1000)

    def release(self) -> None:
        """模拟客户端断开。"""
        self._release.set()


class ClosingSocket(FakeWebSocket):
    """收到即断开的连接，用于特定关闭码的清理路径。"""

    def __init__(self, code: int) -> None:
        super().__init__()
        self._code = code

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(self._code)


def _access_token(*, tenant_id: int | None = TENANT_ID, user_id: int = USER_ID) -> str:
    return create_access_token(user_id=user_id, tenant_id=tenant_id)


async def _start(ws: FakeWebSocket, mgr: ConnectionManager, *, token: str | None = None):
    """启动 `_serve` 并让出一次控制权，使握手跑完、连接进入保持状态。"""
    task = asyncio.create_task(
        _serve(
            ws,
            token_param=token if token is not None else _access_token(),
            connection_manager=mgr,
        )
    )
    await asyncio.sleep(0)
    return task


# ----------------------------------------------------------------------
# 握手拒绝路径
# ----------------------------------------------------------------------
async def test_missing_token_is_rejected():
    ws = FakeWebSocket()
    await _serve(ws, token_param=None, connection_manager=ConnectionManager())

    assert ws.accepted is False, "未认证的连接不该被 accept（别先占住服务端资源）"
    assert ws.closed is not None and ws.closed[0] == 4401


async def test_garbage_token_is_rejected():
    ws = FakeWebSocket()
    await _serve(ws, token_param="not-a-jwt", connection_manager=ConnectionManager())

    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 4401


async def test_refresh_token_cannot_open_socket():
    """★ refresh token 不能用来建长连接。

    它有效期长（默认 14 天），唯一用途是换 access；
    允许它建连等于把长有效期凭据暴露在 URL 里。
    """
    refresh = create_refresh_token(user_id=USER_ID, tenant_id=TENANT_ID)[0]
    ws = FakeWebSocket()
    await _serve(ws, token_param=refresh, connection_manager=ConnectionManager())

    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 4401


async def test_token_without_tenant_is_rejected():
    """没有租户作用域的令牌无法决定连接挂在哪个租户下——必须拒绝。

    连接索引带租户是风险 8 的落地点，索引不出来就不能建连。
    """
    ws = FakeWebSocket()
    await _serve(
        ws, token_param=_access_token(tenant_id=None), connection_manager=ConnectionManager()
    )

    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 4401


# ----------------------------------------------------------------------
# 握手成功路径
# ----------------------------------------------------------------------
async def test_valid_token_connects_and_sends_ack():
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    task = await _start(ws, mgr)

    assert ws.accepted is True
    assert ws.sent[0]["event"] == "connected"
    assert ws.sent[0]["tenant_id"] == TENANT_ID
    assert ws.sent[0]["user_id"] == USER_ID

    ws.release()
    await task


async def test_connection_is_indexed_by_tenant_and_user():
    """★ 连接必须按 (tenant_id, user_id) 登记——风险 8 的落地点。"""
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    task = await _start(ws, mgr)

    assert mgr.connected_keys() == {(TENANT_ID, USER_ID)}

    ws.release()
    await task


async def test_token_via_authorization_header():
    """非浏览器客户端习惯用请求头——两种传法都要支持。"""
    mgr = ConnectionManager()
    ws = FakeWebSocket(headers={"Authorization": f"Bearer {_access_token()}"})
    task = asyncio.create_task(_serve(ws, token_param=None, connection_manager=mgr))
    await asyncio.sleep(0)

    assert ws.accepted is True
    assert mgr.connected_keys() == {(TENANT_ID, USER_ID)}

    ws.release()
    await task


async def test_disconnect_removes_connection():
    """客户端断开后必须清理，否则连接表会泄漏。"""
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    task = await _start(ws, mgr)
    assert mgr.connected_keys() == {(TENANT_ID, USER_ID)}

    ws.release()
    await task

    assert mgr.connected_keys() == set(), "断开后不该残留连接"


# ----------------------------------------------------------------------
# 推送真的能到达这条连接
# ----------------------------------------------------------------------
async def test_pushed_message_reaches_socket():
    """握手成功后，推给该 (tenant, user) 的消息必须到得了这条连接。"""
    mgr = ConnectionManager()
    ws = FakeWebSocket()
    task = await _start(ws, mgr)

    delivered = await mgr.send_to_user(
        tenant_id=TENANT_ID, user_id=USER_ID, message={"event": "task.assigned"}
    )
    assert delivered == 1
    assert {"event": "task.assigned"} in ws.sent

    ws.release()
    await task


async def test_push_does_not_reach_other_tenant_connection():
    """★ 实时推送同样不能跨租户——同一个 user_id 在两个租户各有连接时。"""
    mgr = ConnectionManager()
    ws_t1 = FakeWebSocket()
    ws_t2 = FakeWebSocket()
    t1 = await _start(ws_t1, mgr, token=_access_token(tenant_id=1))
    t2 = await _start(ws_t2, mgr, token=_access_token(tenant_id=2))

    assert mgr.connected_keys() == {(1, USER_ID), (2, USER_ID)}

    await mgr.send_to_user(tenant_id=1, user_id=USER_ID, message={"event": "only-for-t1"})

    assert any(m.get("event") == "only-for-t1" for m in ws_t1.sent)
    assert not any(m.get("event") == "only-for-t1" for m in ws_t2.sent), (
        "租户 2 的连接绝不能收到租户 1 的推送"
    )

    ws_t1.release()
    ws_t2.release()
    await t1
    await t2


@pytest.mark.parametrize("code", [1000, 1001, 1011])
async def test_abnormal_close_still_cleans_up(code):
    """异常关闭码也要走到清理分支。"""
    mgr = ConnectionManager()
    await _serve(ClosingSocket(code), token_param=_access_token(), connection_manager=mgr)

    assert mgr.connected_keys() == set()


async def test_contextvar_is_reset_after_disconnect():
    """连接退出必须复位 contextvar，否则会污染复用该任务的后续协程。"""
    from app.core.db.context import current_tenant_id

    mgr = ConnectionManager()
    ws = FakeWebSocket()
    task = await _start(ws, mgr)
    ws.release()
    await task

    assert current_tenant_id.get() is None, "WS 处理函数退出后租户上下文必须复位"
