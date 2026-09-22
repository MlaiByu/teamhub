"""角色接口测试（第 2 周步骤 4）。

★ 覆盖 RBAC 的两个关键边界：
  1. 角色编码的唯一约束**含 tenant_id**——两个租户可以用同一个编码，
     这是「唯一约束必须含 tenant_id」这条落库契约在接口层的体现。
  2. 角色守卫：只有 MEMBER 角色的用户不能创建角色（403）。
"""

from __future__ import annotations

import pytest

from app.core.constants import MEMBER_ROLE, DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.security import hash_password
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
# GET /roles
# ----------------------------------------------------------------------
async def test_list_roles_includes_default_roles(client):
    """注册开通的租户默认带三个角色，且各自的数据范围正确。"""
    data = await _register(client, "guotao")
    resp = await client.get("/api/v1/roles", headers=_auth(data["token"]["access_token"]))
    assert resp.status_code == 200, resp.text
    roles = {r["code"]: r["data_scope"] for r in resp.json()["data"]}
    assert roles["TENANT_ADMIN"] == "ALL"
    assert roles["DEPT_MANAGER"] == "DEPT"
    assert roles["MEMBER"] == "SELF"


async def test_list_roles_only_own_tenant(client):
    """角色列表绝不能漏进别的租户的角色。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    # alice 建一个自定义角色
    await client.post(
        "/api/v1/roles",
        json={"code": "AUDITOR", "name": "审计员", "data_scope": "DEPT"},
        headers=_auth(alice["token"]["access_token"]),
    )

    resp = await client.get("/api/v1/roles", headers=_auth(bob["token"]["access_token"]))
    codes = {r["code"] for r in resp.json()["data"]}
    assert "AUDITOR" not in codes, "不能看到别的租户的角色"


async def test_list_roles_requires_auth(client):
    resp = await client.get("/api/v1/roles")
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# POST /roles
# ----------------------------------------------------------------------
async def test_create_role(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/roles",
        json={"code": "PROJECT_LEAD", "name": "项目组长", "data_scope": "DEPT"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()["data"]
    assert body["code"] == "PROJECT_LEAD"
    assert body["data_scope"] == "DEPT"


async def test_create_role_normalizes_code_to_uppercase(client):
    """小写编码要统一成大写——编码是程序里的引用键，大小写混用会对不上。"""
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/roles",
        json={"code": "project_lead", "name": "项目组长", "data_scope": "DEPT"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["code"] == "PROJECT_LEAD"


async def test_create_role_duplicate_code_conflicts(client):
    data = await _register(client, "guotao")
    token = _auth(data["token"]["access_token"])
    body = {"code": "CUSTOM", "name": "自定义", "data_scope": "SELF"}
    await client.post("/api/v1/roles", json=body, headers=token)

    resp = await client.post("/api/v1/roles", json=body, headers=token)
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


async def test_same_code_allowed_in_different_tenants(client):
    """同一编码在两个租户下可以共存。

    ★ 这是「唯一约束必须含 tenant_id」在接口层的验证——
      如果约束漏了 tenant_id，第二个租户创建时会撞 UNIQUE 报 500/409。
    """
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")
    body = {"code": "CUSTOM", "name": "自定义", "data_scope": "SELF"}

    r1 = await client.post(
        "/api/v1/roles", json=body, headers=_auth(alice["token"]["access_token"])
    )
    r2 = await client.post("/api/v1/roles", json=body, headers=_auth(bob["token"]["access_token"]))
    assert r1.status_code == r2.status_code == 201, (
        "两个租户应能使用同一个角色编码（唯一约束含 tenant_id）"
    )


async def test_create_role_requires_admin_role(client, db_session_factory):
    data = await _register(client, "guotao")
    member_token = await _member_token(
        client, db_session_factory, tenant_id=data["tenant"]["id"], username="member1"
    )
    resp = await client.post(
        "/api/v1/roles",
        json={"code": "CUSTOM", "name": "自定义", "data_scope": "SELF"},
        headers=_auth(member_token),
    )
    assert resp.status_code == 403


async def test_create_role_rejects_invalid_data_scope(client):
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/roles",
        json={"code": "CUSTOM", "name": "自定义", "data_scope": "EVERYTHING"},
        headers=_auth(data["token"]["access_token"]),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == 40000


async def test_create_role_rejects_invalid_code(client):
    data = await _register(client, "guotao")
    for bad_code in ["1START", "has space", "has-dash"]:
        resp = await client.post(
            "/api/v1/roles",
            json={"code": bad_code, "name": "x", "data_scope": "SELF"},
            headers=_auth(data["token"]["access_token"]),
        )
        assert resp.status_code == 422, f"编码 {bad_code!r} 应被拒绝"
