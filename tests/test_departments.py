"""部门接口测试（第 2 周步骤 2）。

★ 覆盖两条关键防线：
  1. 跨租户挂载：`POST /departments` 的 parent_id 指向**别的租户**的部门时，
     必须返回 404——get() 走守卫 + 钩子过滤，别的租户 ID 自然查不到。
  2. 角色守卫：没有 TENANT_ADMIN 角色的用户创建部门必须 403。

  第 2 条之所以单独写，是因为它验证的是 `require_roles` 依赖真的在工作——
  一个能读不能写的普通成员，不能因为「登录了」就越权建部门。
"""

from __future__ import annotations

import pytest

from app.core.constants import MEMBER_ROLE, DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.security import hash_password
from app.repositories.org import DepartmentRepository
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


async def _create_dept_in_tenant(
    db_session_factory, *, tenant_id: int, name: str, parent_id: int | None = None
) -> int:
    """后台在指定租户下建一个部门。返回部门 id。"""
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
        dept = DepartmentRepository(session).create(
            name=name, parent_id=parent_id, leader_id=None, path=""
        )
        await session.flush()
        dept.path = (f"/{parent_id}/" if parent_id else "/") + f"{dept.id}/"
        await session.commit()
        return dept.id
    finally:
        restore_context(previous)
        await session.close()


async def _login_as_member(client, db_session_factory, *, tenant_id: int, username: str) -> str:
    """建一个只有 MEMBER 角色（**不是** TENANT_ADMIN）的用户并返回其 access token。

    用于验证 require_roles 守卫——注册默认绑的是 TENANT_ADMIN，
    所以要单独造一个「能读不能写」的普通成员。
    """
    session = db_session_factory()
    previous = snapshot_context()
    try:
        user = UserRepository(session).create(
            username=username, password_hash=hash_password(GOOD_PASSWORD), is_active=True
        )
        await session.flush()
        set_context(RequestContext(tenant_id=tenant_id, user_id=user.id, data_scope=DataScope.ALL))
        TenantMemberRepository(session).create(user_id=user.id, status=str(MemberStatus.ACTIVE))
        role = await RoleRepository(session).get_by_code(MEMBER_ROLE)
        await UserRoleRepository(session).bind(user_id=user.id, role_id=role.id)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


# ----------------------------------------------------------------------
# 创建部门
# ----------------------------------------------------------------------
async def test_create_root_department(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/departments",
        json={"name": "研发部"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["name"] == "研发部"
    assert body["parent_id"] is None
    assert body["path"] == f"/{body['id']}/"


async def test_create_child_department_path(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])

    root = await client.post("/api/v1/departments", json={"name": "研发部"}, headers=token)
    root_id = root.json()["data"]["id"]

    child = await client.post(
        "/api/v1/departments", json={"name": "后端组", "parent_id": root_id}, headers=token
    )
    assert child.status_code == 201, child.text
    body = child.json()["data"]
    assert body["parent_id"] == root_id
    # 物化路径必须拼成 parent.path + id/
    assert body["path"] == f"/{root_id}/{body['id']}/"


async def test_create_department_with_cross_tenant_parent_returns_404(client, db_session_factory):
    """把部门挂到**别的租户**的部门下 → 404。

    ★ 这是跨租户挂载拦截的验证。若守卫失效，A 租户就能把部门挂到 B 租户下，
      造成两个租户的部门树缠绕——多租户里最隐蔽的一类数据污染。
    """
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    # 在 alice 的租户里建一个部门，拿到它的 id
    alice_dept = await _create_dept_in_tenant(
        db_session_factory, tenant_id=alice["tenant"]["id"], name="Alice的研发部"
    )

    # bob 试图把自己的部门挂到 alice 的部门下
    resp = await client.post(
        "/api/v1/departments",
        json={"name": "越权挂载", "parent_id": alice_dept},
        headers=_auth(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == 40400


async def test_create_department_duplicate_sibling_name_conflicts(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])

    await client.post("/api/v1/departments", json={"name": "研发部"}, headers=token)
    resp = await client.post("/api/v1/departments", json={"name": "研发部"}, headers=token)
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


async def test_create_department_requires_admin_role(client, db_session_factory):
    """只有 MEMBER 角色的用户创建部门 → 403。

    ★ 这条直接验证 require_roles 真的在拦，而不是只挂了个名字。
    """
    data = await _register(client, "guotao")
    member_token = await _login_as_member(
        client, db_session_factory, tenant_id=data["tenant"]["id"], username="member1"
    )

    resp = await client.post(
        "/api/v1/departments", json={"name": "越权部门"}, headers=_auth(member_token)
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 40300


async def test_create_department_requires_auth(client):
    resp = await client.post("/api/v1/departments", json={"name": "研发部"})
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# 部门树
# ----------------------------------------------------------------------
async def test_department_tree_is_nested(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])

    root = await client.post("/api/v1/departments", json={"name": "研发部"}, headers=token)
    root_id = root.json()["data"]["id"]
    await client.post(
        "/api/v1/departments", json={"name": "后端组", "parent_id": root_id}, headers=token
    )
    await client.post(
        "/api/v1/departments", json={"name": "前端组", "parent_id": root_id}, headers=token
    )

    resp = await client.get("/api/v1/departments", headers=token)
    assert resp.status_code == 200, resp.text
    tree = resp.json()["data"]

    assert len(tree) == 1, "应该只有一个根部门"
    assert tree[0]["name"] == "研发部"
    child_names = {c["name"] for c in tree[0]["children"]}
    assert child_names == {"后端组", "前端组"}


async def test_department_tree_requires_auth(client):
    resp = await client.get("/api/v1/departments")
    assert resp.status_code == 401


async def test_department_tree_only_shows_own_tenant(client, db_session_factory):
    """部门树绝不能漏进别的租户的部门。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    await _create_dept_in_tenant(
        db_session_factory, tenant_id=alice["tenant"]["id"], name="Alice的部门"
    )
    await _create_dept_in_tenant(
        db_session_factory, tenant_id=bob["tenant"]["id"], name="Bob的部门"
    )

    resp = await client.get("/api/v1/departments", headers=_auth(bob["token"]["access_token"]))
    names = {node["name"] for node in resp.json()["data"]}
    assert names == {"Bob的部门"}, f"只能看到本租户的部门，实际：{names}"
