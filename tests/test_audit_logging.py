"""审计测试（第 5 周步骤 2）。

★ 核心验证两点：
  1. **关键写操作真的落审计**——项目创建/更新、任务创建/改派/状态流转、
     成员加入、角色授予、评论、附件上传，都会产生对应的 audit_log 行。
  2. **审计失败不影响业务**——`record` 绝不抛异常（这是它与普通写操作
     最关键的区别）。

查询接口的隔离在步骤 3 测（test_audit.py），本文件只管落库正确性。
"""

from __future__ import annotations

from app.core.constants import DataScope
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
from app.models import AuditLog
from app.repositories.audit import AuditLogRepository
from app.services import audit_service

PASSWORD = "a-long-enough-passphrase"


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _audit_logs(db_session_factory, *, tenant_id: int) -> list[AuditLog]:
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
    try:
        logs, _total = await AuditLogRepository(session).paginate_filtered(page_size=100)
        return list(logs)
    finally:
        restore_context(previous)
        await session.close()


async def _make_project(client, token: str, code: str = "P-1") -> int:
    resp = await client.post(
        "/api/v1/projects", json={"code": code, "name": f"项目{code}"}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


# ----------------------------------------------------------------------
# 落库正确性
# ----------------------------------------------------------------------
async def test_project_create_and_update_are_audited(client, db_session_factory):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tenant_id = data["tenant"]["id"]

    resp = await client.post(
        "/api/v1/projects", json={"code": "A-1", "name": "审计测试"}, headers=_h(token)
    )
    pid = resp.json()["data"]["id"]
    await client.patch(f"/api/v1/projects/{pid}", json={"name": "改名"}, headers=_h(token))

    logs = await _audit_logs(db_session_factory, tenant_id=tenant_id)
    actions = {log.action for log in logs}
    assert "project.create" in actions, f"应审计项目创建，实际 {actions}"
    assert "project.update" in actions, f"应审计项目更新，实际 {actions}"

    create = next(log for log in logs if log.action == "project.create")
    assert create.entity_type == "project"
    assert create.entity_id == pid
    assert create.detail.get("code") == "A-1"


async def test_task_lifecycle_is_audited(client, db_session_factory):
    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tenant_id = data["tenant"]["id"]
    pid = await _make_project(client, token)

    resp = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "任务"}, headers=_h(token)
    )
    tid = resp.json()["data"]["id"]
    await client.patch(f"/api/v1/tasks/{tid}", json={"priority": "URGENT"}, headers=_h(token))
    await client.patch(
        f"/api/v1/tasks/{tid}/status", json={"status": "IN_PROGRESS"}, headers=_h(token)
    )

    logs = await _audit_logs(db_session_factory, tenant_id=tenant_id)
    actions = {log.action for log in logs}
    assert "task.create" in actions
    assert "task.update" in actions
    assert "task.status_changed" in actions

    sc = next(log for log in logs if log.action == "task.status_changed")
    assert sc.detail["from"] == "TODO"
    assert sc.detail["to"] == "IN_PROGRESS"


async def test_member_join_and_role_assign_are_audited(client, db_session_factory):
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    token = admin["token"]["access_token"]

    await _register(client, "mate-user")
    await client.post("/api/v1/members", json={"username": "mate-user"}, headers=_h(token))

    members = (await client.get("/api/v1/members", headers=_h(token))).json()["data"]
    member_id = next(m["id"] for m in members if m["username"] == "mate-user")
    roles = (await client.get("/api/v1/roles", headers=_h(token))).json()["data"]
    role_id = next(r["id"] for r in roles if r["code"] == "MEMBER")
    await client.post(
        f"/api/v1/members/{member_id}/roles", json={"role_id": role_id}, headers=_h(token)
    )

    logs = await _audit_logs(db_session_factory, tenant_id=tenant_id)
    actions = {log.action for log in logs}
    assert "member.join" in actions
    assert "role.assign" in actions


async def test_comment_and_attachment_are_audited(
    client, db_session_factory, tmp_path, monkeypatch
):
    from app.core.storage import LocalStorage
    from app.services import attachment_service

    monkeypatch.setattr(attachment_service, "storage", LocalStorage(root=tmp_path / "st"))

    data = await _register(client, "guotao")
    token = data["token"]["access_token"]
    tenant_id = data["tenant"]["id"]
    pid = await _make_project(client, token)
    resp = await client.post(
        "/api/v1/tasks", json={"project_id": pid, "title": "任务"}, headers=_h(token)
    )
    tid = resp.json()["data"]["id"]

    await client.post(f"/api/v1/tasks/{tid}/comments", json={"content": "评论"}, headers=_h(token))
    await client.post(
        "/api/v1/attachments",
        data={"biz_type": "task", "biz_id": str(tid)},
        files={"file": ("x.txt", b"hi", "text/plain")},
        headers=_h(token),
    )

    logs = await _audit_logs(db_session_factory, tenant_id=tenant_id)
    actions = {log.action for log in logs}
    assert "comment.create" in actions
    assert "attachment.upload" in actions


# ----------------------------------------------------------------------
# 降级：审计失败不影响业务
# ----------------------------------------------------------------------
async def test_record_swallows_errors(db_session_factory, tenant_ctx):
    """★ record 必须绝不抛异常——审计失败不能让业务操作失败。"""
    tenant_ctx(tenant_id=1, user_id=10, scope=DataScope.ALL)

    # 传一个非法 entity_id（超 BigInteger 范围）会触发数据库错误，
    # record 应吞掉而非抛出
    result = await audit_service.record(
        db_session_factory(),
        action="test.fail",
        entity_type="test",
        entity_id=10**30,  # 超 BigInteger
    )
    assert result is None, "record 失败应返回 None 而不是抛异常"


async def test_record_skips_without_tenant_context(db_session_factory):
    """没有租户上下文时，record 静默跳过（不抛、也不写无主的审计行）。"""
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=None))
    try:
        result = await audit_service.record(
            session, action="test.action", entity_type="test", entity_id=1
        )
    finally:
        restore_context(previous)
        await session.close()

    assert result is None


async def test_record_captures_user_and_ip(db_session_factory, tenant_ctx):
    """record 应记录当前用户与客户端 IP。"""
    from app.core.db import context

    tenant_ctx(tenant_id=1, user_id=42, scope=DataScope.ALL)
    # 直接设 contextvar（它是 ContextVar，不是可 monkeypatch 的普通属性）
    token = context.current_client_ip.set("203.0.113.7")
    try:
        await audit_service.record(
            db_session_factory(), action="test.echo", entity_type="test", entity_id=1
        )
    finally:
        context.current_client_ip.reset(token)

    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=1, data_scope=DataScope.ALL))
    try:
        logs, _total = await AuditLogRepository(session).paginate_filtered(page_size=10)
        assert logs[0].user_id == 42
        assert logs[0].ip == "203.0.113.7"
    finally:
        restore_context(previous)
        await session.close()
