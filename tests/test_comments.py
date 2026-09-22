"""评论与 @提及测试（第 4 周步骤 3）。

★ 提及解析是本文件的重头，因为它是**权限边界**而不只是字符串处理：
  客户端不能指定「通知谁」，只能通过写出 `@名字` 来提及，且服务端只认
  当前租户的 ACTIVE 成员。所以边界用例（邮箱里的 @、跨租户同名用户、
  已停用成员、重复提及、@ 自己）都要逐一钉住。

★ 通知规则也是重点：**被 @ 的人只收一条**（提及优先于评论），
  执行人若同时被 @ 不重复收。一次操作产生两条通知是噪音。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, MemberStatus
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.repositories.tenant import TenantMemberRepository
from app.services.comment_service import extract_mentions
from app.services.notification_service import list_notifications

PASSWORD = "a-long-enough-passphrase"


# ----------------------------------------------------------------------
# 提及解析（纯函数，边界最多）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("@张三 看一下", ["张三"]),
        ("请看 @张三", ["张三"]),
        ("@张三，这个接口明天能联调吗？", ["张三"]),
        # 连写提及必须两个都认——这正是不能用 `(?<![\w])` 排除邮箱的原因
        ("@张三@李四", ["张三", "李四"]),
        ("@zhang_san 和 @li-si", ["zhang_san", "li-si"]),
        ("（@张三）", ["张三"]),
        ("@张三 @张三 @张三", ["张三"]),
        ("没有提及", []),
        ("", []),
    ],
)
def test_extract_mentions_basic(content, expected):
    assert extract_mentions(content) == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("联系 me@example.com 吧", ["example.com"]),
        ("见 https://user@host/path", ["host"]),
        ("邮箱：a.b@corp.com.cn", ["corp.com.cn"]),
    ],
)
def test_extract_mentions_email_produces_harmless_candidate(content, expected):
    """邮箱里的 @ **会**解析出一个候选名——这是**有意**的行为，不是漏判。

    ★ 取舍：加 `(?<![\\w])` 后顾断言能排除邮箱，但会连带排除
      `@张三@李四` 这种连写提及（第二个 `@` 前面正是汉字），
      造成**静默漏发通知**。

      而邮箱误判出的候选名（`example.com`）不可能匹配到任何真实成员，
      `resolve_usernames` 查不到就跳过——**完全无害**。

      「漏发一条真实通知」比「多解析一个解析不到的名字」严重得多，
      所以这里刻意选择宽松解析。真正的防线在 `resolve_usernames`
      （只认本租户 ACTIVE 成员），不在正则。
    """
    assert extract_mentions(content) == expected


def test_extract_mentions_stops_at_punctuation():
    """标点必须截断名称，否则「@张三，你好」会解析出「张三，你好」。"""
    assert extract_mentions("@张三,你好") == ["张三"]
    assert extract_mentions("@张三。这是结论") == ["张三"]
    assert extract_mentions("@张三/@李四") == ["张三", "李四"]
    assert extract_mentions("@张三）后续") == ["张三"]


def test_extract_mentions_keeps_order_and_dedupes():
    assert extract_mentions("@b @a @b @c") == ["b", "a", "c"]


# ----------------------------------------------------------------------
# 接口装置
# ----------------------------------------------------------------------
async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _setup_task_with_members(client):
    """建一个「管理员 + 一个成员」的团队，成员被执行任务。

    返回 (admin_token, member_token, member_uid, tenant_id, task_id)
    """
    admin = await _register(client, "admin-user")
    tenant_id, admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    admin_token = admin["token"]["access_token"]

    await _register(client, "mate-user")
    added = await client.post(
        "/api/v1/members", json={"username": "mate-user"}, headers=_h(admin_token)
    )
    assert added.status_code == 201, added.text
    mate_uid = added.json()["data"]["user_id"]

    login = await client.post(
        "/api/v1/auth/login",
        json={"username": "mate-user", "password": PASSWORD, "tenant_id": tenant_id},
    )
    mate_token = login.json()["data"]["access_token"]

    project = await client.post(
        "/api/v1/projects", json={"code": "P-1", "name": "项目一"}, headers=_h(admin_token)
    )
    pid = project.json()["data"]["id"]

    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "任务标题", "assignee_id": mate_uid},
        headers=_h(admin_token),
    )
    assert task.status_code == 201, task.text
    return admin_token, mate_token, mate_uid, tenant_id, task.json()["data"]["id"], admin_uid


async def _notes_of(db_session_factory, *, tenant_id: int, user_id: int) -> list:
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        items, _total = await list_notifications(
            session, user_id=user_id, unread_only=False, page=1, page_size=50
        )
        return items
    finally:
        restore_context(previous)
        await session.close()


async def _clear_notes(db_session_factory, *, tenant_id: int, user_id: int) -> None:
    """把已有通知标记已读，避免与前面的用例互相干扰。

    注意：这里不能直接删通知——列表只返回未读以外的全部，
    所以用「按事件类型筛选」的方式在各用例里断言，而不是清空。
    """
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        from app.services import notification_service

        await notification_service.mark_all_read(session, user_id=user_id)
    finally:
        restore_context(previous)
        await session.close()


# ----------------------------------------------------------------------
# 发表评论
# ----------------------------------------------------------------------
async def test_create_and_list_comments(client):
    admin_token, _mate, _uid, _tid, task_id, _admin_uid = await _setup_task_with_members(client)

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"content": "第一条评论"}, headers=_h(admin_token)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["content"] == "第一条评论"

    resp = await client.get(f"/api/v1/tasks/{task_id}/comments", headers=_h(admin_token))
    assert [c["content"] for c in resp.json()["data"]] == ["第一条评论"]


async def test_comment_requires_auth(client):
    assert (await client.get("/api/v1/tasks/1/comments")).status_code == 401
    assert (await client.post("/api/v1/tasks/1/comments", json={"content": "x"})).status_code == 401


async def test_comment_on_cross_tenant_task_returns_404(client):
    _admin, _mate, _uid, _tid, task_id, _a = await _setup_task_with_members(client)
    outsider = await _register(client, "outsider-user")

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "越权评论"},
        headers=_h(outsider["token"]["access_token"]),
    )
    assert resp.status_code == 404

    resp = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=_h(outsider["token"]["access_token"])
    )
    assert resp.status_code == 404, "读评论也要先过任务可见性"


# ----------------------------------------------------------------------
# 提及 → 通知
# ----------------------------------------------------------------------
async def test_mention_creates_notification_for_mentioned_member(client, db_session_factory):
    admin_token, _mate, mate_uid, tenant_id, task_id, _a = await _setup_task_with_members(client)
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "@mate-user 这个接口明天能联调吗？"},
        headers=_h(admin_token),
    )
    assert resp.status_code == 201, resp.text

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    mentioned = [n for n in notes if n.event == "task.mentioned"]
    assert len(mentioned) == 1, f"应恰好收到一条提及通知，实际 {[n.event for n in notes]}"
    # 标题由模板渲染：含任务名 + 正文摘要（摘要里自然会出现 @名字）
    assert "任务标题" in mentioned[0].title
    assert mentioned[0].payload["task_id"] == task_id
    assert mentioned[0].payload["mentioned_by"] != mate_uid, "触发者应是 admin 而不是被提及者"


async def test_mention_suppresses_duplicate_comment_notification(client, db_session_factory):
    """★ 被 @ 的执行人只收一条（提及），不再收「新评论」。

    一次操作两条通知是噪音，且两条内容几乎一样。
    """
    admin_token, _mate, mate_uid, tenant_id, task_id, _a = await _setup_task_with_members(client)
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "@mate-user 看下这个"},
        headers=_h(admin_token),
    )

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    events = [n.event for n in notes]
    assert events == ["task.mentioned"], f"应只收到提及通知，实际 {events}"


async def test_comment_without_mention_notifies_assignee_once(client, db_session_factory):
    """没 @ 时，执行人正常收到一条「新评论」。"""
    admin_token, _mate, mate_uid, tenant_id, task_id, _a = await _setup_task_with_members(client)
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"content": "没有提及"}, headers=_h(admin_token)
    )

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert [n.event for n in notes] == ["task.commented"]


async def test_comment_author_does_not_notify_self(client, db_session_factory):
    """★ 自己评论自己的任务：不该给自己发通知。

    执行人评论时既不该收到 commented（作者是自己），也不该收到 mentioned（@ 自己被排除）。
    """
    admin = await _register(client, "solo-user")
    tenant_id, uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]

    project = await client.post(
        "/api/v1/projects", json={"code": "SOLO", "name": "独自的项目"}, headers=_h(token)
    )
    pid = project.json()["data"]["id"]
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": pid, "title": "自己的任务", "assignee_id": uid},
        headers=_h(token),
    )
    tid = task.json()["data"]["id"]
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=uid)

    resp = await client.post(
        f"/api/v1/tasks/{tid}/comments",
        json={"content": "@solo-user 自己提醒自己"},
        headers=_h(token),
    )
    assert resp.status_code == 201, resp.text

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=uid)
    assert notes == [], f"作者不该收到自己产生的通知，实际 {[n.event for n in notes]}"


async def test_mention_of_non_member_is_ignored(client, db_session_factory):
    """@ 一个非本团队成员：不报错，也不产生通知。

    关键是不能解析到**别的租户**的同名用户——那会真的给外人发通知。
    """
    admin_token, _mate, mate_uid, tenant_id, task_id, _a = await _setup_task_with_members(client)
    # 另一个租户里存在同名用户
    await _register(client, "other-user")
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "@other-user 你不是这个团队的"},
        headers=_h(admin_token),
    )
    assert resp.status_code == 201, "@ 非成员不该让评论失败"

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert [n.event for n in notes] == ["task.commented"], "不该把通知发给外租户的同名用户"


async def test_mention_skips_disabled_member(client, db_session_factory):
    """被停用的成员不该收到提及通知。"""
    admin_token, _mate, mate_uid, tenant_id, task_id, _a = await _setup_task_with_members(client)
    await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)

    # 把 mate 的成员关系停用
    session = db_session_factory()
    previous = snapshot_context()
    try:
        set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
        member = await TenantMemberRepository(session).get_membership(mate_uid)
        member.status = str(MemberStatus.DISABLED)
        await session.commit()
    finally:
        restore_context(previous)
        await session.close()

    await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "@mate-user 还能看到吗"},
        headers=_h(admin_token),
    )

    notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=mate_uid)
    assert notes == [], "已停用成员不该收到通知"


async def test_mention_multiple_members_creates_one_each(client, db_session_factory):
    """一次 @ 多个成员，各自收到一条（且去重）。"""
    admin = await _register(client, "admin-user")
    tenant_id, _admin_uid = admin["tenant"]["id"], admin["user"]["id"]
    token = admin["token"]["access_token"]

    uids = []
    for name in ("mate-a", "mate-b"):
        await _register(client, name)
        added = await client.post("/api/v1/members", json={"username": name}, headers=_h(token))
        uids.append(added.json()["data"]["user_id"])

    project = await client.post(
        "/api/v1/projects", json={"code": "MP", "name": "多提及"}, headers=_h(token)
    )
    task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project.json()["data"]["id"], "title": "任务", "assignee_id": uids[0]},
        headers=_h(token),
    )
    task_id = task.json()["data"]["id"]

    for uid in uids:
        await _clear_notes(db_session_factory, tenant_id=tenant_id, user_id=uid)

    resp = await client.post(
        f"/api/v1/tasks/{task_id}/comments",
        json={"content": "@mate-a @mate-b @mate-a 一起看下"},
        headers=_h(token),
    )
    assert resp.status_code == 201, resp.text

    for uid in uids:
        notes = await _notes_of(db_session_factory, tenant_id=tenant_id, user_id=uid)
        mentioned = [n for n in notes if n.event == "task.mentioned"]
        assert len(mentioned) == 1, (
            f"user {uid} 应恰好收到一条提及，实际 {[n.event for n in notes]}"
        )


async def test_comments_are_tenant_scoped(client, db_session_factory):
    """评论列表不跨租户。"""
    admin_token, _mate, _uid, _tid, task_id, _a = await _setup_task_with_members(client)
    await client.post(
        f"/api/v1/tasks/{task_id}/comments", json={"content": "内部讨论"}, headers=_h(admin_token)
    )

    outsider = await _register(client, "outsider2-user")
    resp = await client.get(
        f"/api/v1/tasks/{task_id}/comments", headers=_h(outsider["token"]["access_token"])
    )
    assert resp.status_code == 404
