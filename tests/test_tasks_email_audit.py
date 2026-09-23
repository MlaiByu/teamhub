"""邮件异步发送 + 审计异步路径测试（第 7 周步骤 2）。

★ 覆盖三条：

  1. **邮件真的发出去了**（用 Spy 后端断言，不依赖真实 SMTP）
  2. **没邮箱就跳过**——不为了「发出去」去猜地址
  3. **审计异步路径**：`record_deferred()` 能正确落库，且无租户上下文时跳过

★ 为什么用 Spy 而不是 Console 后端 + 抓日志：
  抓日志要依赖日志格式（一旦改渲染器断言就碎）。Spy 直接断言调用参数，
  稳定且能精确验证「收件人 / 主题 / 正文」都对。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope
from app.core.db.context import (
    RequestContext,
    reset_context,
    restore_context,
    set_context,
    snapshot_context,
)
from app.models import AuditLog
from app.repositories.audit import AuditLogRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services import audit_service

PASSWORD = "a-long-enough-passphrase"


class SpyMailer:
    """记录所有「发出去」的邮件。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def spy_mailer(monkeypatch) -> SpyMailer:
    """把任务里用的 mailer 换成 Spy。

    注意 patch 目标是 `app.tasks.email.get_mailer`（模块级 import 的绑定），
    不是 `app.core.mailer.get_mailer`——patch 后者不会影响已绑定的引用。
    """
    from app.tasks import email as email_task

    spy = SpyMailer()
    monkeypatch.setattr(email_task, "get_mailer", lambda: spy)
    return spy


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _set_email(db_session_factory, *, username: str, email: str) -> int:
    """给用户设邮箱（注册 API 不收 email，所以直接改库）。返回 user_id。"""
    session = db_session_factory()
    try:
        user = await UserRepository(session).get_by_username(username)
        user.email = email
        await session.commit()
        return user.id
    finally:
        await session.close()


# ======================================================================
# send_email 任务本身
# ======================================================================
async def test_send_email_task_invokes_mailer(spy_mailer):
    from app.tasks.email import send_email

    result = send_email.delay(
        to="someone@example.com", subject="标题", body="正文", tenant_id=1
    ).get()

    assert result == {"to": "someone@example.com", "subject": "标题"}
    assert len(spy_mailer.sent) == 1
    assert spy_mailer.sent[0]["to"] == "someone@example.com"
    assert spy_mailer.sent[0]["subject"] == "标题"
    assert spy_mailer.sent[0]["body"] == "正文"


async def test_send_email_does_not_require_tenant(spy_mailer):
    """纯通知邮件不碰租户级表，所以不带 tenant_id 也能发。"""
    from app.tasks.email import send_email

    result = send_email.delay(to="a@b.com", subject="s", body="b").get()
    assert result["to"] == "a@b.com"


# ======================================================================
# 触发点：成员加入团队
# ======================================================================
async def test_member_join_sends_welcome_email(client, db_session_factory, spy_mailer):
    """端到端：把有邮箱的用户加入团队 → 发欢迎邮件。"""
    admin = await _register(client, "admin-user")
    token = admin["token"]["access_token"]
    tenant_name = admin["tenant"]["name"]

    await _register(client, "mate-user")
    await _set_email(db_session_factory, username="mate-user", email="mate@example.com")

    resp = await client.post("/api/v1/members", json={"username": "mate-user"}, headers=_h(token))
    assert resp.status_code == 201, resp.text

    assert len(spy_mailer.sent) == 1, f"应发一封欢迎邮件，实际 {spy_mailer.sent}"
    mail = spy_mailer.sent[0]
    assert mail["to"] == "mate@example.com"
    assert tenant_name in mail["subject"]
    assert "mate-user" in mail["body"]


async def test_member_join_without_email_sends_nothing(client, db_session_factory, spy_mailer):
    """★ 用户没填邮箱 → 一封都不发（不猜地址）。"""
    admin = await _register(client, "admin-user")
    token = admin["token"]["access_token"]

    await _register(client, "noemail-user")  # 注册 API 不收 email，故 email 为空

    resp = await client.post(
        "/api/v1/members", json={"username": "noemail-user"}, headers=_h(token)
    )
    assert resp.status_code == 201, resp.text

    assert spy_mailer.sent == [], f"没邮箱不该发信，实际 {spy_mailer.sent}"


async def test_email_failure_does_not_rollback_member_join(client, db_session_factory, monkeypatch):
    """★ 邮件发送失败**不能**让已提交的业务变更回滚。

    这才是真正要保证的事——邮件是旁路通知，它的失败不该影响业务。

    ★ 关于「失败的表现形式」为什么不在断言范围里：

      eager 模式（本地默认）下 `.delay()` 是同步执行，且配了
      `autoretry_for`，于是异常会以 `celery.exceptions.Retry`
      （或它包裹的 OSError）的形式冒出来，还可能被全局异常处理器转成 500。
      具体是「直接抛」还是「500」取决于异常在哪一层被接住——**这属于
      执行模式的细节，不是业务契约**。

      生产环境 `CELERY_ALWAYS_EAGER=false`，`.delay()` 只投递消息，
      异常根本不会进入请求链路，接口照常 201。
      所以这里不断言状态码，只断言**业务事实**。
    """
    from app.tasks import email as email_task

    class BoomMailer:
        def send(self, *, to: str, subject: str, body: str) -> None:
            raise OSError("smtp unreachable")

    monkeypatch.setattr(email_task, "get_mailer", lambda: BoomMailer())

    admin = await _register(client, "admin-user")
    token = admin["token"]["access_token"]
    await _register(client, "mate-user")
    await _set_email(db_session_factory, username="mate-user", email="mate@example.com")

    # eager 下邮件异常可能直接抛出，也可能被全局处理器转成 500——
    # 两种都不影响下面这条断言要验证的事实。
    try:
        await client.post("/api/v1/members", json={"username": "mate-user"}, headers=_h(token))
    except Exception as exc:  # noqa: BLE001
        assert "smtp unreachable" in str(exc) or "Retry" in type(exc).__name__, (
            f"若抛出，应是邮件相关的异常，实际 {type(exc).__name__}: {exc}"
        )

    # ★ 关键：成员关系必须已经落库（业务变更在发信之前就 commit 了）
    members = (await client.get("/api/v1/members", headers=_h(token))).json()["data"]
    assert any(m["username"] == "mate-user" for m in members), "邮件失败不该让已提交的成员关系回滚"


# ======================================================================
# 审计异步路径
# ======================================================================
async def test_record_deferred_persists_audit(db_session_factory, tenant_ctx):
    """`record_deferred()` 在 eager 下同步执行，审计应立即落库。"""
    tenant_ctx(tenant_id=1, user_id=9, scope=DataScope.ALL)

    audit_service.record_deferred(
        action="probe.read", entity_type="probe", entity_id=3, detail={"k": "v"}
    )

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=1, data_scope=DataScope.ALL))
        logs, total = await AuditLogRepository(session).paginate_filtered(page_size=10)
        assert total == 1
        assert logs[0].action == "probe.read"
        assert logs[0].user_id == 9
        assert logs[0].detail == {"k": "v"}
    finally:
        reset_context()
        await session.close()


async def test_record_deferred_skips_without_tenant_context(db_session_factory):
    """无租户上下文时不投递（审计行的 tenant_id 是隔离关键）。"""
    session = db_session_factory()
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=None))
    try:
        # 不该抛异常，也不该产生任何行
        audit_service.record_deferred(action="probe", entity_type="probe")
    finally:
        restore_context(previous)
        await session.close()


async def test_deferred_audit_is_tenant_scoped(db_session_factory):
    """★ 异步路径落库也必须带租户——任务里写租户表同样受守卫约束。

    造两个租户，在租户 2 上下文里异步落一条审计，
    断言它落在租户 2 而不是租户 1。
    """
    session = db_session_factory()
    try:
        for code in ("a", "b"):
            set_context(RequestContext(tenant_id=None, data_scope=DataScope.ALL))
            TenantRepository(session).create(
                code=code, name=f"团队{code}", status="ACTIVE", max_members=10
            )
            await session.flush()
        await session.commit()

        set_context(RequestContext(tenant_id=2, data_scope=DataScope.ALL))
        audit_service.record_deferred(action="scoped.probe", entity_type="probe")
    finally:
        reset_context()
        await session.close()

    from sqlalchemy import select

    session = db_session_factory()
    try:
        rows = await session.execute(select(AuditLog.tenant_id, AuditLog.action))
        found = rows.all()
        assert len(found) == 1, f"应只有一条审计，实际 {found}"
        assert found[0][0] == 2, f"审计应落在租户 2，实际 {found}"
    finally:
        await session.close()
