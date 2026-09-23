"""批量写的租户隔离回归测试（隔离地基，PROJECT-PLAN 硬规则 9）。

★ 本文件钉住一个**真实漏洞的修复**，背景必须讲清楚：

  `do_orm_execute` 钩子注入的 `with_loader_criteria` **只作用于实体加载（SELECT）**，
  对 bulk UPDATE / DELETE **完全不生效**。这是实测确认的（不是推测）：

      在租户 1 的上下文里执行 `delete(Token).where(expires_at < cutoff)`
      （不带租户条件）→ **两个租户的行一起被删掉了**

  于是项目原先那句约定——「业务代码不手写 tenant_id，钩子会注入」——
  **只对查询成立**。修复前项目里有 4 处批量写踩了这个坑：

      RefreshTokenRepository.purge_expired   删过期令牌 → 跨租户删除
      RefreshTokenRepository.revoke_chain    登出撤令牌 → 跨租户撤销
      NotificationRepository.mark_one_read   标记已读   → 跨租户标记
      NotificationRepository.mark_all_read   全部已读   → 跨租户清空未读

  对策不是「逐处记得手写 tenant_id」（那还会再漏），而是在
  `TenantAwareRepository` 上提供 `bulk_update()` / `bulk_delete()`，
  把租户条件固化进封装，调用方**没有机会漏写**。

★ 所以本文件的断言重点是「**另一个租户的数据一行都不能被动到**」，
  而不是「操作成功了」——后者在漏洞存在时**同样会通过**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.constants import DataScope
from app.core.db.context import (
    RequestContext,
    is_bypass,
    reset_context,
    set_context,
)
from app.core.exceptions import TenantContextMissingError
from app.models import Notification, RefreshToken
from app.repositories.notification import NotificationRepository
from app.repositories.refresh_token import RefreshTokenRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository

pytestmark = pytest.mark.asyncio


async def _seed_two_tenants(db_session_factory) -> dict:
    """两个租户，各一份「用户 + 令牌 + 通知」。返回关键 id。"""
    session = db_session_factory()
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        out: dict = {}
        for code in ("alpha", "beta"):
            set_context(RequestContext(tenant_id=None, data_scope=DataScope.ALL))
            tenant = TenantRepository(session).create(
                code=code, name=f"团队{code}", status="ACTIVE", max_members=50
            )
            await session.flush()

            set_context(RequestContext(tenant_id=tenant.id, data_scope=DataScope.ALL))
            user = UserRepository(session).create(
                username=f"user-{code}", password_hash="x", is_active=True
            )
            await session.flush()

            session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti=f"{code}-expired",
                    expires_at=now - timedelta(days=30),
                    revoked=False,
                )
            )
            session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti=f"{code}-valid",
                    expires_at=now + timedelta(days=7),
                    revoked=False,
                )
            )
            session.add(
                Notification(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    type="member.joined",
                    payload={"tenant_name": f"团队{code}"},
                    read_at=None,
                )
            )
            out[code] = {"tenant_id": tenant.id, "user_id": user.id}
        await session.commit()
        return out
    finally:
        reset_context()
        await session.close()


async def _jtis_in(db_session_factory, *, tenant_id: int) -> set[str]:
    session = db_session_factory()
    try:
        rows = await session.execute(
            select(RefreshToken.jti).where(RefreshToken.tenant_id == tenant_id)
        )
        return set(rows.scalars().all())
    finally:
        await session.close()


async def _unread_in(db_session_factory, *, tenant_id: int) -> int:
    session = db_session_factory()
    try:
        rows = await session.execute(
            select(Notification.id).where(
                Notification.tenant_id == tenant_id,
                Notification.read_at.is_(None),
            )
        )
        return len(rows.scalars().all())
    finally:
        await session.close()


# ======================================================================
# bulk_delete
# ======================================================================
async def test_bulk_delete_does_not_touch_other_tenant(db_session_factory, _schema):
    """★ 修复前：删过期令牌会把**两个租户**的过期行一起删掉。"""
    data = await _seed_two_tenants(db_session_factory)
    now = datetime.now(UTC).replace(tzinfo=None)

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=data["alpha"]["tenant_id"], data_scope=DataScope.ALL))
        removed = await RefreshTokenRepository(session).bulk_delete(RefreshToken.expires_at < now)
        await session.commit()
    finally:
        reset_context()
        await session.close()

    assert removed == 1, "只该删本租户那一条过期令牌"

    # 反向：另一个租户一行都不能少
    beta = await _jtis_in(db_session_factory, tenant_id=data["beta"]["tenant_id"])
    assert beta == {"beta-expired", "beta-valid"}, f"租户 beta 被误删，实际 {beta}"


async def test_bulk_delete_requires_tenant_context(db_session_factory, _schema):
    """没有租户上下文时必须拒绝，而不是「不过滤就全删」。"""
    await _seed_two_tenants(db_session_factory)

    session = db_session_factory()
    try:
        reset_context()
        with pytest.raises(TenantContextMissingError):
            await RefreshTokenRepository(session).bulk_delete()
    finally:
        await session.close()


# ======================================================================
# bulk_update
# ======================================================================
async def test_bulk_update_does_not_touch_other_tenant(db_session_factory, _schema):
    """★ 修复前：把某用户的通知全标已读，会把他在**其他租户**的未读也清掉。"""
    data = await _seed_two_tenants(db_session_factory)

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=data["alpha"]["tenant_id"], data_scope=DataScope.ALL))
        updated = await NotificationRepository(session).bulk_update(
            {"read_at": datetime.now(UTC)},
            Notification.read_at.is_(None),
        )
        await session.commit()
    finally:
        reset_context()
        await session.close()

    assert updated == 1
    assert await _unread_in(db_session_factory, tenant_id=data["alpha"]["tenant_id"]) == 0

    # 反向：另一个租户的未读必须原样保留
    assert await _unread_in(db_session_factory, tenant_id=data["beta"]["tenant_id"]) == 1, (
        "租户 beta 的未读数被跨租户清掉了"
    )


# ======================================================================
# 真实业务方法（修复前踩坑的那两个）
# ======================================================================
async def test_revoke_chain_is_tenant_scoped(db_session_factory, _schema):
    """★ 登出撤销令牌只该撤销**当前租户**的。

    修复前的行为：用户在一个租户登出，会把他在**所有租户**的 refresh token
    一起撤销——在别家公司的会话被莫名踢掉。
    """
    data = await _seed_two_tenants(db_session_factory)

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=data["alpha"]["tenant_id"], data_scope=DataScope.ALL))
        await RefreshTokenRepository(session).revoke_chain(
            data["alpha"]["user_id"], reason="LOGOUT"
        )
        await session.commit()

        # 本租户全部已撤销
        repo = RefreshTokenRepository(session)
        own = await repo.list_all()
        assert all(r.revoked for r in own), "本租户令牌应全部撤销"
    finally:
        reset_context()
        await session.close()

    # 反向：另一个租户的令牌一条都不能被撤销
    session = db_session_factory()
    try:
        rows = await session.execute(
            select(RefreshToken.jti, RefreshToken.revoked).where(
                RefreshToken.tenant_id == data["beta"]["tenant_id"]
            )
        )
        for jti, revoked in rows.all():
            assert revoked is False, f"租户 beta 的 {jti} 被跨租户撤销了"
    finally:
        await session.close()


async def test_purge_expired_is_tenant_scoped(db_session_factory, _schema):
    """★ 定期清理过期令牌只该清**本租户**的。

    这是第 7 周写清理任务时被测试抓出来的：一条不带租户条件的 DELETE
    会删掉所有租户的过期令牌。
    """
    from app.services import token_service

    data = await _seed_two_tenants(db_session_factory)

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=data["alpha"]["tenant_id"], data_scope=DataScope.ALL))
        removed = await token_service.purge_expired_refresh_tokens(session, retention_days=7)
    finally:
        reset_context()
        await session.close()

    assert removed == 1
    alpha = await _jtis_in(db_session_factory, tenant_id=data["alpha"]["tenant_id"])
    assert alpha == {"alpha-valid"}, f"本租户应只剩有效令牌，实际 {alpha}"

    beta = await _jtis_in(db_session_factory, tenant_id=data["beta"]["tenant_id"])
    assert beta == {"beta-expired", "beta-valid"}, f"租户 beta 被误清，实际 {beta}"


# ======================================================================
# bypass 下的安全失败
# ======================================================================
async def test_bulk_write_under_bypass_deletes_nothing(db_session_factory, _schema):
    """bypass 下 `bulk_*` 的条件是 `tenant_id == 0`，匹配不到行——**安全失败**。

    这是刻意的：批量跨租户写必须走 `bypass_update`（会写审计），
    绝不能靠「钩子会过滤」兜底（它根本不管批量写）。
    所以要钉住「bypass 下 bulk 写不会误删全部」这个行为。
    """
    data = await _seed_two_tenants(db_session_factory)

    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=None, data_scope=DataScope.ALL, bypass=True))
        assert is_bypass() is True
        removed = await RefreshTokenRepository(session).bulk_delete()
        await session.commit()
    finally:
        reset_context()
        await session.close()

    assert removed == 0, "bypass 下 bulk 写必须安全失败（返回 0），不能误删"
    for code in ("alpha", "beta"):
        assert len(await _jtis_in(db_session_factory, tenant_id=data[code]["tenant_id"])) == 2
