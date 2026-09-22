"""成员接口测试（第 2 周步骤 3）。

★ 重点防线：
  1. 配额：到 max_members 上限时拒绝（409），「扩容」必须是个显式动作
  2. 角色守卫：只有 MEMBER 角色的用户不能加人、不能绑角色（403）
  3. 跨租户：把 A 租户的角色绑给 B 租户的成员 → 404
"""

from __future__ import annotations

import pytest

from app.core.constants import MEMBER_ROLE, DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.security import hash_password
from app.repositories.rbac import RoleRepository, UserRoleRepository
from app.repositories.tenant import TenantMemberRepository, TenantRepository
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


async def _make_user(db_session_factory, username: str) -> int:
    """后台建一个全局用户（不加入任何租户）。返回 user id。"""
    session = db_session_factory()
    try:
        user = UserRepository(session).create(
            username=username, password_hash=hash_password(GOOD_PASSWORD), is_active=True
        )
        await session.commit()
        return user.id
    finally:
        await session.close()


async def _member_token(client, db_session_factory, *, tenant_id: int, username: str) -> str:
    """造一个只有 MEMBER 角色（无管理员权）的用户并返回 access token。"""
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
# GET /members
# ----------------------------------------------------------------------
async def test_list_members_shows_registered_user_info(client, db_session_factory):
    """成员列表要带出用户名，不能只给一串 user_id。"""
    data = await _register(client, "guotao")
    resp = await client.get("/api/v1/members", headers=_auth(data["token"]["access_token"]))
    assert resp.status_code == 200, resp.text
    members = resp.json()["data"]
    assert len(members) == 1
    assert members[0]["username"] == "guotao"
    assert "password_hash" not in members[0]


async def test_list_members_requires_auth(client):
    resp = await client.get("/api/v1/members")
    assert resp.status_code == 401


async def test_list_members_only_own_tenant(client):
    """成员列表绝不能漏进别的租户的人。"""
    await _register(client, "alice")
    bob = await _register(client, "bob")
    resp = await client.get("/api/v1/members", headers=_auth(bob["token"]["access_token"]))
    usernames = {m["username"] for m in resp.json()["data"]}
    assert usernames == {"bob"}, f"只能看到本租户成员，实际：{usernames}"


# ----------------------------------------------------------------------
# POST /members
# ----------------------------------------------------------------------
async def test_add_existing_user_as_member(client, db_session_factory):
    data = await _register(client, "guotao")
    await _make_user(db_session_factory, "newbie")

    resp = await client.post(
        "/api/v1/members", json={"username": "newbie"}, headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["username"] == "newbie"

    # 新成员现在能登录进这个租户
    login = await client.post(
        "/api/v1/auth/login", json={"username": "newbie", "password": GOOD_PASSWORD}
    )
    assert login.status_code == 200, login.text
    me = await client.get("/api/v1/auth/me", headers=_auth(login.json()["data"]["access_token"]))
    assert me.json()["data"]["tenant"]["id"] == data["tenant"]["id"]


async def test_add_member_unknown_user_returns_404(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/members",
        json={"username": "no-such-user"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 404


async def test_add_member_duplicate_conflicts(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/members", json={"username": "guotao"}, headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


async def test_add_member_respects_max_members_quota(client, db_session_factory):
    """成员数到上限后拒绝加入。

    ★ 「扩容」必须是显式动作，不能靠撞上限后报错才发现——
      但**超过上限**这件事本身必须被拦，否则 max_members 就只是个显示字段。
    """
    data = await _register(client, "guotao")
    tenant_id = data["tenant"]["id"]

    # 把配额压到 1（注册者已占 1 个）
    session = db_session_factory()
    tenant = await TenantRepository(session).get(tenant_id)
    tenant.max_members = 1
    await session.commit()
    await session.close()

    await _make_user(db_session_factory, "newbie")
    resp = await client.post(
        "/api/v1/members", json={"username": "newbie"}, headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 409, resp.text
    assert "上限" in resp.json()["message"]


async def test_add_member_requires_admin_role(client, db_session_factory):
    data = await _register(client, "guotao")
    member_token = await _member_token(
        client, db_session_factory, tenant_id=data["tenant"]["id"], username="member1"
    )
    resp = await client.post(
        "/api/v1/members", json={"username": "whoever"}, headers=_auth(member_token)
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# POST /members/{id}/roles
# ----------------------------------------------------------------------
async def _get_member_id(client, admin_token: dict) -> int:
    resp = await client.get("/api/v1/members", headers=admin_token)
    return resp.json()["data"][0]["id"]


async def test_assign_role_to_member(client, db_session_factory):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    member_id = await _get_member_id(client, token)

    # 取 MEMBER 角色 id
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=data["tenant"]["id"], data_scope=DataScope.ALL))
        role = await RoleRepository(session).get_by_code(MEMBER_ROLE)
        member_role_id = role.id
    finally:
        restore_context(previous)
        await session.close()

    resp = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": member_role_id}, headers=token
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["role_id"] == member_role_id


async def test_assign_role_is_idempotent(client, db_session_factory):
    """重复绑定同一角色返回现有绑定，而不是撞 UNIQUE 约束报 500/409。"""
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    member_id = await _get_member_id(client, token)

    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=data["tenant"]["id"], data_scope=DataScope.ALL))
        role = await RoleRepository(session).get_by_code(MEMBER_ROLE)
        role_id = role.id
    finally:
        restore_context(previous)
        await session.close()

    first = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=token
    )
    second = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=token
    )
    assert first.status_code == second.status_code == 201


async def test_assign_cross_tenant_member_returns_404(client):
    """给**别的租户**的成员绑角色 → 404。

    ★ 若守卫失效，管理员就能把角色绑到别的租户的成员上，
      相当于凭空操纵别的租户的权限分配。
    """
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")

    alice_member_id = await _get_member_id(client, _auth(alice["token"]["access_token"]))

    # bob 用自己租户的管理员身份，去给 alice 租户的成员绑角色
    resp = await client.post(
        f"/api/v1/members/{alice_member_id}/roles",
        json={"role_id": 1},
        headers=_auth(bob["token"]["access_token"]),
    )
    assert resp.status_code == 404


async def test_assign_role_requires_admin_role(client, db_session_factory):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    member_id = await _get_member_id(client, token)
    member_token = await _member_token(
        client, db_session_factory, tenant_id=data["tenant"]["id"], username="member1"
    )

    resp = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": 1}, headers=_auth(member_token)
    )
    assert resp.status_code == 403
