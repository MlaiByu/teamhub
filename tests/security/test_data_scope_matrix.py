"""数据范围越权矩阵（第 6 周）。

★ 这是全项目**唯一一处「同一份数据、不同身份看到不同结果」的集中验证**。
  别的安全测试验证的是「租户之间看不串」（横向），本文件验证的是
  「同租户内、不同数据范围看到的东西不同」（纵向）。两个维度都是越权，
  但机制不同：租户隔离靠 `tenant_id` 钩子，数据范围靠 `owner_id`/`dept_id` 钩子。

★ 为什么用「一个身份 × 全部资源」的矩阵，而不是逐个接口写用例：

  数据范围的问题特点是**漏一处就静默放行**——新增一个接口忘了走守卫查询，
  或者像附件那样查了一张不带 `DataScopedMixin` 的表，都不会报错，
  只是「本该看不到的人看到了」。逐个接口写用例时，新增接口很容易漏测。

  矩阵的写法把「身份 × 资源」的所有组合一次钉死，新增受保护资源时
  只需在 `SEEDS` 里加一行，所有身份×该资源的组合自动被覆盖。

★ 三层身份覆盖：

    alice  TENANT_ADMIN  → ALL，看全租户
    mgr    DEPT_MANAGER  → DEPT，看本部门（研发部）
    mem_a  MEMBER        → SELF，只看 owner 是自己的（研发部）
    mem_b  MEMBER        → SELF，只看 owner 是自己的（市场部）

★ 断言方向成对（正向 + 反向）：
    范围内 → 200；范围外 → 404（不是 403，避免存在性泄露）
    列表 → 返回集合**精确等于**期望集合（多一条是泄露，少一条是误杀）
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.constants import (
    DEPT_MANAGER_ROLE,
    MEMBER_ROLE,
    DataScope,
)
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.core.storage import LocalStorage
from app.repositories.project import ProjectRepository
from app.repositories.task import TaskRepository

PASSWORD = "a-long-enough-passphrase"

# ----------------------------------------------------------------------
# 种子：4 个资源 × (owner, dept)，覆盖「自己的 / 本部门的 / 别人的」三种归属
# ----------------------------------------------------------------------
# code → (owner 身份, 所属部门身份)
SEEDS: dict[str, tuple[str, str | None]] = {
    "P_ADMIN": ("alice", None),  # 管理员私有，无部门
    "P_DEV": ("mgr", "dev"),  # 研发部经理的
    "P_A": ("mem_a", "dev"),  # 研发部成员甲的
    "P_B": ("mem_b", "mkt"),  # 市场部成员乙的
}
ALL_CODES = list(SEEDS)

# ----------------------------------------------------------------------
# 期望可见性（矩阵的核心；改这里就等于改测试契约）
# ----------------------------------------------------------------------
EXPECTED_VISIBLE: dict[str, set[str]] = {
    # ALL：全租户都能看
    "alice": set(ALL_CODES),
    # DEPT=研发部：只看 dept_id 等于自己部门的（P_DEV / P_A）；
    # P_ADMIN 的 dept_id 为空，不等于「研发部」，所以看不到——
    # 这是「DEPT 不是 ALL」的关键区别。
    "mgr": {"P_DEV", "P_A"},
    # SELF：只看 owner_id 等于自己的
    "mem_a": {"P_A"},
    "mem_b": {"P_B"},
}

IDENTITIES = ["alice", "mgr", "mem_a", "mem_b"]


# ----------------------------------------------------------------------
# 装置
# ----------------------------------------------------------------------
class Scenario:
    """一次建好的完整场景：身份 token + 资源 id + 附件 id。"""

    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}
        self.project_ids: dict[str, int] = {}
        self.task_ids: dict[str, int] = {}
        self.attachment_ids: dict[str, int] = {}
        self.tenant_id: int = 0

    def headers(self, who: str) -> dict:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _login_in(client, username: str, tenant_id: int) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD, "tenant_id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _build_scenario(client, db_session_factory) -> Scenario:
    """建租户 + 4 身份 + 2 部门 + 4 资源（项目各带一个任务）。"""
    sc = Scenario()

    alice = await _register(client, "alice")
    sc.tenant_id = alice["tenant"]["id"]
    admin_token = alice["token"]["access_token"]
    alice_uid = alice["user"]["id"]

    # 两个部门
    dev_id = (
        await client.post("/api/v1/departments", json={"name": "研发部"}, headers=_h(admin_token))
    ).json()["data"]["id"]
    mkt_id = (
        await client.post("/api/v1/departments", json={"name": "市场部"}, headers=_h(admin_token))
    ).json()["data"]["id"]

    # 三个非管理员身份：加入租户、挂部门、绑角色
    specs = [
        ("mgr", dev_id, DEPT_MANAGER_ROLE),
        ("mem_a", dev_id, MEMBER_ROLE),
        ("mem_b", mkt_id, MEMBER_ROLE),
    ]
    uids: dict[str, int] = {"alice": alice_uid}
    for username, dept_id, role_code in specs:
        await _register(client, username)
        added = await client.post(
            "/api/v1/members",
            json={"username": username, "dept_id": dept_id},
            headers=_h(admin_token),
        )
        assert added.status_code == 201, added.text
        uids[username] = added.json()["data"]["user_id"]

        roles = (await client.get("/api/v1/roles", headers=_h(admin_token))).json()["data"]
        role_id = next(r["id"] for r in roles if r["code"] == role_code)
        await client.post(
            f"/api/v1/members/{added.json()['data']['id']}/roles",
            json={"role_id": role_id},
            headers=_h(admin_token),
        )

    # 登录（此时 token 已带正确的 data_scope / dept_id）
    for who in IDENTITIES:
        sc.tokens[who] = await _login_in(client, who, sc.tenant_id)

    # 直接写库种资源：owner_id / dept_id 必须精确可控，
    # 走 API 建的话 owner 只能是创建者本人。
    dept_ids = {"dev": dev_id, "mkt": mkt_id, None: None}
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=sc.tenant_id, data_scope=DataScope.ALL))
    try:
        project_repo = ProjectRepository(session)
        task_repo = TaskRepository(session)
        for code, (owner, dept) in SEEDS.items():
            project = project_repo.create(
                code=code,
                name=f"项目{code}",
                status="ACTIVE",
                owner_id=uids[owner],
                dept_id=dept_ids[dept],
            )
            await session.flush()
            sc.project_ids[code] = project.id

            task = task_repo.create(
                project_id=project.id,
                title=f"任务{code}",
                status="TODO",
                priority="MEDIUM",
                owner_id=uids[owner],
                dept_id=dept_ids[dept],
            )
            await session.flush()
            sc.task_ids[code] = task.id
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    # 给每个项目挂一个附件（验证「附件可见性继承挂靠实体」）
    for code in ALL_CODES:
        up = await client.post(
            "/api/v1/attachments",
            data={"biz_type": "project", "biz_id": str(sc.project_ids[code])},
            files={"file": (f"{code}.txt", code.encode(), "text/plain")},
            headers=_h(admin_token),
        )
        assert up.status_code == 201, up.text
        sc.attachment_ids[code] = up.json()["data"]["id"]

    return sc


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """附件落盘换到 tmp 目录，避免污染真实 local_storage/。"""
    from app.services import attachment_service

    backend = LocalStorage(root=tmp_path / "storage")
    monkeypatch.setattr(attachment_service, "storage", backend)
    return backend


@pytest_asyncio.fixture
async def scenario(client, db_session_factory, tmp_storage):
    return await _build_scenario(client, db_session_factory)


# ======================================================================
# 列表接口：返回集合必须精确等于期望
# ======================================================================
@pytest.mark.parametrize("who", IDENTITIES)
async def test_project_list_visibility(client, scenario, who):
    resp = await client.get("/api/v1/projects", headers=scenario.headers(who))
    assert resp.status_code == 200, resp.text
    codes = {p["code"] for p in resp.json()["data"]["items"]}
    assert codes == EXPECTED_VISIBLE[who], f"{who} 应看到 {EXPECTED_VISIBLE[who]}，实际 {codes}"


@pytest.mark.parametrize("who", IDENTITIES)
async def test_task_list_visibility(client, scenario, who):
    resp = await client.get("/api/v1/tasks", headers=scenario.headers(who))
    assert resp.status_code == 200, resp.text
    titles = {t["title"] for t in resp.json()["data"]["items"]}
    expected = {f"任务{c}" for c in EXPECTED_VISIBLE[who]}
    assert titles == expected, f"{who} 应看到 {expected}，实际 {titles}"


# ======================================================================
# 详情：范围内 200 / 范围外 404
# ======================================================================
@pytest.mark.parametrize("who", IDENTITIES)
async def test_project_detail_scope(client, scenario, who):
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.get(
            f"/api/v1/projects/{scenario.project_ids[code]}", headers=scenario.headers(who)
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 访问项目 {code}：期望 {'200' if should_see else '404'}，实际 {resp.status_code}"
        )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_task_detail_scope(client, scenario, who):
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.get(
            f"/api/v1/tasks/{scenario.task_ids[code]}", headers=scenario.headers(who)
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 访问任务 {code}：期望 {'200' if should_see else '404'}，实际 {resp.status_code}"
        )


# ======================================================================
# 写操作：范围外一律 404（不能改看不到的东西）
# ======================================================================
@pytest.mark.parametrize("who", IDENTITIES)
async def test_project_update_scope(client, scenario, who):
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.patch(
            f"/api/v1/projects/{scenario.project_ids[code]}",
            json={"name": f"被{who}改名"},
            headers=scenario.headers(who),
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 更新项目 {code}：期望 {'200' if should_see else '404'}，实际 {resp.status_code}"
        )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_task_update_scope(client, scenario, who):
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.patch(
            f"/api/v1/tasks/{scenario.task_ids[code]}",
            json={"priority": "URGENT"},
            headers=scenario.headers(who),
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 更新任务 {code}：期望 {'200' if should_see else '404'}，实际 {resp.status_code}"
        )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_task_status_change_scope(client, scenario, who):
    """状态流转同样受数据范围约束——改状态也是一种写操作。"""
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.patch(
            f"/api/v1/tasks/{scenario.task_ids[code]}/status",
            json={"status": "IN_PROGRESS"},
            headers=scenario.headers(who),
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 流转任务 {code}：期望 {'200' if should_see else '404'}，实际 {resp.status_code}"
        )


# ======================================================================
# 评论：可见性继承任务
# ======================================================================
@pytest.mark.parametrize("who", IDENTITIES)
async def test_comment_read_scope(client, scenario, who):
    """★ 评论表不带 DataScopedMixin，可见性**必须**继承任务。

    若服务端图省事直接查评论表，同租户的 SELF 成员就能读到别人任务的讨论。
    """
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.get(
            f"/api/v1/tasks/{scenario.task_ids[code]}/comments", headers=scenario.headers(who)
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 读任务 {code} 的评论：期望 {'200' if should_see else '404'}，"
            f"实际 {resp.status_code}"
        )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_comment_write_scope(client, scenario, who):
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.post(
            f"/api/v1/tasks/{scenario.task_ids[code]}/comments",
            json={"content": "越权测试"},
            headers=scenario.headers(who),
        )
        assert (resp.status_code == 201) is should_see, (
            f"{who} 评论任务 {code}：期望 {'201' if should_see else '404'}，实际 {resp.status_code}"
        )


# ======================================================================
# 附件：可见性同样必须继承挂靠实体
# ======================================================================
@pytest.mark.parametrize("who", IDENTITIES)
async def test_attachment_list_scope(client, scenario, who):
    """★ 附件表也不带 DataScopedMixin，列表**必须**按挂靠实体过滤。

    这是曾经的真漏洞：`list_attachments` 只查附件表（仅租户过滤），
    SELF 成员可以列出同租户别人项目的附件。

    断言写法故意留了两种合规实现：返回 404（先校验实体可见性）
    或返回空列表（按可见实体过滤）。**核心是绝不返回范围外的附件**。
    """
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.get(
            f"/api/v1/attachments?biz_type=project&biz_id={scenario.project_ids[code]}",
            headers=scenario.headers(who),
        )
        if should_see:
            assert resp.status_code == 200, f"{who} 应能列出项目 {code} 的附件：{resp.text}"
            ids = {a["id"] for a in resp.json()["data"]}
            assert ids == {scenario.attachment_ids[code]}, (
                f"{who} 列出项目 {code} 的附件应为该项目的附件，实际 {ids}"
            )
        else:
            visible_ids: set[int] = set()
            if resp.status_code == 200:
                visible_ids = {a["id"] for a in resp.json()["data"]}
            assert resp.status_code == 404 or visible_ids == set(), (
                f"{who} 不该拿到项目 {code} 的附件，实际 status={resp.status_code} ids={visible_ids}"
            )
            assert scenario.attachment_ids[code] not in visible_ids, (
                f"★ 越权：{who} 拿到了范围外项目 {code} 的附件"
            )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_attachment_download_scope(client, scenario, who):
    """★ 下载同样要按挂靠实体过滤——知道 id 不等于有权下载。"""
    for code in ALL_CODES:
        should_see = code in EXPECTED_VISIBLE[who]
        resp = await client.get(
            f"/api/v1/attachments/{scenario.attachment_ids[code]}/download",
            headers=scenario.headers(who),
        )
        assert (resp.status_code == 200) is should_see, (
            f"{who} 下载项目 {code} 的附件：期望 {'200' if should_see else '404'}，"
            f"实际 {resp.status_code}"
        )


@pytest.mark.parametrize("who", IDENTITIES)
async def test_attachment_list_without_filter_is_bounded(client, scenario, who):
    """不带过滤参数时也应只返回「自己有权看到的实体」的附件。

    这条最容易漏：带了 biz_id 会想着去校验，不带反而直接全表扫。
    """
    resp = await client.get("/api/v1/attachments", headers=scenario.headers(who))
    assert resp.status_code == 200, resp.text
    got = {a["id"] for a in resp.json()["data"]}
    allowed = {scenario.attachment_ids[c] for c in EXPECTED_VISIBLE[who]}
    assert got == allowed, f"{who} 应看到附件 {allowed}，实际 {got}"
