"""租户接口测试（第 2 周步骤 1）。

★ 本文件里最该看的一条是 `test_switch_succeeds_when_old_context_is_active`——
  它直接钉住切换租户时的一个真实陷阱：切换前的上下文是**旧租户**，
  而 `list_memberships_for_user` 在「上下文带 tenant_id」时会被钩子过滤到旧租户。
  如果 service 忘了先清空上下文再枚举成员关系，切换必然失败，
  而且失败信息会误导成「你不是成员」。这条测试在那种实现下会挂。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.repositories.tenant import TenantMemberRepository, TenantRepository

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


async def _make_second_membership(db_session_factory, *, user_id: int, code: str, name: str) -> int:
    """建第二个租户并把用户加进去。返回新租户 id。

    用 db_session_factory 而不是 client：这是「后台造数据」，
    不是要走接口验证的东西——成员管理的接口在步骤 3 才做。
    """
    session = db_session_factory()
    previous = snapshot_context()
    try:
        tenant = TenantRepository(session).create(
            code=code, name=name, status="ACTIVE", max_members=50
        )
        await session.flush()
        set_context(RequestContext(tenant_id=tenant.id, user_id=user_id, data_scope=DataScope.ALL))
        TenantMemberRepository(session).create(user_id=user_id, status=str(MemberStatus.ACTIVE))
        await session.commit()
        return tenant.id
    finally:
        restore_context(previous)
        await session.close()


# ----------------------------------------------------------------------
# GET /tenants/current
# ----------------------------------------------------------------------
async def test_current_tenant_returns_bound_tenant(client):
    data = await _register(client, "guotao")
    resp = await client.get("/api/v1/tenants/current", headers=_auth(data["token"]["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["id"] == data["tenant"]["id"]
    assert body["name"] == data["tenant"]["name"]


async def test_current_tenant_requires_auth(client):
    resp = await client.get("/api/v1/tenants/current")
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# POST /tenants/{id}/switch
# ----------------------------------------------------------------------
async def test_switch_succeeds_when_old_context_is_active(client, db_session_factory):
    """切换到自己是成员的第二个租户，新 token 必须绑定新租户。

    ★ 这是核心用例：切换发生时，请求上下文是**旧租户**（来自旧 token）。
      若 service 不先清空上下文，钩子会把成员关系查询过滤到旧租户，
      目标租户的成员关系查不到 → 切换永远 404。这条正是防这个。
    """
    data = await _register(client, "guotao")
    user_id = data["user"]["id"]
    tenant_a = data["tenant"]["id"]

    tenant_b = await _make_second_membership(
        db_session_factory, user_id=user_id, code="globex-x1", name="Globex"
    )
    assert tenant_b != tenant_a

    # 此刻用的是**租户 A 作用域的 token**——上下文正是旧租户
    resp = await client.post(
        f"/api/v1/tenants/{tenant_b}/switch", headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["tenant"]["id"] == tenant_b

    # 新 token 必须已经绑定到租户 B
    new_access = body["token"]["access_token"]
    me = await client.get("/api/v1/auth/me", headers=_auth(new_access))
    assert me.json()["data"]["tenant"]["id"] == tenant_b


async def test_switch_token_scopes_authorities_to_new_tenant(client, db_session_factory):
    """切换后的 token，其 roles / data_scope 必须按**目标租户**计算。

    用户在租户 A 是 TENANT_ADMIN，在租户 B 只加了成员、没绑角色。
    所以切到 B 后 roles 应为空、data_scope 应为最小的 SELF——
    绝不能把 A 的管理员身份带进 B。
    """
    data = await _register(client, "guotao")
    user_id = data["user"]["id"]
    tenant_b = await _make_second_membership(
        db_session_factory, user_id=user_id, code="globex-x2", name="Globex"
    )

    resp = await client.post(
        f"/api/v1/tenants/{tenant_b}/switch", headers=_auth(data["token"]["access_token"])
    )
    new_access = resp.json()["data"]["token"]["access_token"]

    me = await client.get("/api/v1/auth/me", headers=_auth(new_access))
    body = me.json()["data"]
    assert body["roles"] == [], "切换后角色必须按目标租户重新计算"
    assert body["data_scope"] == "SELF", "无角色时数据范围应为最小的 SELF"


async def test_switch_to_non_member_tenant_returns_404(client, db_session_factory):
    """切换到不是成员的租户 → 404（不是 403，避免存在性泄露）。"""
    data = await _register(client, "guotao")
    # 造一个与用户无关的租户
    other = await _register(client, "alice")
    other_tenant = other["tenant"]["id"]

    resp = await client.post(
        f"/api/v1/tenants/{other_tenant}/switch", headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


async def test_switch_requires_auth(client):
    resp = await client.post("/api/v1/tenants/1/switch")
    assert resp.status_code == 401


async def test_switch_to_disabled_membership_returns_404(client, db_session_factory):
    """成员关系已被停用的租户，同样视为无权访问。"""
    data = await _register(client, "guotao")
    user_id = data["user"]["id"]
    tenant_b = await _make_second_membership(
        db_session_factory, user_id=user_id, code="globex-x3", name="Globex"
    )

    # 把用户在 B 的成员关系停用
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=tenant_b, user_id=user_id, data_scope=DataScope.ALL))
        member = await TenantMemberRepository(session).get_membership(user_id)
        member.status = str(MemberStatus.DISABLED)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    resp = await client.post(
        f"/api/v1/tenants/{tenant_b}/switch", headers=_auth(data["token"]["access_token"])
    )
    assert resp.status_code == 404
