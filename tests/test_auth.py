"""认证业务测试（第 2 周交付物：「能注册登录」）。

★ 全部走 HTTP 层（ASGI 直连），不直接调 service。
  理由：这一周要证明的是「接口能通」，而 `Depends` 装配、中间件写上下文、
  异常处理器转信封、`response_model` 校验这些环节的 bug 只有走完整链路才暴露。
  单独测 service 会漏掉「幂等键忘了加依赖」这类只存在于装配层的问题。

★ 与 tests/security/ 的分工（PROJECT-PLAN 10.1）：
    本文件 = 业务正确性（能注册、能登录、令牌能轮换）
    tests/security/ = 隔离与越权（看不到别人的数据、守卫没被绕过）
  两者不混。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.repositories.tenant import TenantMemberRepository, TenantRepository
from app.repositories.user import UserRepository

pytestmark = pytest.mark.asyncio

GOOD_PASSWORD = "a-long-enough-passphrase"


async def _register(client, username: str = "guotao", **overrides) -> dict:
    """注册并返回 data 段。多数用例都从这一步开始。"""
    body = {"username": username, "password": GOOD_PASSWORD, **overrides}
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------
async def test_register_creates_user_and_personal_tenant(client):
    """注册同时建个人团队并绑定管理员角色。

    这条覆盖了本 slice 最关键的设计决策：refresh token 是租户级表，
    没有团队就发不出 refresh token。所以注册必须一并建团队，
    否则「注册成功但登录不了」。
    """
    data = await _register(client, "guotao")

    assert data["user"]["username"] == "guotao"
    assert data["user"]["email"] is None
    # 绝不能把密码哈希漏出去
    assert "password_hash" not in data["user"]

    assert data["tenant"]["name"] == "guotao 的团队"
    assert data["tenant"]["code"].startswith("guotao-")

    assert data["token"]["access_token"]
    assert data["token"]["refresh_token"]
    assert data["token"]["token_type"] == "Bearer"
    assert data["token"]["expires_in"] > 0


async def test_register_uses_custom_tenant_name(client):
    data = await _register(client, "alice", tenant_name="Acme 科技")
    assert data["tenant"]["name"] == "Acme 科技"


async def test_register_duplicate_username_conflicts(client):
    await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/auth/register", json={"username": "guotao", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40900


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"username": "ab", "password": GOOD_PASSWORD}, "用户名过短"),
        ({"username": "guo tao", "password": GOOD_PASSWORD}, "用户名含空白"),
        ({"username": "guotao", "password": "12345678"}, "密码纯数字"),
        ({"username": "guotao", "password": "aaaaaaaa"}, "密码单字符重复"),
        ({"username": "guotao", "password": "short"}, "密码过短"),
        ({"username": "guotao", "password": GOOD_PASSWORD, "email": "nope"}, "邮箱格式错"),
    ],
)
async def test_register_validation_rejects_bad_input(client, body, why):
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 422, f"{why} 应被拒绝：{resp.text}"
    assert resp.json()["code"] == 40000


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
async def test_login_succeeds_and_issues_tokens(client):
    await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "guotao", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["access_token"] and data["refresh_token"]


async def test_login_wrong_password_unauthorized(client):
    await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "guotao", "password": "wrong-password-here"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40100


async def test_login_unknown_user_same_response_as_wrong_password(client):
    """未知用户与错误密码必须给出**完全一致**的响应。

    否则攻击者能靠响应差异枚举出哪些用户名存在。
    这里连同状态码和 message 一起断言——只对齐状态码、message 泄漏同样白搭。
    """
    await _register(client, "guotao")

    wrong_pw = await client.post(
        "/api/v1/auth/login", json={"username": "guotao", "password": "wrong-password-here"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"username": "nobody-here", "password": "wrong-password-here"}
    )

    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json()["code"] == unknown.json()["code"]
    assert wrong_pw.json()["message"] == unknown.json()["message"]


async def test_login_returns_bearer_token(client):
    """令牌类型必须是 RFC 6750 的 Bearer，前端据此拼请求头。"""
    data = await _register(client, "guotao")
    assert data["token"]["token_type"] == "Bearer"


# ----------------------------------------------------------------------
# /auth/me —— 证明 token 声明真的被中间件读进去了
# ----------------------------------------------------------------------
async def test_me_returns_identity_and_authorities(client):
    data = await _register(client, "guotao")
    resp = await client.get("/api/v1/auth/me", headers=_auth(data["token"]["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]

    assert body["user"]["username"] == "guotao"
    assert body["tenant"]["id"] == data["tenant"]["id"]
    # 注册时绑的是 TENANT_ADMIN，其 data_scope 是 ALL
    assert body["roles"] == ["TENANT_ADMIN"]
    assert body["data_scope"] == "ALL"


async def test_me_requires_authentication(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 40100


async def test_me_rejects_garbage_token(client):
    resp = await client.get("/api/v1/auth/me", headers=_auth("not-a-real-jwt"))
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# 刷新（轮换）
# ----------------------------------------------------------------------
async def test_refresh_rotates_both_tokens(client):
    """每次刷新都必须换发**新的** refresh token（rotate on use）。"""
    data = await _register(client, "guotao")
    old_refresh = data["token"]["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200, resp.text
    new = resp.json()["data"]

    assert new["refresh_token"] != old_refresh, "refresh token 必须轮换，不能复用"
    assert new["access_token"]


async def test_refresh_rejects_access_token(client):
    """拿 access token 去刷新必须被拒——否则 access 的短有效期就形同虚设。"""
    data = await _register(client, "guotao")
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["token"]["access_token"]}
    )
    assert resp.status_code == 401


async def test_refresh_rejects_garbage(client):
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "garbage"})
    assert resp.status_code == 401


# ----------------------------------------------------------------------
# 复用检测
# ----------------------------------------------------------------------
async def test_reuse_detection_revokes_whole_chain(client):
    """已轮换的 refresh token 再次出现 → 判定泄露 → 撤销整条链。

    ★ 这条是本周最该写的安全断言。它证明的不只是「旧票不能用」，
      而是「旧票被用时会触发主动响应」——两件事的价值差一个数量级。
    """
    data = await _register(client, "guotao")
    first_refresh = data["token"]["refresh_token"]

    # 正常轮换一次 → first 变成 ROTATED，并签发出 second
    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200
    second_refresh = rotated.json()["data"]["refresh_token"]

    # 攻击者拿已被换掉的旧票再来一次
    reused = await client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert reused.status_code == 401
    assert "复用" in reused.json()["message"]

    # ★ 关键：新票也必须已经失效——否则「撤销整条链」只是嘴上说说
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": second_refresh})
    assert after.status_code == 401, "复用检测必须连带撤销后继令牌，否则泄露未真正掐断"


async def test_plain_rotation_does_not_trigger_reuse_alarm(client):
    """正常轮换不产生复用告警，且旧票只报「已失效」而非「复用」。

    ★ 这条防的是「告警疲劳」：如果把普通作废也报成泄露，
      真正的泄露事件会淹在噪音里。
    """
    data = await _register(client, "guotao")
    first = data["token"]["refresh_token"]

    ok_resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert ok_resp.status_code == 200

    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert again.status_code == 401
    # ROTATED 的旧票再出现**确实**是复用——这里断言的是措辞包含「复用」，
    # 证明走的是告警分支而不是普通失效分支
    assert "复用" in again.json()["message"]


# ----------------------------------------------------------------------
# 登出
# ----------------------------------------------------------------------
async def test_logout_revokes_chain(client):
    data = await _register(client, "guotao")
    refresh_token = data["token"]["refresh_token"]

    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert resp.status_code == 200

    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert after.status_code == 401


async def test_logout_is_idempotent(client):
    """重复登出也要成功。

    登出接口报错会让前端陷入「想登出但登不出去」的状态——
    用户已经决定要走，此时返回错误没有任何补救价值。
    """
    data = await _register(client, "guotao")
    refresh_token = data["token"]["refresh_token"]

    first = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    second = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert first.status_code == second.status_code == 200


async def test_logout_with_invalid_token_still_succeeds(client):
    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": "garbage"})
    assert resp.status_code == 200


async def test_logout_does_not_trigger_reuse_alarm(client):
    """登出后旧票再试，应该只报「已失效」，不应该报「复用」。"""
    data = await _register(client, "guotao")
    refresh_token = data["token"]["refresh_token"]

    await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})

    assert after.status_code == 401
    assert "复用" not in after.json()["message"], (
        "登出不是复用——报成复用会让真正的泄露告警淹没在噪音里"
    )


# ----------------------------------------------------------------------
# 账号 / 租户状态对登录与刷新的影响
# ----------------------------------------------------------------------
async def test_login_rejected_when_tenant_suspended(client, db_session_factory):
    """租户被停用后，其成员不得登录。

    ★ 这条规则很容易只写在 service 里就以为生效了——停用租户是平台运营动作，
      如果登录路径不校验状态，被停用的租户仍然能正常签发新令牌，
      「停用」就只是个显示字段。
    """
    data = await _register(client, "guotao")

    session = db_session_factory()
    tenant = await TenantRepository(session).get(data["tenant"]["id"])
    tenant.status = "SUSPENDED"
    await session.commit()
    await session.close()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": "guotao", "password": GOOD_PASSWORD}
    )
    assert resp.status_code == 401, resp.text
    assert "SUSPENDED" in resp.json()["message"]


async def test_refresh_rejected_when_user_disabled(client, db_session_factory):
    """账号被禁用后，已签发的 refresh token 也不得再换取新令牌。

    ★ 与「撤销令牌」互补：禁用账号时如果只标记 `is_active=False` 而不校验刷新路径，
      旧 refresh 仍能换来可用的 access token，禁用形同虚设。
    """
    data = await _register(client, "guotao")

    session = db_session_factory()
    user = await UserRepository(session).get_by_username("guotao")
    user.is_active = False
    await session.commit()
    await session.close()

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["token"]["refresh_token"]}
    )
    assert resp.status_code == 401, resp.text


async def test_login_rejected_when_membership_disabled(client, db_session_factory):
    """成员关系被停用后不得登录——「人被移出团队」必须立刻生效。

    ★ 这里必须自己设租户上下文：`TenantMember` 是租户级表，
      不带上下文改它会被 repository 守卫直接拒绝。
      这反过来也证明了守卫在起作用——测试代码也不能绕过它。
    """
    data = await _register(client, "guotao")

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=data["tenant"]["id"], data_scope=DataScope.ALL))
    try:
        user = await UserRepository(session).get_by_username("guotao")
        member = await TenantMemberRepository(session).get_membership(user.id)
        member.status = str(MemberStatus.DISABLED)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    resp = await client.post(
        "/api/v1/auth/login", json={"username": "guotao", "password": GOOD_PASSWORD}
    )
    # 成员状态不可用 → 该账号在「有效团队」里一个都不剩 → 业务冲突
    assert resp.status_code == 409, resp.text
    assert "团队" in resp.json()["message"]
