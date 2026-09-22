"""事件触发点测试（第 4 周步骤 4）。

★ 本文件覆盖「全部事件类型的触发」，每个类型都成对写：

    [正向] 该发的时候发了，且内容正确
    [反向] 不该发的时候**没有**发

  反向断言比正向更重要：事件系统最典型的故障不是「没发」，
  而是「不该发的也发了」——自己给自己派活、自己推进自己的任务、
  重复点两次保存、把同一个角色绑两遍，都会产生噪音通知。
  噪音会让人关掉通知，等于系统失效。所以每条正向断言后面都跟一条反向。

★ 覆盖的 6 个事件类型：
    TASK_ASSIGNED       创建时分配 / 改派
    TASK_STATUS_CHANGED 状态流转
    TASK_COMMENTED      评论（在 test_comments.py）
    TASK_MENTIONED      @提及（在 test_comments.py）
    MEMBER_JOINED       成员加入
    ROLE_ASSIGNED       角色授予
"""

from __future__ import annotations

from app.core.constants import MEMBER_ROLE, DataScope
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.models import Notification
from app.services.notification_service import list_notifications

PASSWORD = "a-long-enough-passphrase"


# ----------------------------------------------------------------------
# 装置
# ----------------------------------------------------------------------
async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _notes(db_session_factory, *, tenant_id: int, user_id: int) -> list[tuple[str, dict]]:
    """取该用户的通知，返回 [(event, payload), ...]。"""
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        items, _total = await list_notifications(
            session, user_id=user_id, unread_only=False, page=1, page_size=50
        )
        return [(n.event, n.payload) for n in items]
    finally:
        restore_context(previous)
        await session.close()


async def _reset(db_session_factory, *, tenant_id: int, user_id: int) -> None:
    """清空该用户的通知，让用例只观察本次操作产生的事件。"""
    from sqlalchemy import delete

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


async def _invite(client, admin_token: str, username: str) -> int:
    """注册一个用户并加入管理员团队，返回其 user_id。"""
    await _register(client, username)
    resp = await client.post(
        "/api/v1/members", json={"username": username}, headers=_h(admin_token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["user_id"]


async def _make_project(client, token: str, code: str = "P-1") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"code": code, "name": f"项目{code}"}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _login_in(client, username: str, tenant_id: int) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD, "tenant_id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


# ======================================================================
# TASK_ASSIGNED —— 创建时分配
# ======================================================================
async def test_creating_assigned_task_notifies_assignee(client, db_session_factory):
    """[正向] 创建任务并分配 → 执行人收到 TASK_ASSIGNED，内容含任务与项目名。"""
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token)

    mate_uid = await _invite(client, token, "mate-user")
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "写接口", "assignee_id": mate_uid},
        headers=_h(token),
    )
    assert resp.status_code == 201, resp.text

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert [n[0] for n in notes] == ["task.assigned"]
    _event, payload = notes[0]
    assert payload["task_title"] == "写接口"
    assert payload["project_name"] == "项目P-1"
    assert payload["reassigned"] is False
    assert payload["assigned_by"] == admin_uid


async def test_self_assigned_task_does_not_notify(client, db_session_factory):
    """[反向] 把任务分配给自己 → 不该有任何通知。"""
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "SELF")
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=admin_uid)

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "自己的活", "assignee_id": admin_uid},
        headers=_h(token),
    )
    assert resp.status_code == 201, resp.text

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=admin_uid)
    assert notes == [], "自己分配给自己不该产生通知"


async def test_unassigned_task_does_not_notify(client, db_session_factory):
    """[反向] 不分配执行人 → 没有 TASK_ASSIGNED。"""
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "NOASSIGN")
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=admin_uid)

    await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "没人认领"}, headers=_h(token)
    )
    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=admin_uid) == []


# ======================================================================
# TASK_ASSIGNED —— 改派
# ======================================================================
async def test_reassign_notifies_new_assignee(client, db_session_factory):
    """[正向] 改派 → 新执行人收到 TASK_ASSIGNED 且 reassigned=True。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "REASSIGN")

    first_uid = await _invite(client, token, "mate-a")
    second_uid = await _invite(client, token, "mate-b")

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "转手的活", "assignee_id": first_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]

    await _reset(db_session_factory, tenant_id=tenant_id, user_id=second_uid)

    resp = await client.patch(
        f"/api/v1/tasks/{tid}", json={"assignee_id": second_uid}, headers=_h(token)
    )
    assert resp.status_code == 200, resp.text

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=second_uid)
    assert [n[0] for n in notes] == ["task.assigned"]
    assert notes[0][1]["reassigned"] is True


async def test_reassign_to_same_assignee_does_not_notify(client, db_session_factory):
    """[反向] 把 assignee_id 设成**同一个人** → 没有变化就没有事件。

    没有这个比较的话，前端「保存」按钮被点两次就会发两条改派通知。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "SAME-ASSIGNEE")
    mate_uid = await _invite(client, token, "mate-user")

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "任务", "assignee_id": mate_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    await client.patch(f"/api/v1/tasks/{tid}", json={"assignee_id": mate_uid}, headers=_h(token))

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid) == []


async def test_updating_only_priority_does_not_notify(client, db_session_factory):
    """[反向] 只改优先级 → 不产生任何事件。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "PRIO")
    mate_uid = await _invite(client, token, "mate-user")

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "任务", "assignee_id": mate_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    await client.patch(f"/api/v1/tasks/{tid}", json={"priority": "URGENT"}, headers=_h(token))

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid) == []


# ======================================================================
# TASK_STATUS_CHANGED
# ======================================================================
async def test_status_change_notifies_assignee(client, db_session_factory):
    """[正向] 管理员推进别人负责的任务 → 执行人收到 TASK_STATUS_CHANGED。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "STATUS")
    mate_uid = await _invite(client, token, "mate-user")

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "推进我", "assignee_id": mate_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    resp = await client.patch(
        f"/api/v1/tasks/{tid}/status", json={"status": "IN_PROGRESS"}, headers=_h(token)
    )
    assert resp.status_code == 200, resp.text

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert [n[0] for n in notes] == ["task.status_changed"]
    _event, payload = notes[0]
    assert payload["from_status"] == "TODO"
    assert payload["to_status"] == "IN_PROGRESS"


async def test_assignee_changing_own_status_does_not_notify(client, db_session_factory):
    """[反向] 执行人自己推进自己的任务 → 不该通知自己。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "SELF-STATUS")
    mate_uid = await _invite(client, token, "mate-user")
    mate_token = await _login_in(client, "mate-user", tenant_id)

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "自己做", "assignee_id": mate_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    resp = await client.patch(
        f"/api/v1/tasks/{tid}/status", json={"status": "IN_PROGRESS"}, headers=_h(mate_token)
    )
    assert resp.status_code == 200, resp.text

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid) == [], (
        "自己推进自己的任务不该产生通知"
    )


async def test_idempotent_status_change_does_not_notify(client, db_session_factory):
    """[反向] 状态没变（幂等调用）→ 没有变化就没有事件。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "IDEMPOTENT")
    mate_uid = await _invite(client, token, "mate-user")

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "任务", "assignee_id": mate_uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    # TODO → TODO
    resp = await client.patch(
        f"/api/v1/tasks/{tid}/status", json={"status": "TODO"}, headers=_h(token)
    )
    assert resp.status_code == 200

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid) == []


async def test_status_change_on_unassigned_task_does_not_notify(client, db_session_factory):
    """[反向] 任务没有执行人 → 状态变了也没人可通知，不该报错。"""
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "NOBODY")
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=admin_uid)

    task = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "无人任务"}, headers=_h(token)
    )
    tid = task.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/tasks/{tid}/status", json={"status": "IN_PROGRESS"}, headers=_h(token)
    )
    assert resp.status_code == 200, resp.text
    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=admin_uid) == []


# ======================================================================
# MEMBER_JOINED
# ======================================================================
async def test_member_joined_notifies_new_member(client, db_session_factory):
    """[正向] 被拉进团队 → 收到 MEMBER_JOINED，含团队名。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]

    await _register(client, "newbie-user")
    resp = await client.post("/api/v1/members", json={"username": "newbie-user"}, headers=_h(token))
    assert resp.status_code == 201, resp.text
    newbie_uid = resp.json()["data"]["user_id"]

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=newbie_uid)
    assert [n[0] for n in notes] == ["member.joined"]
    _event, payload = notes[0]
    assert payload["tenant_name"] == "admin-user 的团队"
    # 刚加入时还没有任何角色——不要为了通知好看去猜一个角色名
    assert payload["role_codes"] == []


async def test_registration_does_not_notify_the_registrant(client, db_session_factory):
    """[反向] 自己注册开通团队 → 不该收到「你已加入团队」的通知。

    注册这个动作本身就是自己发起的，给一个刚注册的人弹一条
    「你已加入团队」纯属噪音。
    """
    admin = await _register(client, "admin-user")
    tenant_id, uid = admin["tenant"]["id"], admin["user"]["id"]

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=uid) == []


# ======================================================================
# ROLE_ASSIGNED
# ======================================================================
async def _role_id(client, token: str, code: str) -> int:
    resp = await client.get("/api/v1/roles", headers=_h(token))
    return next(r["id"] for r in resp.json()["data"] if r["code"] == code)


async def test_role_assigned_notifies_member(client, db_session_factory):
    """[正向] 被授予角色 → 收到 ROLE_ASSIGNED。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    mate_uid = await _invite(client, token, "mate-user")

    members = (await client.get("/api/v1/members", headers=_h(token))).json()["data"]
    member_id = next(m["id"] for m in members if m["username"] == "mate-user")
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    role_id = await _role_id(client, token, MEMBER_ROLE)
    resp = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text

    notes = await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert [n[0] for n in notes] == ["role.assigned"]
    _event, payload = notes[0]
    assert payload["role_code"] == MEMBER_ROLE
    assert payload["data_scope"] == "SELF"


async def test_rebinding_same_role_does_not_notify(client, db_session_factory):
    """[反向] 重复绑同一角色（幂等命中）→ 不该再发一条。

    管理员手滑点两次「保存」不该给用户发两条一模一样的通知。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]
    mate_uid = await _invite(client, token, "mate-user")

    members = (await client.get("/api/v1/members", headers=_h(token))).json()["data"]
    member_id = next(m["id"] for m in members if m["username"] == "mate-user")
    role_id = await _role_id(client, token, MEMBER_ROLE)

    await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=_h(token)
    )
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    second = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=_h(token)
    )
    assert second.status_code == 201, second.text

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid) == [], (
        "幂等命中不该产生事件"
    )


async def test_admin_self_role_assignment_does_not_notify(client, db_session_factory):
    """[反向] 管理员给自己绑角色 → 不通知自己。"""
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]

    members = (await client.get("/api/v1/members", headers=_h(token))).json()["data"]
    own_member_id = next(m["id"] for m in members if m["user_id"] == admin_uid)
    await _reset(db_session_factory, tenant_id=tenant_id, user_id=admin_uid)

    role_id = await _role_id(client, token, MEMBER_ROLE)
    resp = await client.post(
        f"/api/v1/members/{own_member_id}/roles", json={"role_id": role_id}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text

    assert await _notes(db_session_factory, tenant_id=tenant_id, user_id=admin_uid) == []


# ======================================================================
# 跨租户隔离：事件也不会串
# ======================================================================
async def test_events_do_not_leak_across_tenants(client, db_session_factory):
    """事件落库与推送都按租户隔离 —— 另一个租户的人收不到任何通知。"""
    admin = await _register(client, "admin-user")
    token = admin["token"]["access_token"]
    pid = await _make_project(client, token, "ISOLATED")

    outsider = await _register(client, "outsider-user")
    outsider_uid, outsider_tenant = outsider["user"]["id"], outsider["tenant"]["id"]
    await _reset(db_session_factory, tenant_id=outsider_tenant, user_id=outsider_uid)

    # 在 admin 的租户里做一串会产生事件的操作
    await _invite(client, token, "mate-user")
    await client.post("/api/v1/tasks", json={"project_id": pid, "title": "任务"}, headers=_h(token))

    assert (
        await _notes(db_session_factory, tenant_id=outsider_tenant, user_id=outsider_uid) == []
    ), "别的租户不该收到任何通知"
