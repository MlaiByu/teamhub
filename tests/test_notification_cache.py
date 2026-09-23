"""热点接口缓存测试（第 8 周步骤 3）。

★ 被缓存的是「未读通知数」——前端角标的轮询接口，真正的热点读。

★★ 本文件最核心的一条：**同一个 user_id 在两个租户里不能串缓存**
  （PROJECT-PLAN 风险 10）。

  `User` 是**全局表**，同一个账号可以加入多家公司。所以
  「user_id = 42 在租户 1 有 2 条未读、在租户 2 有 5 条未读」是完全正常的场景。
  缓存 key 若不带租户前缀，两边会互相覆盖：

      用户在 A 公司看到 B 公司的未读数（或者角标永远是 0/错值）

  而且**不会报错**——这才是最危险的。所以这里用「同 user_id 双租户」
  这个真实场景把它钉死。

★ 另一条容易漏的：**写时失效**。缓存加了但忘了在写路径删除，
  角标会一直显示旧值（要等 TTL 到期）。本文件用「直接改库绕过 service」
  来证明真的命中了缓存，再用真实写操作证明失效是有效的。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.constants import DataScope
from app.core.db.context import RequestContext, reset_context, set_context
from app.models import Notification, Tenant, User
from app.services import notification_service

pytestmark = pytest.mark.asyncio


async def _seed_tenant_with_notifications(
    db_session_factory,
    *,
    tenant_code: str,
    username: str,
    unread: int,
    read: int = 0,
) -> tuple[int, int]:
    """建一个租户 + 用户 + 若干通知。返回 (tenant_id, user_id)。"""
    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=None, data_scope=DataScope.ALL))
        tenant = Tenant(code=tenant_code, name=tenant_code, status="ACTIVE", max_members=10)
        session.add(tenant)
        await session.flush()

        # 复用同一个全局用户（模拟「同一账号加入多个租户」）
        existing = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is None:
            user = User(username=username, password_hash="x", is_active=True)
            session.add(user)
            await session.flush()
        else:
            user = existing

        for i in range(unread):
            session.add(
                Notification(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    type="member.joined",
                    payload={"tenant_name": tenant_code, "n": i},
                    read_at=None,
                )
            )
        for i in range(read):
            session.add(
                Notification(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    type="member.joined",
                    payload={"tenant_name": tenant_code, "r": i},
                    read_at=datetime(2026, 1, 1),
                )
            )
        await session.commit()
        return tenant.id, user.id
    finally:
        reset_context()
        await session.close()


async def _count(db_session_factory, *, tenant_id: int, user_id: int) -> int:
    """在指定租户上下文下读未读数。"""
    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
        return await notification_service.count_unread(session, user_id=user_id)
    finally:
        reset_context()
        await session.close()


async def _add_unread_directly(db_session_factory, *, tenant_id: int, user_id: int) -> None:
    """**绕过 service** 直接写库（不触发缓存失效），用于证明「读到了缓存」。"""
    session = db_session_factory()
    try:
        session.add(
            Notification(
                tenant_id=tenant_id,
                user_id=user_id,
                type="task.assigned",
                payload={"task_title": "直插"},
                read_at=None,
            )
        )
        await session.commit()
    finally:
        await session.close()


# ======================================================================
# 基本缓存行为
# ======================================================================
async def test_unread_count_returns_correct_value(db_session_factory, _schema):
    tenant_id, user_id = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=2, read=3
    )
    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 2


async def test_unread_count_is_served_from_cache(db_session_factory, _schema):
    """★ 证明真的命中了缓存：绕过 service 改库后，读到的仍是缓存里的旧值。"""
    tenant_id, user_id = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=2
    )
    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 2

    # 直接插一条未读（不经过 service，所以不会失效缓存）
    await _add_unread_directly(db_session_factory, tenant_id=tenant_id, user_id=user_id)

    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 2, (
        "应命中缓存返回旧值；若返回 3 说明缓存根本没生效"
    )


# ======================================================================
# ★★ 跨租户隔离
# ======================================================================
async def test_same_user_in_two_tenants_does_not_share_cache(db_session_factory, _schema):
    """★★ 同一个 user_id 在两个租户的未读数必须互不干扰。

    `User` 是全局表——同一账号加入两家公司是正常场景。
    缓存 key 漏了租户前缀的话，两边会互相覆盖/命中，
    用户在 A 公司看到 B 公司的角标数字，而且不报错。
    """
    t1, u1 = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=2
    )
    t2, u2 = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="globex", username="guotao", unread=5
    )

    assert u1 == u2, "前提：这是同一个全局用户，只是分属两个租户"

    assert await _count(db_session_factory, tenant_id=t1, user_id=u1) == 2
    assert await _count(db_session_factory, tenant_id=t2, user_id=u2) == 5

    # 再读一轮（此时两边都已缓存），值仍必须各自正确
    assert await _count(db_session_factory, tenant_id=t1, user_id=u1) == 2
    assert await _count(db_session_factory, tenant_id=t2, user_id=u2) == 5


async def test_cache_key_includes_tenant_and_user(db_session_factory, _schema):
    """直接钉住 key 形态：租户与用户都要在里面。"""
    from app.core import cache

    key = cache.cache_key(3, "notifications", "unread", 42)
    assert key == "cache:tenant:3:notifications:unread:42"

    t1, u = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=1
    )
    await _count(db_session_factory, tenant_id=t1, user_id=u)
    from app.core.redis import get_redis

    assert await get_redis().exists(cache.cache_key(t1, "notifications", "unread", u)) == 1


# ======================================================================
# 写时失效
# ======================================================================
async def test_mark_all_read_invalidates_cache(db_session_factory, _schema):
    """★ 全部标为已读后，未读数必须立刻变 0（不能等 TTL）。"""
    tenant_id, user_id = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=4
    )
    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 4

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
        await notification_service.mark_all_read(session, user_id=user_id)
    finally:
        reset_context()
        await session.close()

    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 0


async def test_mark_one_read_invalidates_cache(db_session_factory, _schema):
    tenant_id, user_id = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=3
    )
    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 3

    # 取一条通知 id
    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
        nid = (
            await session.execute(
                select(Notification.id).where(Notification.user_id == user_id).limit(1)
            )
        ).scalar_one()
        await notification_service.mark_read(session, user_id=user_id, notification_id=nid)
    finally:
        reset_context()
        await session.close()

    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 2


async def test_emit_invalidates_cache(db_session_factory, _schema):
    """★ 新通知落库后（emit），未读数必须立刻 +1。"""
    from app.core.constants import MemberStatus
    from app.realtime.events import EventType
    from app.repositories.tenant import TenantMemberRepository
    from app.repositories.user import UserRepository

    tenant_id, user_id = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=1
    )
    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 1

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.ALL))
        # emit 要求目标是本租户 ACTIVE 成员
        user = await UserRepository(session).get(user_id)
        TenantMemberRepository(session).create(
            user_id=user.id, status=str(MemberStatus.ACTIVE), dept_id=None
        )
        await session.commit()

        await notification_service.emit(
            session,
            event_type=EventType.TASK_ASSIGNED,
            target_user_id=user_id,
            actor_id=None,
            task_id=1,
            task_title="缓存失效测试",
            project_id=1,
            project_name="P",
            assigned_by=None,
            reassigned=False,
        )
    finally:
        reset_context()
        await session.close()

    assert await _count(db_session_factory, tenant_id=tenant_id, user_id=user_id) == 2, (
        "emit 后未读数应立刻更新（缓存已被失效）"
    )


async def test_invalidate_of_one_tenant_does_not_clear_another(db_session_factory, _schema):
    """★ 失效缓存也不能跨租户——否则 A 租户的写操作会把 B 租户的缓存打掉。"""
    t1, u1 = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="acme", username="guotao", unread=2
    )
    t2, u2 = await _seed_tenant_with_notifications(
        db_session_factory, tenant_code="globex", username="guotao", unread=7
    )

    # 两个租户都先建立缓存
    assert await _count(db_session_factory, tenant_id=t1, user_id=u1) == 2
    assert await _count(db_session_factory, tenant_id=t2, user_id=u2) == 7

    # 只失效租户 1
    await notification_service.invalidate_unread_cache(tenant_id=t1, user_id=u1)

    from app.core import cache
    from app.core.redis import get_redis

    r = get_redis()
    assert await r.exists(cache.cache_key(t1, "notifications", "unread", u1)) == 0
    assert await r.exists(cache.cache_key(t2, "notifications", "unread", u2)) == 1, (
        "租户 2 的缓存不该被租户 1 的失效操作打掉"
    )
