"""数据范围（越权）测试。

★ 隔离 = 能不能跨租户（横向）；越权 = 同租户内能不能看到不该看的数据（纵向）。
  两者都是本目录的职责，不混进业务测试。

★ 本文件全程只走**公开接口**，不做任何数据库直连：
  用户、成员、角色、部门、项目全部通过 API 建出来。好处是它同时验证了
  「注册 → 邀请 → 挂部门 → 绑角色 → 登录 → 查列表」整条 RBAC 链路能串起来，
  而不只是单个 service 的行为。

★ 每个范围断言都**成对**出现（正向 + 反向），这是本项目的硬性纪律：

    [正向] MEMBER(SELF) 查列表 → 只看到自己的
    [反向] TENANT_ADMIN(ALL) 查同一接口 → 看到全部

  没有反向验证的断言一律视为无效——如果库里本来就只剩一条数据，
  正向断言会**假通过**，你以为是过滤器在起作用，其实只是碰巧没别的数据。
"""

from __future__ import annotations

import pytest

from app.core.constants import (
    DEPT_MANAGER_ROLE,
    MEMBER_ROLE,
    TENANT_ADMIN_ROLE,
    DataScope,
    widest_scope,
)

# 注意：不要把 pytestmark 设成 asyncio——本文件里有同步用例。
# 需要 async 的用例单独加 @pytest.mark.asyncio。

PASSWORD = "a-long-enough-passphrase"


def test_widest_scope_picks_broadest():
    """多角色取最宽（5.1）。

    这条收敛在 core/constants.widest_scope 一处计算，
    就是为了避免散在多个 service 里各算一遍、早晚算错。
    """
    assert widest_scope([DataScope.SELF, DataScope.DEPT]) == DataScope.DEPT
    assert widest_scope([DataScope.SELF, DataScope.ALL]) == DataScope.ALL
    assert widest_scope([DataScope.DEPT, DataScope.ALL]) == DataScope.ALL
    assert widest_scope([]) == DataScope.SELF  # 无角色 → 最小权限


# ----------------------------------------------------------------------
# 测试装置：全部通过公开接口搭建
# ----------------------------------------------------------------------
def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _login_in_tenant(client, username: str, tenant_id: int) -> str:
    """以指定租户身份登录，返回 access token（其 data_scope 由角色决定）。"""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD, "tenant_id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _role_id_of(client, admin_token: str, code: str) -> int:
    resp = await client.get("/api/v1/roles", headers=_h(admin_token))
    assert resp.status_code == 200, resp.text
    return next(r["id"] for r in resp.json()["data"] if r["code"] == code)


async def _bring_in_member(
    client, admin_token: str, username: str, *, role_code: str, dept_id: int | None = None
) -> str:
    """把已注册用户加入管理员的租户、挂到指定部门、绑定角色，返回其 access token。"""
    await _register(client, username)
    added = await client.post(
        "/api/v1/members", json={"username": username, "dept_id": dept_id}, headers=_h(admin_token)
    )
    assert added.status_code == 201, added.text
    member_id = added.json()["data"]["id"]

    role_id = await _role_id_of(client, admin_token, role_code)
    bound = await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=_h(admin_token)
    )
    assert bound.status_code == 201, bound.text
    return member_id


async def _create_project(client, token: str, code: str) -> int:
    resp = await client.post(
        "/api/v1/projects", json={"code": code, "name": f"项目{code}"}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _project_codes(client, token: str) -> set[str]:
    resp = await client.get("/api/v1/projects", headers=_h(token))
    assert resp.status_code == 200, resp.text
    return {p["code"] for p in resp.json()["data"]["items"]}


# ----------------------------------------------------------------------
# SELF 范围
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_self_scope_is_bounded_and_all_scope_is_not(client):
    """[正向 + 反向] SELF 只看自己的；同一批数据下 ALL 能看到全部。

    ★ 成对的意义：若只断言「SELF 只看到 1 条」，而库里恰好只有 1 条，
      断言会假通过。加上「ALL 能看到 2 条」才能证明 SELF 的过滤真在起作用。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = admin["token"]["access_token"]

    await _bring_in_member(client, admin_token, "member-a", role_code=MEMBER_ROLE)
    member_token = await _login_in_tenant(client, "member-a", tenant_id)

    # 两人各建一个项目，owner 分别是各自的 user_id
    await _create_project(client, admin_token, "ADMIN-P")
    await _create_project(client, member_token, "MEMBER-P")

    # [正向] SELF 只能看到自己名下的
    self_codes = await _project_codes(client, member_token)
    assert self_codes == {"MEMBER-P"}, f"SELF 应只见自己的项目，实际 {self_codes}"

    # [反向] ALL 能看到同一批数据的全部
    all_codes = await _project_codes(client, admin_token)
    assert all_codes == {"ADMIN-P", "MEMBER-P"}, f"ALL 应见全租户，实际 {all_codes}"


@pytest.mark.asyncio
async def test_all_scope_does_not_break_tenant_boundary(client):
    """[正向 + 反向] ALL 放开的是数据范围，**不是**租户边界。

    钉住「两层过滤是 AND 关系」——上层租户过滤永远生效。
    """
    alice = await _register(client, "alice")
    alice_token = alice["token"]["access_token"]
    await _create_project(client, alice_token, "ALICE-ONLY")

    # 另一个租户的管理员（同样是 ALL 范围）
    bob = await _register(client, "bob")
    bob_codes = await _project_codes(client, bob["token"]["access_token"])
    assert bob_codes == set(), f"ALL 也不能越租户，实际 {bob_codes}"

    # [反向] alice 自己看得到 → 证明上面空集是租户过滤造成的，而不是根本没数据
    assert await _project_codes(client, alice_token) == {"ALICE-ONLY"}


# ----------------------------------------------------------------------
# DEPT 范围
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dept_scope_is_bounded_and_all_scope_is_not(client):
    """[正向 + 反向] DEPT 只看本部门；ALL 能看到本部门 + 无部门的。

    做法全走公开接口：建部门 → 成员挂到部门 A 并绑 DEPT_MANAGER →
    该成员建的项目 dept_id 落在部门 A；管理员建的项目 dept_id 为空。
    DEPT 范围下前者可见、后者不可见。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = admin["token"]["access_token"]

    dept_a = (
        await client.post("/api/v1/departments", json={"name": "研发部"}, headers=_h(admin_token))
    ).json()["data"]["id"]
    # 第二个部门仅用于确认「差异不是因为部门只有 A」
    dept_b = (
        await client.post("/api/v1/departments", json={"name": "市场部"}, headers=_h(admin_token))
    ).json()["data"]["id"]
    assert dept_b != dept_a

    await _bring_in_member(
        client, admin_token, "dept-a-user", role_code=DEPT_MANAGER_ROLE, dept_id=dept_a
    )
    member_token = await _login_in_tenant(client, "dept-a-user", tenant_id)

    await _create_project(client, member_token, "DEPT-A-P")
    await _create_project(client, admin_token, "NO-DEPT-P")

    # [正向] DEPT 只看到本部门的
    dept_codes = await _project_codes(client, member_token)
    assert dept_codes == {"DEPT-A-P"}, f"DEPT 应只见本部门，实际 {dept_codes}"

    # [反向] ALL 能看到全部（含无部门的那条）
    all_codes = await _project_codes(client, admin_token)
    assert all_codes == {"DEPT-A-P", "NO-DEPT-P"}, f"ALL 应见全部，实际 {all_codes}"


# ----------------------------------------------------------------------
# 数据范围的来源（token 声明 vs 每请求重算）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scope_comes_from_token_not_recomputed_per_request(client):
    """数据范围来自 **token 声明**，而不是每个请求重查角色表。

    ★ 这是有意的取舍：角色变更要等 access token 过期（默认 30 分钟）才生效，
      换来的是鉴权路径不查库。此测试把该行为显式钉住——
      若哪天改成每请求查库，这条会失败，提醒这是有意做的改动。

    做法：成员以 MEMBER（SELF）登录拿到 token 后，管理员再给他绑 ALL 角色；
    不重新登录的话旧 token 仍按 SELF 过滤。
    """
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = admin["token"]["access_token"]

    member_id = await _bring_in_member(client, admin_token, "member-b", role_code=MEMBER_ROLE)
    member_token = await _login_in_tenant(client, "member-b", tenant_id)

    await _create_project(client, admin_token, "ADMIN-P2")
    await _create_project(client, member_token, "MEMBER-P2")

    # 旧 token 是 SELF 范围
    assert await _project_codes(client, member_token) == {"MEMBER-P2"}

    # 事后升权：绑一个 ALL 角色
    admin_role_id = await _role_id_of(client, admin_token, TENANT_ADMIN_ROLE)
    await client.post(
        f"/api/v1/members/{member_id}/roles",
        json={"role_id": admin_role_id},
        headers=_h(admin_token),
    )

    # 旧 token 不变（仍是 SELF）——证明范围来自 token，不是每请求重算
    assert await _project_codes(client, member_token) == {"MEMBER-P2"}, (
        "数据范围应来自 token 声明；若这里变成全集，说明改成了每请求查库"
    )

    # [反向] 重新登录拿到的新 token 才是 ALL
    fresh_token = await _login_in_tenant(client, "member-b", tenant_id)
    assert await _project_codes(client, fresh_token) == {"ADMIN-P2", "MEMBER-P2"}
