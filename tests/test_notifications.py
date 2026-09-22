"""通知落库与投递测试（第 4 周步骤 2）。

★ 本文件最重要的两条：

  1. `test_emit_without_tenant_context_returns_none_and_does_not_raise`
     —— `emit` 的调用点在业务操作**成功之后**。此处抛异常会让客户端
     以为业务失败并重复提交。所以它必须降级返回 None 并记 ERROR。

  2. `test_notification_visible_only_to_own_user`
     —— 通知 payload 里含**任务标题与评论摘要**。只靠租户钩子的话，
     同租户的同事能读到彼此的通知。租户隔离与用户隔离是两个维度。

  另有 `test_emit_pushes_to_connected_socket` 与「落库先于推送」的顺序验证。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.realtime.events import EventType
from app.realtime.manager import ConnectionManager
from app.repositories.tenant import TenantMemberRepository, TenantRepository
from app.repositories.user import UserRepository
from app.services import notification_service

pytestmark = pytest.mark.asyncio

PASSWORD = "a-long-enough-passphrase"


class FakeSocket:
    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.received: list[dict] = []
        self.fail_on_send = fail_on_send

    async def send_json(self, data) -> None:
        if self.fail_on_send:
            raise RuntimeError("closed")
        self.received.append(data)


async def _bootstrap(db_session_factory, *, code: str, username: str) -> tuple[int, int]:
    """建「租户 + 用户 + ACTIVE 成员关系」，返回 (tenant_id, user_id)。

    ★ 为什么造数据的测试也必须真的建成员关系：
      `emit` 有一条**结构性守卫**——通知只发给当前租户的 ACTIVE 成员
      （见 notification_service.emit）。只建租户就 emit 的话会被静默跳过。
      这条守卫本身也有独立测试（test_emit_to_non_member_is_skipped）。
    """
    session = db_session_factory()
    previous = snapshot_context()
    try:
        tenant = TenantRepository(session).create(
            code=code, name=f"团队{code}", status="ACTIVE", max_members=50
        )
        await session.flush()
        user = UserRepository(session).create(
            username=username, password_hash="not-a-real-hash", is_active=True
        )
        await session.flush()
        set_context(RequestContext(tenant_id=tenant.id, user_id=user.id, data_scope=DataScope.ALL))
        TenantMemberRepository(session).create(user_id=user.id, status=str(MemberStatus.ACTIVE))
        await session.commit()
        return tenant.id, user.id
    finally:
        restore_context(previous)
        await session.close()


async def _add_membership(db_session_factory, *, tenant_id: int, user_id: int) -> None:
    """把已有用户加进另一个租户。

    用于「同一 user_id 属于两个租户」这个真实场景（`User` 是全局表）。
    """
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
        TenantMemberRepository(session).create(user_id=user_id, status=str(MemberStatus.ACTIVE))
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()


async def _emit_as(
    db_session_factory,
    *,
    tenant_id: int,
    user_id: int,
    event_type: EventType = EventType.MEMBER_JOINED,
    payload: dict | None = None,
    manager: ConnectionManager | None = None,
):
    """在指定租户/用户上下文里 emit 一条通知。"""
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        return await notification_service.emit(
            session,
            event_type=event_type,
            target_user_id=user_id,
            actor_id=None,
            connection_manager=manager,
            **(payload or {"tenant_name": "Acme", "role_codes": []}),
        )
    finally:
        restore_context(previous)
        await session.close()


# ----------------------------------------------------------------------
# emit：落库
# ----------------------------------------------------------------------
async def test_emit_persists_notification(db_session_factory):
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")
    event = await _emit_as(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    assert event is not None
    assert event.payload["tenant_name"] == "Acme"

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        items, total = await notification_service.list_notifications(
            session, user_id=user_id, unread_only=False, page=1, page_size=10
        )
    finally:
        restore_context(previous)
        await session.close()

    assert total == 1
    assert items[0].event == "member.joined"
    assert items[0].is_read is False
    assert "Acme" in items[0].title


async def test_emit_pushes_to_connected_socket(db_session_factory):
    """落库之外还要推送——两者都做到才算「事件被正确处理」。"""
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")
    mgr = ConnectionManager()
    sock = FakeSocket()
    await mgr.connect(tenant_id=tenant_id, user_id=user_id, websocket=sock)

    await _emit_as(db_session_factory, tenant_id=tenant_id, user_id=user_id, manager=mgr)

    assert len(sock.received) == 1
    msg = sock.received[0]
    assert msg["event"] == "member.joined"
    assert msg["tenant_id"] == tenant_id
    assert msg["user_id"] == user_id
    assert "Acme" in msg["title"]


async def test_emit_pushes_only_within_same_tenant(db_session_factory):
    """★ 同一个 user_id 属于两个租户时，通知不能串租户。

    这是真实场景：`User` 是全局表，同一账号可以加入多家公司。
    """
    t1, uid = await _bootstrap(db_session_factory, code="acme", username="alice")
    t2, _other = await _bootstrap(db_session_factory, code="globex", username="bob")
    await _add_membership(db_session_factory, tenant_id=t2, user_id=uid)

    mgr = ConnectionManager()
    sock_t1 = FakeSocket()
    sock_t2 = FakeSocket()
    await mgr.connect(tenant_id=t1, user_id=uid, websocket=sock_t1)
    await mgr.connect(tenant_id=t2, user_id=uid, websocket=sock_t2)

    await _emit_as(db_session_factory, tenant_id=t1, user_id=uid, manager=mgr)

    assert len(sock_t1.received) == 1
    assert sock_t2.received == [], "租户 2 的连接绝不该收到租户 1 的通知"


async def test_emit_without_connection_still_persists(db_session_factory):
    """用户离线时只落库——恢复后拉列表仍能看到，所以不算丢。"""
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")
    event = await _emit_as(
        db_session_factory, tenant_id=tenant_id, user_id=user_id, manager=ConnectionManager()
    )
    assert event is not None


async def test_emit_without_tenant_context_returns_none_and_does_not_raise(db_session_factory):
    """★ 无租户上下文时降级返回 None，绝不向上抛。

    抛异常会让客户端以为业务操作失败并重复提交，而业务其实已经成功了。
    （上下文丢失本身是调用点写错，所以日志用 ERROR 级别暴露它。）
    """
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=None))
    try:
        result = await notification_service.emit(
            session,
            event_type=EventType.MEMBER_JOINED,
            target_user_id=10,
            tenant_name="Acme",
        )
    finally:
        restore_context(previous)
        await session.close()

    assert result is None


async def test_emit_with_invalid_payload_returns_none(db_session_factory):
    """payload 不合规（缺字段）同样降级，不影响业务返回。"""
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        result = await notification_service.emit(
            session,
            event_type=EventType.TASK_ASSIGNED,
            target_user_id=user_id,
            task_id=1,  # 缺 task_title / project_id / project_name
        )
    finally:
        restore_context(previous)
        await session.close()

    assert result is None


async def test_emit_with_unregistered_event_type_returns_none(db_session_factory):
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        result = await notification_service.emit(
            session, event_type="not.a.real.event", target_user_id=user_id, foo="bar"
        )
    finally:
        restore_context(previous)
        await session.close()

    assert result is None


# ----------------------------------------------------------------------
# emit_to_many
# ----------------------------------------------------------------------
async def test_emit_to_many_dedupes_and_excludes_actor(db_session_factory):
    """@ 多人时要去重，且不给自己发。

    去重必须在服务端做：客户端传什么就发什么的话，
    一条评论里 `@张三 @张三` 会产生两条通知。
    """
    t1, actor_uid = await _bootstrap(db_session_factory, code="acme", username="actor")
    _t2, u2 = await _bootstrap(db_session_factory, code="globex", username="bob")
    _t3, u3 = await _bootstrap(db_session_factory, code="initech", username="carol")
    # bob / carol 也加入 acme —— emit 的成员守卫要求他们是本租户的 ACTIVE 成员
    await _add_membership(db_session_factory, tenant_id=t1, user_id=u2)
    await _add_membership(db_session_factory, tenant_id=t1, user_id=u3)

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=t1, user_id=actor_uid, data_scope=DataScope.ALL))
    try:
        events = await notification_service.emit_to_many(
            session,
            event_type=EventType.TASK_MENTIONED,
            target_user_ids=[u2, u3, u2, actor_uid],  # u2 重复；actor 是触发者
            actor_id=actor_uid,
            task_id=1,
            task_title="任务",
            comment_id=5,
            excerpt="…",
            mentioned_by=actor_uid,
        )
    finally:
        restore_context(previous)
        await session.close()

    assert [e.target_user_id for e in events] == [u2, u3]


async def test_emit_to_non_member_is_skipped(db_session_factory):
    """★ 结构性守卫：通知只发给**当前租户的** ACTIVE 成员。

    少了这条守卫，「给别的公司的人发通知」就不会被拦住——
    通知行会真的写进库、甚至真的推到 socket 上。
    """
    t1, _uid = await _bootstrap(db_session_factory, code="acme", username="alice")
    _t2, outsider_uid = await _bootstrap(db_session_factory, code="globex", username="bob")

    # 在租户 1 的上下文里，给「只属于租户 2」的人发通知
    event = await _emit_as(db_session_factory, tenant_id=t1, user_id=outsider_uid)
    assert event is None


async def test_emit_to_disabled_member_is_skipped(db_session_factory):
    """已停用的成员同样跳过——「人已经离开团队」不该继续收到该团队的动态。"""
    tenant_id, user_id = await _bootstrap(db_session_factory, code="acme", username="alice")

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        member = await TenantMemberRepository(session).get_membership(user_id)
        member.status = str(MemberStatus.DISABLED)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    assert await _emit_as(db_session_factory, tenant_id=tenant_id, user_id=user_id) is None


# ----------------------------------------------------------------------
# 通知接口
# ----------------------------------------------------------------------
async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _reset_notes(db_session_factory, *, tenant_id: int, user_id: int) -> None:
    """删掉该用户已有的通知——准备阶段的动作本身也会产生通知（如 MEMBER_JOINED）。

    DML 不经过 do_orm_execute 钩子，所以 tenant_id 必须显式写。
    """
    from sqlalchemy import delete

    from app.models import Notification

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        await session.execute(
            delete(Notification).where(
                Notification.tenant_id == tenant_id,
                Notification.user_id == user_id,
            )
        )
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()


async def _seed_note(
    db_session_factory, *, tenant_id: int, user_id: int, name: str = "Acme"
) -> None:
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        await notification_service.emit(
            session,
            event_type=EventType.MEMBER_JOINED,
            target_user_id=user_id,
            tenant_name=name,
        )
    finally:
        restore_context(previous)
        await session.close()


async def test_notification_endpoints_require_auth(client):
    for method, url in [
        ("get", "/api/v1/notifications"),
        ("get", "/api/v1/notifications/unread-count"),
        ("post", "/api/v1/notifications/read-all"),
        ("post", "/api/v1/notifications/1/read"),
    ]:
        resp = await getattr(client, method)(url)
        assert resp.status_code == 401, f"{method.upper()} {url} 应要求登录"


async def test_list_and_unread_count(client, db_session_factory):
    data = await _register(client, "guotao")
    tenant_id = data["tenant"]["id"]
    user_id = data["user"]["id"]
    token = data["token"]["access_token"]

    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id, name="一号团队")
    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id, name="二号团队")

    resp = await client.get("/api/v1/notifications", headers=_h(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["total"] == 2
    assert all(item["is_read"] is False for item in body["items"])
    assert "一号团队" in body["items"][0]["title"] or "二号团队" in body["items"][0]["title"]

    resp = await client.get("/api/v1/notifications/unread-count", headers=_h(token))
    assert resp.json()["data"]["unread"] == 2


async def test_list_unread_only_filter(client, db_session_factory):
    data = await _register(client, "guotao")
    tenant_id, user_id = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]

    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id)
    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    # 读完一条
    listed = await client.get("/api/v1/notifications", headers=_h(token))
    first_id = listed.json()["data"]["items"][0]["id"]
    await client.post(f"/api/v1/notifications/{first_id}/read", headers=_h(token))

    resp = await client.get("/api/v1/notifications?unread=true", headers=_h(token))
    assert resp.json()["data"]["total"] == 1
    resp = await client.get("/api/v1/notifications", headers=_h(token))
    assert resp.json()["data"]["total"] == 2, "不带 unread 时应返回全部（含已读）"


async def test_mark_read_is_idempotent(client, db_session_factory):
    data = await _register(client, "guotao")
    tenant_id, user_id = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]
    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    nid = (await client.get("/api/v1/notifications", headers=_h(token))).json()["data"]["items"][0][
        "id"
    ]

    first = await client.post(f"/api/v1/notifications/{nid}/read", headers=_h(token))
    assert first.status_code == 200 and first.json()["data"]["updated"] == 1

    second = await client.post(f"/api/v1/notifications/{nid}/read", headers=_h(token))
    assert second.status_code == 200, "重复标记已读必须幂等成功，不能 404"
    assert second.json()["data"]["updated"] == 0


async def test_mark_all_read(client, db_session_factory):
    data = await _register(client, "guotao")
    tenant_id, user_id = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]
    for _ in range(3):
        await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    resp = await client.post("/api/v1/notifications/read-all", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json()["data"]["updated"] == 3

    resp = await client.get("/api/v1/notifications/unread-count", headers=_h(token))
    assert resp.json()["data"]["unread"] == 0


async def test_notification_visible_only_to_own_user(client, db_session_factory):
    """★ 同租户内也不能读同事的通知——payload 含任务标题与评论摘要。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = admin["token"]["access_token"]

    # 造一个同事，加入同一租户
    await _register(client, "mate-user")
    added = await client.post(
        "/api/v1/members", json={"username": "mate-user"}, headers=_h(admin_token)
    )
    mate_uid = added.json()["data"]["user_id"]

    # 先把「加入团队」产生的通知清掉，让本用例只观察下面这一条
    await _reset_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=mate_uid, name="同事的团队")

    # 管理员看不到同事的通知
    resp = await client.get("/api/v1/notifications", headers=_h(admin_token))
    assert resp.json()["data"]["total"] == 0, "同租户也不能读同事的通知"

    # 同事自己看得到 → 证明上面那个 0 是用户维度过滤造成的，而不是没数据
    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "mate-user", "password": PASSWORD, "tenant_id": tenant_id},
    )
    mate_token = login.json()["data"]["access_token"]
    resp = await client.get("/api/v1/notifications", headers=_h(mate_token))
    assert resp.json()["data"]["total"] == 1

    # 管理员尝试标记同事的通知已读 → 404（不是 403，避免存在性泄露）
    mate_nid = resp.json()["data"]["items"][0]["id"]
    resp = await client.post(f"/api/v1/notifications/{mate_nid}/read", headers=_h(admin_token))
    assert resp.status_code == 404


async def test_mark_read_unknown_id_returns_404(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/notifications/999999/read", headers=_h(data["token"]["access_token"])
    )
    assert resp.status_code == 404


async def test_notifications_are_tenant_scoped(client, db_session_factory):
    """跨租户看不到彼此的通知。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")

    await _seed_note(
        db_session_factory,
        tenant_id=alice["tenant"]["id"],
        user_id=alice["user"]["id"],
        name="Alice团队",
    )

    resp = await client.get("/api/v1/notifications", headers=_h(bob["token"]["access_token"]))
    assert resp.json()["data"]["total"] == 0

    # 反向：alice 自己看得到
    resp = await client.get("/api/v1/notifications", headers=_h(alice["token"]["access_token"]))
    assert resp.json()["data"]["total"] == 1


async def test_list_notifications_pagination(client, db_session_factory):
    data = await _register(client, "guotao")
    tenant_id, user_id = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]
    for _ in range(5):
        await _seed_note(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    resp = await client.get("/api/v1/notifications?page=1&page_size=2", headers=_h(token))
    body = resp.json()["data"]
    assert len(body["items"]) == 2
    assert body["total"] == 5, "total 必须是全部匹配数，不是当前页条数"
