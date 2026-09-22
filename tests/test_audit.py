"""审计查询接口测试（第 5 周步骤 3）。

★ 三条边界：
  1. **只给管理员**：普通成员（MEMBER）查审计 → 403
  2. **租户隔离**：管理员也只能看到自己租户的日志（钩子注入）
  3. **过滤与分页**：entity_type / action / user_id 组合过滤，total 正确
"""

from __future__ import annotations

from app.core.constants import DataScope
from app.core.db.context import RequestContext, restore_context, set_context, snapshot_context
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


async def _login_in(client, username: str, tenant_id: int) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD, "tenant_id": tenant_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _seed_audit(
    db_session_factory, *, tenant_id: int, user_id: int, action: str, entity_type: str = "task"
) -> None:
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
    try:
        await audit_service.record(
            session, action=action, entity_type=entity_type, entity_id=1, detail={"x": 1}
        )
    finally:
        restore_context(previous)
        await session.close()


async def test_admin_can_read_audit_logs(client, db_session_factory):
    data = await _register(client, "admin-user")
    tenant_id, uid = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]

    await _seed_audit(db_session_factory, tenant_id=tenant_id, user_id=uid, action="task.create")

    resp = await client.get("/api/v1/audit-logs", headers=_h(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["action"] == "task.create"
    assert body["items"][0]["user_id"] == uid


async def test_member_cannot_read_audit_logs(client, db_session_factory):
    """★ 普通成员查审计 → 403。"""
    admin = await _register(client, "admin-user")
    tenant_id = admin["tenant"]["id"]
    admin_token = admin["token"]["access_token"]

    await _register(client, "mate-user")
    await client.post("/api/v1/members", json={"username": "mate-user"}, headers=_h(admin_token))
    member_token = await _login_in(client, "mate-user", tenant_id)

    resp = await client.get("/api/v1/audit-logs", headers=_h(member_token))
    assert resp.status_code == 403, resp.text


async def test_audit_logs_require_auth(client):
    assert (await client.get("/api/v1/audit-logs")).status_code == 401


async def test_audit_logs_are_tenant_scoped(client, db_session_factory):
    """★ 管理员也只能看到自己租户的日志。"""
    alice = await _register(client, "alice")
    bob = await _register(client, "bob")

    await _seed_audit(
        db_session_factory,
        tenant_id=alice["tenant"]["id"],
        user_id=alice["user"]["id"],
        action="project.create",
        entity_type="project",
    )

    # bob 看不到 alice 的日志
    resp = await client.get("/api/v1/audit-logs", headers=_h(bob["token"]["access_token"]))
    assert resp.json()["data"]["total"] == 0

    # 反向：alice 自己看得到
    resp = await client.get("/api/v1/audit-logs", headers=_h(alice["token"]["access_token"]))
    assert resp.json()["data"]["total"] == 1


async def test_filter_by_action_and_entity(client, db_session_factory):
    data = await _register(client, "admin-user")
    tenant_id, uid = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]

    await _seed_audit(
        db_session_factory,
        tenant_id=tenant_id,
        user_id=uid,
        action="task.create",
        entity_type="task",
    )
    await _seed_audit(
        db_session_factory,
        tenant_id=tenant_id,
        user_id=uid,
        action="project.create",
        entity_type="project",
    )

    resp = await client.get("/api/v1/audit-logs?action=task.create", headers=_h(token))
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["items"][0]["action"] == "task.create"

    resp = await client.get("/api/v1/audit-logs?entity_type=project", headers=_h(token))
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["items"][0]["entity_type"] == "project"


async def test_pagination(client, db_session_factory):
    data = await _register(client, "admin-user")
    tenant_id, uid = data["tenant"]["id"], data["user"]["id"]
    token = data["token"]["access_token"]

    for _ in range(5):
        await _seed_audit(
            db_session_factory, tenant_id=tenant_id, user_id=uid, action="task.create"
        )

    resp = await client.get("/api/v1/audit-logs?page=1&page_size=2", headers=_h(token))
    body = resp.json()["data"]
    assert len(body["items"]) == 2
    assert body["total"] == 5, "total 必须是全部匹配数，不是当前页条数"


async def test_audit_is_written_for_project_create(client, db_session_factory):
    """端到端：真实创建项目 → 审计可查。"""
    data = await _register(client, "admin-user")
    token = data["token"]["access_token"]

    await client.post(
        "/api/v1/projects", json={"code": "AUD-1", "name": "审计项目"}, headers=_h(token)
    )

    resp = await client.get("/api/v1/audit-logs", headers=_h(token))
    actions = {item["action"] for item in resp.json()["data"]["items"]}
    assert "project.create" in actions
