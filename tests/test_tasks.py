"""任务接口测试（第 3 周步骤 2）。

★ 本文件最该看的一条：`test_create_task_with_cross_tenant_assignee_returns_404`。

  `assignee_id` 指向**全局** `users` 表。`User` 不带 tenant_id、
  不在租户过滤范围内，所以 `do_orm_execute` 钩子**对它无能为力**，
  数据库外键也只保证「users 里存在这个 id」。

  这是本项目里唯一一处「跨租户边界的全局表→租户表引用」——
  唯一必须人工兜底的挂载校验。若它失效，A 公司的任务能分配给 B 公司的人，
  而且对方会在自己的待办里看到这条任务（不是报错，是静默串数据）。

  对照：`project_id` 的跨租户校验是**免费**的（走守卫查询），
  但同样值得钉一条测试，因为它同样是静默失败型的问题。
"""

from __future__ import annotations

import pytest

from app.core.constants import MEMBER_ROLE, DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.security import hash_password
from app.repositories.rbac import RoleRepository, UserRoleRepository
from app.repositories.task import TaskRepository
from app.repositories.tenant import TenantMemberRepository

pytestmark = pytest.mark.asyncio

GOOD_PASSWORD = "a-long-enough-passphrase"


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_project(client, token: str, code: str = "P-1") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"code": code, "name": f"项目{code}"}, headers=token
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _invite(client, token: str, username: str) -> None:
    resp = await client.post("/api/v1/members", json={"username": username}, headers=token)
    assert resp.status_code == 201, resp.text


async def _make_loose_user(db_session_factory, username: str) -> int:
    """建一个「存在于 users 表但没加入任何租户」的用户，返回其 user_id。"""
    from app.repositories.user import UserRepository

    session = db_session_factory()
    try:
        user = UserRepository(session).create(
            username=username, password_hash=hash_password(GOOD_PASSWORD), is_active=True
        )
        await session.commit()
        return user.id
    finally:
        await session.close()


# ----------------------------------------------------------------------
# 创建
# ----------------------------------------------------------------------
async def test_create_task_without_assignee(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "写接口"}, headers=token
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["title"] == "写接口"
    assert body["status"] == "TODO"
    assert body["priority"] == "MEDIUM"
    assert body["assignee_id"] is None
    # 无人被分配时，归属退回创建者
    assert body["owner_id"] == data["user"]["id"]


async def test_create_task_with_member_assignee_sets_owner_to_assignee(client):
    """分配后 owner 必须是**被分配人**——SELF 范围才是「分配给我的任务」。"""
    admin = await _register(client, "admin-user")
    token = _auth(admin["token"]["access_token"])
    pid = await _make_project(client, token)

    # 先把 bob 加进本团队
    await client.post(
        "/api/v1/auth/register", json={"username": "bob", "password": GOOD_PASSWORD}
    )  # bob 有自己的租户，但同时也会被加入 admin 的团队
    await _invite(client, token, "bob")
    members = (await client.get("/api/v1/members", headers=token)).json()["data"]
    bob_uid = next(m["user_id"] for m in members if m["username"] == "bob")

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "分配给 bob", "assignee_id": bob_uid},
        headers=token,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["owner_id"] == bob_uid, "owner 必须跟着被分配人"


async def test_create_task_with_cross_tenant_assignee_returns_404(client, db_session_factory):
    """★ 跨租户分配必须被拦。

    `assignee_id` 指向全局 users 表，钩子覆盖不到——这条测试就是钉住
    `ensure_assignee_is_member` 真的在起作用。若失效，任务会静默分配给
    别的公司的人，而且对方能在自己的待办里看到它。
    """
    admin = await _register(client, "admin-user")
    token = _auth(admin["token"]["access_token"])
    pid = await _make_project(client, token)

    # outsider 存在于 users 表，但**没有**加入 admin 的团队
    outsider_id = await _make_loose_user(db_session_factory, "outsider")

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "偷偷分给外人", "assignee_id": outsider_id},
        headers=token,
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == 40400


async def test_create_task_with_cross_tenant_project_returns_404(client):
    """项目引用走守卫查询，跨租户天然返回 404（「免费」的挂载校验）。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_pid = await _make_project(client, _auth(alice["token"]["access_token"]), "ALICE-P")

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": alice_pid, "title": "挂到别人项目下"},
        headers=_auth(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404


async def test_create_task_ignores_client_supplied_owner(client):
    """请求体塞 owner_id / dept_id 必须无效。"""
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "伪装", "owner_id": 99999, "dept_id": 88888},
        headers=token,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["owner_id"] == data["user"]["id"]


# ----------------------------------------------------------------------
# 列表与过滤
# ----------------------------------------------------------------------
async def test_list_tasks_filters(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    pid_a = await _make_project(client, token, "PA")
    pid_b = await _make_project(client, token, "PB")

    await client.post("/api/v1/tasks", json={"project_id": pid_a, "title": "A-1"}, headers=token)
    await client.post(
        "/api/v1/tasks",
        json={"project_id": pid_a, "title": "A-2", "status": "IN_PROGRESS"},
        headers=token,
    )
    await client.post("/api/v1/tasks", json={"project_id": pid_b, "title": "B-1"}, headers=token)

    # 按项目
    r = await client.get(f"/api/v1/tasks?project_id={pid_a}", headers=token)
    assert {t["title"] for t in r.json()["data"]["items"]} == {"A-1", "A-2"}
    assert r.json()["data"]["total"] == 2

    # 按状态
    r = await client.get("/api/v1/tasks?status=IN_PROGRESS", headers=token)
    assert [t["title"] for t in r.json()["data"]["items"]] == ["A-2"]

    # 按执行人（自己）
    me = data["user"]["id"]
    r = await client.get(f"/api/v1/tasks?assignee_id={me}", headers=token)
    assert r.json()["data"]["total"] == 0, "没分配给人的任务不该出现在「我的任务」里"


async def test_list_tasks_only_own_tenant(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = _auth(alice["token"]["access_token"])
    pid = await _make_project(client, alice_token, "ALICE-T")
    await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "Alice 的任务"}, headers=alice_token
    )

    r = await client.get("/api/v1/tasks", headers=_auth(bob["token"]["access_token"]))
    assert r.json()["data"]["items"] == []


async def test_task_endpoints_require_auth(client):
    for method, url, body in [
        ("get", "/api/v1/tasks", None),
        ("post", "/api/v1/tasks", {"project_id": 1, "title": "x"}),
        ("get", "/api/v1/tasks/1", None),
        ("patch", "/api/v1/tasks/1", {"title": "x"}),
    ]:
        resp = (
            await getattr(client, method)(url, json=body)
            if body
            else await getattr(client, method)(url)
        )
        assert resp.status_code == 401, f"{method.upper()} {url} 应要求登录"


# ----------------------------------------------------------------------
# 更新与改派
# ----------------------------------------------------------------------
async def test_update_task_priority(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    pid = await _make_project(client, token)
    t = await client.post("/api/v1/tasks", json={"project_id": pid, "title": "T"}, headers=token)
    tid = t.json()["data"]["id"]

    resp = await client.patch(f"/api/v1/tasks/{tid}", json={"priority": "URGENT"}, headers=token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["priority"] == "URGENT"


async def test_reassign_task_moves_owner(client):
    """改派后数据归属必须跟着走，否则列表可见性还挂在原负责人名下。"""
    data = await _register(client, "admin-user")
    token = _auth(data["token"]["access_token"])
    pid = await _make_project(client, token)

    await client.post("/api/v1/auth/register", json={"username": "bob", "password": GOOD_PASSWORD})
    await _invite(client, token, "bob")
    members = (await client.get("/api/v1/members", headers=token)).json()["data"]
    bob_uid = next(m["user_id"] for m in members if m["username"] == "bob")

    t = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "待改派"}, headers=token
    )
    tid = t.json()["data"]["id"]
    assert t.json()["data"]["owner_id"] == data["user"]["id"]

    resp = await client.patch(f"/api/v1/tasks/{tid}", json={"assignee_id": bob_uid}, headers=token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["owner_id"] == bob_uid, "改派后 owner 必须变成新执行人"


async def test_reassign_to_non_member_returns_404(client, db_session_factory):
    data = await _register(client, "admin-user")
    token = _auth(data["token"]["access_token"])
    pid = await _make_project(client, token)
    t = await client.post("/api/v1/tasks", json={"project_id": pid, "title": "T"}, headers=token)
    tid = t.json()["data"]["id"]

    outsider_id = await _make_loose_user(db_session_factory, "outsider2")
    resp = await client.patch(
        f"/api/v1/tasks/{tid}", json={"assignee_id": outsider_id}, headers=token
    )
    assert resp.status_code == 404, "改派给非本团队成员也必须被拦"


async def test_update_cross_tenant_task_returns_404(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = _auth(alice["token"]["access_token"])
    pid = await _make_project(client, alice_token, "A-TASK")
    t = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "Alice 的任务"}, headers=alice_token
    )
    tid = t.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/tasks/{tid}",
        json={"title": "被篡改"},
        headers=_auth(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# 数据范围在任务上的表现
# ----------------------------------------------------------------------
async def test_task_self_scope_shows_only_assigned_to_me(client, db_session_factory):
    """SELF 范围在任务上的语义：「分配给我的」而不是「我创建的」。

    ★ 这正是 owner_id 取被分配人（而非创建者）的原因。若取创建者，
      被管理员分配任务的普通成员会看不到自己的任务。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = _auth(admin["token"]["access_token"])
    pid = await _make_project(client, admin_token)

    # 造一个只有 MEMBER 角色（SELF 范围）的成员
    session = db_session_factory()
    previous = snapshot_context()
    try:
        from app.repositories.user import UserRepository

        member = UserRepository(session).create(
            username="member-x", password_hash=hash_password(GOOD_PASSWORD), is_active=True
        )
        await session.flush()
        member_uid = member.id
        set_context(
            RequestContext(tenant_id=tenant_id, user_id=member_uid, data_scope=DataScope.ALL)
        )
        TenantMemberRepository(session).create(user_id=member_uid, status=str(MemberStatus.ACTIVE))
        role = await RoleRepository(session).get_by_code(MEMBER_ROLE)
        await UserRoleRepository(session).bind(user_id=member_uid, role_id=role.id)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "member-x", "password": GOOD_PASSWORD, "tenant_id": tenant_id},
    )
    member_token = _auth(login.json()["data"]["access_token"])

    # 一条分给 member，一条留给 admin
    await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "给 member 的", "assignee_id": member_uid},
        headers=admin_token,
    )
    await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "admin 自己的"}, headers=admin_token
    )

    r = await client.get("/api/v1/tasks", headers=member_token)
    titles = {t["title"] for t in r.json()["data"]["items"]}
    assert titles == {"给 member 的"}, f"SELF 只看分配给我的，实际 {titles}"


async def test_task_cross_tenant_detail_returns_404(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = _auth(alice["token"]["access_token"])
    pid = await _make_project(client, alice_token, "A-DET")
    t = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "T"}, headers=alice_token
    )
    tid = t.json()["data"]["id"]

    resp = await client.get(f"/api/v1/tasks/{tid}", headers=_auth(bob["token"]["access_token"]))
    assert resp.status_code == 404


async def test_task_repository_direct_seed_is_isolated(db_session_factory):
    """顺带确认仓储层：无租户上下文时守卫拒绝。"""
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=1, data_scope=DataScope.ALL))
        repo = TaskRepository(session)
        set_context(RequestContext(tenant_id=None))
        from app.core.exceptions import TenantContextMissingError

        with pytest.raises(TenantContextMissingError):
            await repo.list_all()
    finally:
        restore_context(previous)
        await session.close()


# ----------------------------------------------------------------------
# 状态流转（PATCH /tasks/{id}/status）
# ----------------------------------------------------------------------
async def _create_task_of(client, raw_token: str, title: str = "T") -> int:
    """用**原始 token 字符串**建一个项目+任务，返回任务 id。

    注意 `_make_project` 收的是 auth 头字典，所以这里统一 `_auth()` 一下——
    之前把原始字符串直接当 headers 传，httpx 会去迭代字符串找键值对而报
    `not enough values to unpack`。
    """
    pid = await _make_project(client, _auth(raw_token), code=f"SP-{title[:6]}")
    resp = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": title}, headers=_auth(raw_token)
    )
    return resp.json()["data"]["id"]


async def _set_status(client, *, token: str, task_id: int, target: str):
    return await client.patch(
        f"/api/v1/tasks/{task_id}/status", json={"status": target}, headers=_auth(token)
    )


async def test_valid_status_transitions_chain(client):
    """合法链路：TODO → IN_PROGRESS → REVIEW → DONE 必须逐级可走。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tid = await _create_task_of(client, token, "流转链路")

    for target in ["IN_PROGRESS", "REVIEW", "DONE"]:
        resp = await _set_status(client, token=token, task_id=tid, target=target)
        assert resp.status_code == 200, f"{target}: {resp.text}"
        assert resp.json()["data"]["status"] == target


async def test_status_can_fall_back_to_todo(client):
    """IN_PROGRESS → TODO 是允许的（任务被打回）。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tid = await _create_task_of(client, token)
    await _set_status(client, token=token, task_id=tid, target="IN_PROGRESS")

    resp = await _set_status(client, token=token, task_id=tid, target="TODO")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "TODO"


async def test_status_can_be_cancelled_from_active_states(client):
    """TODO / IN_PROGRESS 都可以直接取消。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]

    tid1 = await _create_task_of(client, token, "取消1")
    r1 = await _set_status(client, token=token, task_id=tid1, target="CANCELLED")
    assert r1.status_code == 200, r1.text

    tid2 = await _create_task_of(client, token, "取消2")
    await _set_status(client, token=token, task_id=tid2, target="IN_PROGRESS")
    r2 = await _set_status(client, token=token, task_id=tid2, target="CANCELLED")
    assert r2.status_code == 200, r2.text


@pytest.mark.parametrize(
    ("path", "blocked_target", "why"),
    [
        (["IN_PROGRESS", "REVIEW", "DONE"], "IN_PROGRESS", "DONE 是终态"),
        (["CANCELLED"], "TODO", "CANCELLED 是终态"),
        ([], "DONE", "不能从 TODO 直接跳到 DONE"),
        ([], "REVIEW", "不能从 TODO 直接跳到 REVIEW"),
        (["IN_PROGRESS"], "DONE", "不能从 IN_PROGRESS 直接跳到 DONE"),
    ],
)
async def test_illegal_status_transitions_rejected(client, path, blocked_target, why):
    """非法跳转返回 409（业务冲突），不是 422——请求格式没问题，是状态不允许。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tid = await _create_task_of(client, token, "非法流转")

    for step in path:
        r = await _set_status(client, token=token, task_id=tid, target=step)
        assert r.status_code == 200, f"前置 {step} 应成功：{r.text}"

    resp = await _set_status(client, token=token, task_id=tid, target=blocked_target)
    assert resp.status_code == 409, f"{why}：应 409，实际 {resp.status_code}"
    assert resp.json()["code"] == 40900


async def test_status_transition_is_idempotent(client):
    """目标状态与当前相同时直接返回，不报错。"""
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tid = await _create_task_of(client, token)

    resp = await _set_status(client, token=token, task_id=tid, target="TODO")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "TODO"


async def test_status_rejects_unknown_value(client):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tid = await _create_task_of(client, token)

    resp = await _set_status(client, token=token, task_id=tid, target="NOT_A_STATUS")
    assert resp.status_code == 422


async def test_status_requires_auth(client):
    resp = await client.patch("/api/v1/tasks/1/status", json={"status": "IN_PROGRESS"})
    assert resp.status_code == 401


async def test_status_cross_tenant_returns_404(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    alice_token = alice["token"]["access_token"]
    tid = await _create_task_of(client, alice_token, "Alice 的任务")

    resp = await _set_status(
        client, token=bob["token"]["access_token"], task_id=tid, target="IN_PROGRESS"
    )
    assert resp.status_code == 404, "跨租户改状态必须 404"
