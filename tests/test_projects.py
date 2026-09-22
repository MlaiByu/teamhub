"""项目接口测试（第 3 周步骤 1）。

★ 本文件第一次在**真实接口**上验证数据范围过滤（`DataScopedMixin`）。
  在此之前只测过 `widest_scope` 这种纯函数与仓储层行为；
  `Project` 是第一个同时带「租户过滤 + 数据范围过滤」且有 HTTP 接口的实体。

★ 另一个重点：`owner_id` / `dept_id` 必须由服务端推导。
  测试里显式尝试用请求体覆盖它们，断言不生效——否则成员可以把项目
  「种」进别人的数据范围，数据权限形同虚设。
"""

from __future__ import annotations

import pytest

from app.core.constants import DEPT_MANAGER_ROLE, MEMBER_ROLE, DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.security import hash_password
from app.repositories.project import ProjectRepository
from app.repositories.rbac import RoleRepository, UserRoleRepository
from app.repositories.tenant import TenantMemberRepository
from app.repositories.user import UserRepository

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


async def _add_member_with_role(
    client,
    db_session_factory,
    *,
    tenant_id: int,
    username: str,
    role_code: str,
    user_id: int | None = None,
    dept_id: int | None = None,
) -> tuple[str, int]:
    """在当前租户建一个用户、绑指定角色，返回 (access_token, user_id)。"""
    session = db_session_factory()
    previous = snapshot_context()
    try:
        if user_id is None:
            user = UserRepository(session).create(
                username=username, password_hash=hash_password(GOOD_PASSWORD), is_active=True
            )
            await session.flush()
            user_id = user.id
        set_context(
            RequestContext(
                tenant_id=tenant_id, user_id=user_id, dept_id=dept_id, data_scope=DataScope.ALL
            )
        )
        TenantMemberRepository(session).create(
            user_id=user_id, status=str(MemberStatus.ACTIVE), dept_id=dept_id
        )
        role = await RoleRepository(session).get_by_code(role_code)
        await UserRoleRepository(session).bind(user_id=user_id, role_id=role.id)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": GOOD_PASSWORD, "tenant_id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"], user_id


async def _seed_projects(
    db_session_factory, *, tenant_id: int, specs: list[tuple[str, int, int | None]]
) -> list[int]:
    """后台直接种项目（(code, owner_id, dept_id) 列表），返回 id 列表。"""
    session = db_session_factory()
    previous = snapshot_context()
    ids: list[int] = []
    try:
        set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
        repo = ProjectRepository(session)
        for code, owner_id, dept_id in specs:
            p = repo.create(
                code=code, name=f"项目{code}", status="ACTIVE", owner_id=owner_id, dept_id=dept_id
            )
            await session.flush()
            ids.append(p.id)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()
    return ids


# ----------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------
async def test_create_project(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/projects",
        json={"code": "PROJ-1", "name": "协作平台重构", "description": "重构任务模块"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["code"] == "PROJ-1"
    assert body["status"] == "PLANNING"
    # owner_id 必须是创建者自己
    assert body["owner_id"] == data["user"]["id"]


async def test_create_project_ignores_client_supplied_owner_and_dept(client):
    """请求体里塞 owner_id / dept_id 必须无效——否则能把数据种进别人的范围。"""
    admin = await _register(client, "alice")
    victim = await _register(client, "bob")  # 另一个租户，其 user_id 用于伪装

    resp = await client.post(
        "/api/v1/projects",
        json={
            "code": "SNEAK",
            "name": "伪装归属",
            "owner_id": victim["user"]["id"],
            "dept_id": 99999,
        },
        headers=_auth(admin["token"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["owner_id"] == admin["user"]["id"], "owner_id 必须来自当前身份，不能被请求体覆盖"
    assert body["dept_id"] != 99999, "dept_id 必须来自当前成员关系，不能被请求体覆盖"


async def test_create_project_duplicate_code_conflicts(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    payload = {"code": "DUP", "name": "重名"}
    await client.post("/api/v1/projects", json=payload, headers=token)

    resp = await client.post("/api/v1/projects", json=payload, headers=token)
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


async def test_same_code_allowed_in_different_tenants(client):
    """同一项目编码在两个租户下可共存（唯一约束含 tenant_id）。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    payload = {"code": "SAME", "name": "同名项目"}

    r1 = await client.post(
        "/api/v1/projects", json=payload, headers=_auth(alice["token"]["access_token"])
    )
    r2 = await client.post(
        "/api/v1/projects", json=payload, headers=_auth(bob["token"]["access_token"])
    )
    assert r1.status_code == r2.status_code == 201


async def test_get_project_detail(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    created = await client.post(
        "/api/v1/projects", json={"code": "P1", "name": "项目一"}, headers=token
    )
    pid = created.json()["data"]["id"]

    resp = await client.get(f"/api/v1/projects/{pid}", headers=token)
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == pid


async def test_get_cross_tenant_project_returns_404(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    created = await client.post(
        "/api/v1/projects",
        json={"code": "A-1", "name": "Alice 的项目"},
        headers=_auth(alice["token"]["access_token"]),
    )
    pid = created.json()["data"]["id"]

    resp = await client.get(f"/api/v1/projects/{pid}", headers=_auth(bob["token"]["access_token"]))
    assert resp.status_code == 404, "跨租户取详情必须 404（不是 403，避免存在性泄露）"


async def test_update_project_partial(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    created = await client.post(
        "/api/v1/projects", json={"code": "U1", "name": "旧名"}, headers=token
    )
    pid = created.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/projects/{pid}", json={"name": "新名", "status": "ACTIVE"}, headers=token
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["name"] == "新名"
    assert body["status"] == "ACTIVE"
    assert body["code"] == "U1", "未传的字段不应被改动"


async def test_update_project_can_clear_description(client):
    """显式传 null 要能清空字段——这正是 exclude_unset 的意义。"""
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    created = await client.post(
        "/api/v1/projects",
        json={"code": "U2", "name": "带描述", "description": "原描述"},
        headers=token,
    )
    pid = created.json()["data"]["id"]

    resp = await client.patch(f"/api/v1/projects/{pid}", json={"description": None}, headers=token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["description"] is None


async def test_update_cross_tenant_project_returns_404(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    created = await client.post(
        "/api/v1/projects",
        json={"code": "A-2", "name": "Alice 的"},
        headers=_auth(alice["token"]["access_token"]),
    )
    pid = created.json()["data"]["id"]

    resp = await client.patch(
        f"/api/v1/projects/{pid}",
        json={"name": "被篡改"},
        headers=_auth(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404


async def test_project_endpoints_require_auth(client):
    for method, url, body in [
        ("get", "/api/v1/projects", None),
        ("post", "/api/v1/projects", {"code": "X", "name": "X"}),
        ("get", "/api/v1/projects/1", None),
        ("patch", "/api/v1/projects/1", {"name": "X"}),
    ]:
        resp = (
            await getattr(client, method)(url, json=body)
            if body
            else await getattr(client, method)(url)
        )
        assert resp.status_code == 401, (
            f"{method.upper()} {url} 应要求登录，实际 {resp.status_code}"
        )


# ----------------------------------------------------------------------
# 分页与过滤
# ----------------------------------------------------------------------
async def test_list_projects_paginated(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    for i in range(5):
        await client.post(
            "/api/v1/projects", json={"code": f"PG-{i}", "name": f"项目{i}"}, headers=token
        )

    resp = await client.get("/api/v1/projects?page=1&page_size=2", headers=token)
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert len(body["items"]) == 2
    assert body["total"] == 5, "total 必须反映全部匹配数，而不是当前页条数"
    assert body["page"] == 1 and body["page_size"] == 2


async def test_list_projects_filter_by_status(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    await client.post("/api/v1/projects", json={"code": "S-1", "name": "规划中"}, headers=token)
    await client.post(
        "/api/v1/projects",
        json={"code": "S-2", "name": "进行中", "status": "ACTIVE"},
        headers=token,
    )

    resp = await client.get("/api/v1/projects?status=ACTIVE", headers=token)
    items = resp.json()["data"]["items"]
    assert [p["code"] for p in items] == ["S-2"]


async def test_list_projects_only_own_tenant(client):
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    await client.post(
        "/api/v1/projects",
        json={"code": "ALICE", "name": "Alice 的"},
        headers=_auth(alice["token"]["access_token"]),
    )

    resp = await client.get("/api/v1/projects", headers=_auth(bob["token"]["access_token"]))
    assert resp.json()["data"]["items"] == [], "不能看到别的租户的项目"


async def test_list_projects_rejects_oversized_page_size(client):
    data = await _register(client, "guotao")
    resp = await client.get(
        "/api/v1/projects?page_size=1000", headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 422, "page_size 上限由统一分页依赖约束"


# ----------------------------------------------------------------------
# 数据范围（SELF / DEPT / ALL）—— 首次在真实接口上验证
# ----------------------------------------------------------------------
async def test_data_scope_self_only_sees_own_project(client, db_session_factory):
    """MEMBER（SELF）只能看到 owner_id 是自己的项目。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_uid = admin["user"]["id"]

    member_token, member_uid = await _add_member_with_role(
        client, db_session_factory, tenant_id=tenant_id, username="member-a", role_code=MEMBER_ROLE
    )

    await _seed_projects(
        db_session_factory,
        tenant_id=tenant_id,
        specs=[("ADMIN-P", admin_uid, None), ("MEMBER-P", member_uid, None)],
    )

    resp = await client.get("/api/v1/projects", headers=_auth(member_token))
    codes = {p["code"] for p in resp.json()["data"]["items"]}
    assert codes == {"MEMBER-P"}, f"SELF 范围只能看到自己的项目，实际 {codes}"


async def test_data_scope_all_sees_whole_tenant(client, db_session_factory):
    """TENANT_ADMIN（ALL）能看到全租户的项目，但仍受租户边界约束。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_uid = admin["user"]["id"]
    admin_token = _auth(admin["token"]["access_token"])

    _, member_uid = await _add_member_with_role(
        client, db_session_factory, tenant_id=tenant_id, username="member-b", role_code=MEMBER_ROLE
    )
    # 另一个租户的项目，绝不能出现在列表里
    bob = await _register(client, "bob")
    await _seed_projects(
        db_session_factory,
        tenant_id=bob["tenant"]["id"],
        specs=[("OTHER-P", bob["user"]["id"], None)],
    )

    await _seed_projects(
        db_session_factory,
        tenant_id=tenant_id,
        specs=[("ADMIN-P2", admin_uid, None), ("MEMBER-P2", member_uid, None)],
    )

    resp = await client.get("/api/v1/projects", headers=admin_token)
    codes = {p["code"] for p in resp.json()["data"]["items"]}
    assert codes == {"ADMIN-P2", "MEMBER-P2"}, f"ALL 看全租户但不越界，实际 {codes}"


async def test_data_scope_dept_sees_own_department_only(client, db_session_factory):
    """DEPT_MANAGER 看到本部门的，看不到别的部门。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_uid = admin["user"]["id"]

    # 部门用固定的 dept_id 值区分即可（DEPT 过滤只比对 dept_id 是否相等）
    DEPT_A, DEPT_B = 1001, 2002
    mgr_token, mgr_uid = await _add_member_with_role(
        client,
        db_session_factory,
        tenant_id=tenant_id,
        username="dept-manager",
        role_code=DEPT_MANAGER_ROLE,
        dept_id=DEPT_A,
    )

    await _seed_projects(
        db_session_factory,
        tenant_id=tenant_id,
        specs=[
            ("DEPT-A-P", mgr_uid, DEPT_A),
            ("DEPT-B-P", admin_uid, DEPT_B),
        ],
    )

    resp = await client.get("/api/v1/projects", headers=_auth(mgr_token))
    codes = {p["code"] for p in resp.json()["data"]["items"]}
    assert codes == {"DEPT-A-P"}, f"DEPT 范围只能看到本部门，实际 {codes}"
