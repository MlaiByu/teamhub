"""连接管理器测试（第 4 周步骤 1·连接侧）。

★ 本文件的核心是 **风险 8 的验证**：连接必须按 (tenant_id, user_id) 索引。

  本项目的 `User` 是**全局表**——同一账号可加入多个租户。若只按 user_id
  索引，user_id=42 在租户 A 和租户 B 各有一条连接时会落进同一个桶，
  推给「租户 A 的 42」的消息就送到「租户 B 的 42」的 socket 上了。
  这是**静默跨租户泄露**：不报错，只是某天有人在 B 公司界面看到 A 公司的任务标题。

  所以 `test_same_user_id_in_two_tenants_does_not_leak` 是本文件的灵魂。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.realtime.manager import ConnectionManager

# 本文件混有同步与异步用例，故**不设全局 asyncio 标记**——
# pyproject 里 asyncio_mode = "auto"，异步函数会被自动识别；
# 给同步函数套 asyncio 标记只会产生 warning。


class FakeSocket:
    """假连接：只记下收到了什么。满足 manager 需要的 `Sendable` 协议。"""

    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.received: list[dict[str, Any]] = []
        self.fail_on_send = fail_on_send

    async def send_json(self, data: Any) -> None:
        if self.fail_on_send:
            raise RuntimeError("connection already closed")
        self.received.append(data)


MESSAGE = {"event": "task.assigned", "title": "任务「X」已分配给你"}


# ----------------------------------------------------------------------
# 连接生命周期
# ----------------------------------------------------------------------
async def test_connect_and_disconnect():
    m = ConnectionManager()
    sock = FakeSocket()

    await m.connect(tenant_id=1, user_id=2, websocket=sock)
    assert m.connection_count() == 1
    assert m.connected_keys() == {(1, 2)}

    await m.disconnect(tenant_id=1, user_id=2, websocket=sock)
    assert m.connection_count() == 0
    # 空桶必须被清掉，否则长期运行会积累空 key
    assert m.connected_keys() == set()


async def test_multiple_connections_per_user_all_receive():
    """同一用户开多个标签页 → 都该收到。"""
    m = ConnectionManager()
    a, b = FakeSocket(), FakeSocket()
    await m.connect(tenant_id=1, user_id=2, websocket=a)
    await m.connect(tenant_id=1, user_id=2, websocket=b)

    assert m.connection_count() == 2
    delivered = await m.send_to_user(tenant_id=1, user_id=2, message=MESSAGE)
    assert delivered == 2
    assert a.received == [MESSAGE] and b.received == [MESSAGE]


async def test_disconnect_only_removes_that_socket():
    m = ConnectionManager()
    a, b = FakeSocket(), FakeSocket()
    await m.connect(tenant_id=1, user_id=2, websocket=a)
    await m.connect(tenant_id=1, user_id=2, websocket=b)

    await m.disconnect(tenant_id=1, user_id=2, websocket=a)
    assert m.connection_count() == 1

    await m.send_to_user(tenant_id=1, user_id=2, message=MESSAGE)
    assert a.received == []
    assert b.received == [MESSAGE]


async def test_disconnect_unknown_connection_is_noop():
    m = ConnectionManager()
    await m.disconnect(tenant_id=9, user_id=9, websocket=FakeSocket())
    assert m.connection_count() == 0


# ----------------------------------------------------------------------
# ★ 风险 8：跨租户不串
# ----------------------------------------------------------------------
async def test_same_user_id_in_two_tenants_does_not_leak():
    """★ 同一个 user_id 在两个租户各有连接时，推送必须精确落到对应租户。

    这正是必须按 (tenant_id, user_id) 索引的原因。若退化成按 user_id 索引，
    下面第一条断言会变成「A 的连接收到 2 条」——跨租户泄露。
    """
    m = ConnectionManager()
    sock_in_tenant_a = FakeSocket()
    sock_in_tenant_b = FakeSocket()

    await m.connect(tenant_id=1, user_id=42, websocket=sock_in_tenant_a)
    await m.connect(tenant_id=2, user_id=42, websocket=sock_in_tenant_b)

    await m.send_to_user(tenant_id=1, user_id=42, message=MESSAGE)

    assert sock_in_tenant_a.received == [MESSAGE]
    assert sock_in_tenant_b.received == [], "租户 B 的连接绝不能收到租户 A 的通知"


async def test_send_to_user_misses_when_tenant_not_connected():
    """该租户下没有该用户的连接时，送达数为 0（而不是误发到别的租户）。"""
    m = ConnectionManager()
    await m.connect(tenant_id=1, user_id=42, websocket=FakeSocket())

    delivered = await m.send_to_user(tenant_id=999, user_id=42, message=MESSAGE)
    assert delivered == 0


async def test_tenant_id_is_required_keyword():
    """`tenant_id` 是必填关键字参数——签名层面就挡住「忘了传租户」。"""
    m = ConnectionManager()
    with pytest.raises(TypeError):
        await m.send_to_user(user_id=42, message=MESSAGE)  # type: ignore[call-arg]


# ----------------------------------------------------------------------
# 租户广播
# ----------------------------------------------------------------------
async def test_send_to_tenant_reaches_only_that_tenant():
    m = ConnectionManager()
    a1, a2 = FakeSocket(), FakeSocket()
    b1 = FakeSocket()
    await m.connect(tenant_id=1, user_id=10, websocket=a1)
    await m.connect(tenant_id=1, user_id=11, websocket=a2)
    await m.connect(tenant_id=2, user_id=10, websocket=b1)

    delivered = await m.send_to_tenant(tenant_id=1, message=MESSAGE)

    assert delivered == 2
    assert a1.received == [MESSAGE] and a2.received == [MESSAGE]
    assert b1.received == [], "租户广播不能越租户"


def test_connection_count_can_filter_by_tenant():
    m = ConnectionManager()
    assert m.connection_count() == 0
    assert m.connection_count(tenant_id=1) == 0


async def test_connection_count_filter_after_connect():
    m = ConnectionManager()
    await m.connect(tenant_id=1, user_id=10, websocket=FakeSocket())
    await m.connect(tenant_id=1, user_id=11, websocket=FakeSocket())
    await m.connect(tenant_id=2, user_id=10, websocket=FakeSocket())

    assert m.connection_count() == 3
    assert m.connection_count(tenant_id=1) == 2
    assert m.connection_count(tenant_id=2) == 1
    assert m.connection_count(tenant_id=3) == 0


# ----------------------------------------------------------------------
# 容错
# ----------------------------------------------------------------------
async def test_dead_connection_does_not_block_others():
    """一个卡死的客户端不能让整个租户的通知都发不出去。"""
    m = ConnectionManager()
    dead = FakeSocket(fail_on_send=True)
    alive = FakeSocket()
    await m.connect(tenant_id=1, user_id=2, websocket=dead)
    await m.connect(tenant_id=1, user_id=2, websocket=alive)

    delivered = await m.send_to_user(tenant_id=1, user_id=2, message=MESSAGE)

    assert delivered == 1, "失败的那条不计入送达数"
    assert alive.received == [MESSAGE], "存活连接必须照常收到"


async def test_dead_connection_is_cleaned_up():
    m = ConnectionManager()
    dead = FakeSocket(fail_on_send=True)
    await m.connect(tenant_id=1, user_id=2, websocket=dead)
    assert m.connection_count() == 1

    await m.send_to_user(tenant_id=1, user_id=2, message=MESSAGE)

    assert m.connection_count() == 0, "发送失败的连接应被清理，避免反复失败"


async def test_send_to_user_with_no_connections_returns_zero():
    m = ConnectionManager()
    assert await m.send_to_user(tenant_id=1, user_id=2, message=MESSAGE) == 0


async def test_close_all_clears_everything():
    m = ConnectionManager()
    await m.connect(tenant_id=1, user_id=2, websocket=FakeSocket())
    await m.connect(tenant_id=2, user_id=3, websocket=FakeSocket())

    await m.close_all()

    assert m.connection_count() == 0
    assert m.connected_keys() == set()
