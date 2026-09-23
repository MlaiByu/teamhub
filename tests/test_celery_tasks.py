"""Celery 任务骨架与租户上下文守卫测试（第 7 周步骤 1）。

★ 本文件里价值最高的两条，都是围绕 PROJECT-PLAN 风险 2：

  1. `test_task_sets_own_context_without_outer_context`
     —— 证明「任务在**没有任何外部上下文**时也能自己设对」。
     Celery worker 就是这种处境：没有中间件、没有请求、没有 contextvar。
     少了这道保障，任务里的查询会扫到**所有租户**的数据，且不报错。

  2. `test_cleanup_only_affects_target_tenant`
     —— 矩阵 9 的落地点：租户级清理只能动本租户的行。

★ 另外两条容易漏的边界：
  · 已撤销但**未过期**的令牌不能删（复用检测靠它，删了告警就永不触发）
  · 任务抛异常时上下文也必须复位（否则污染 worker 进程里的下一个任务）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.constants import DataScope
from app.core.db.context import (
    RequestContext,
    current_data_scope,
    current_tenant_id,
    reset_context,
    set_context,
)
from app.models import RefreshToken
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.tasks.celery_app import celery_app
from app.tasks.context import TenantContextMissingError, tenant_task
from app.tasks.maintenance import (
    cleanup_expired_refresh_tokens,
    cleanup_tenant_refresh_tokens,
)

# 注意：本文件混有同步与异步用例，故不设全局 asyncio 标记
# （pyproject 里 asyncio_mode = "auto" 会自动识别异步函数）。


# ======================================================================
# Celery 配置
# ======================================================================
def test_eager_mode_is_on_for_zero_dependency_runs():
    """本地/测试必须 eager，否则零依赖跑不起来（需要 broker + worker）。"""
    assert celery_app.conf.task_always_eager is True


def test_eager_propagates_so_failures_are_visible():
    """eager 下异常必须上抛。

    默认 `task_eager_propagates=False` 会把异常存进结果对象，
    调用方看到的是「任务成功了但没结果」——非常难查。
    """
    assert celery_app.conf.task_eager_propagates is True


def test_beat_schedule_registers_cleanup():
    """beat 必须有定期任务，否则 compose 里的 beat 进程空转无意义。"""
    schedule = celery_app.conf.beat_schedule
    assert "cleanup-expired-refresh-tokens" in schedule
    assert (
        schedule["cleanup-expired-refresh-tokens"]["task"]
        == "app.tasks.maintenance.cleanup_expired_refresh_tokens"
    )


def test_tasks_are_registered():
    """任务模块必须真的被导入注册（include 配错会静默注册不上）。"""
    celery_app.loader.import_default_modules()
    names = set(celery_app.tasks)
    assert "app.tasks.maintenance.cleanup_expired_refresh_tokens" in names
    assert "app.tasks.maintenance.cleanup_tenant_refresh_tokens" in names


# ======================================================================
# tenant_task 装饰器
# ======================================================================
def test_tenant_task_rejects_missing_tenant_id():
    """★ 缺 tenant_id 必须拒绝，而不是「没上下文就不过滤」。"""

    @tenant_task
    def probe(*, tenant_id: int) -> None:  # pragma: no cover - 不该被执行
        raise AssertionError("缺少 tenant_id 时任务体不该被执行")

    with pytest.raises(TenantContextMissingError):
        probe()  # type: ignore[call-arg]


def test_tenant_task_rejects_explicit_none():
    @tenant_task
    def probe(*, tenant_id: int | None) -> None:  # pragma: no cover
        raise AssertionError("tenant_id=None 时任务体不该被执行")

    with pytest.raises(TenantContextMissingError):
        probe(tenant_id=None)


def test_task_sets_own_context_without_outer_context():
    """★ 无任何外部上下文时，任务自己把上下文设对。

    这是 worker 的真实处境——它不经过中间件，没有 contextvar。
    """
    captured: dict[str, object] = {}

    @tenant_task
    def probe(*, tenant_id: int) -> None:
        captured["tenant"] = current_tenant_id.get()
        captured["scope"] = current_data_scope.get()

    # 前置条件：此刻确实没有任何上下文（模拟 worker 进程）
    assert current_tenant_id.get() is None

    probe(tenant_id=7)

    assert captured["tenant"] == 7
    # 必须是 ALL：任务代表系统而非某个用户；
    # 若设 SELF 且无 user_id，钩子会判定恒假，任务什么都查不到。
    assert captured["scope"] == DataScope.ALL


def test_tenant_task_resets_context_after_success():
    @tenant_task
    def probe(*, tenant_id: int) -> None:
        pass

    probe(tenant_id=7)
    assert current_tenant_id.get() is None, "任务结束后必须复位，否则污染后续任务"


def test_tenant_task_resets_context_after_failure():
    """★ 任务抛异常时同样要复位。

    Celery worker 的进程会被后续任务复用，不复位 =
    下一个任务带着上一个租户的上下文执行 = **跨租户写入**。
    """

    @tenant_task
    def boom(*, tenant_id: int) -> None:
        raise ValueError("任务内部失败")

    with pytest.raises(ValueError):
        boom(tenant_id=7)

    assert current_tenant_id.get() is None


# ======================================================================
# 清理任务（矩阵 9：只处理本租户数据）
# ======================================================================
async def _seed_two_tenants_with_tokens(db_session_factory) -> tuple[int, int]:
    """建两个租户，各放三条 token：已过期 / 有效 / 已撤销但未过期。

    返回 (tenant_a_id, tenant_b_id)。
    """
    session = db_session_factory()
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        tenant_ids: list[int] = []
        for code in ("acme", "globex"):
            tenant = TenantRepository(session).create(
                code=code, name=f"团队{code}", status="ACTIVE", max_members=50
            )
            await session.flush()
            tenant_ids.append(tenant.id)

            set_context(RequestContext(tenant_id=tenant.id, data_scope=DataScope.ALL))
            user = UserRepository(session).create(
                username=f"user-{code}", password_hash="x", is_active=True
            )
            await session.flush()

            # 10 天前过期（超过 7 天保留期 → 应被删）
            session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti=f"{code}-expired",
                    expires_at=now - timedelta(days=10),
                    revoked=False,
                )
            )
            # 未来过期（有效 → 不该删）
            session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti=f"{code}-valid",
                    expires_at=now + timedelta(days=7),
                    revoked=False,
                )
            )
            # 已撤销但**未过期**（复用检测需要 → 不该删）
            session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti=f"{code}-revoked",
                    expires_at=now + timedelta(days=7),
                    revoked=True,
                    revoked_reason="ROTATED",
                )
            )
        await session.commit()
        return tenant_ids[0], tenant_ids[1]
    finally:
        await session.close()


async def _jti_set(db_session_factory, *, tenant_id: int) -> set[str]:
    """在指定租户上下文下取出所有 jti。"""
    session = db_session_factory()
    try:
        set_context(RequestContext(tenant_id=tenant_id, data_scope=DataScope.ALL))
        # 显式带 tenant_id：这条只为了取断言用的快照，
        # 不依赖钩子（钩子只对 ORM 实体查询生效，取单列不经过它）。
        rows = await session.execute(
            select(RefreshToken.jti).where(RefreshToken.tenant_id == tenant_id)
        )
        return set(rows.scalars().all())
    finally:
        reset_context()
        await session.close()


async def test_cleanup_removes_only_expired(db_session_factory, _schema):
    """只删过期的；有效的与「已撤销未过期」都保留。"""
    tenant_a, _tenant_b = await _seed_two_tenants_with_tokens(db_session_factory)

    result = cleanup_tenant_refresh_tokens.delay(tenant_id=tenant_a).get()
    assert result["removed"] == 1, f"只该删 1 条过期令牌，实际 {result}"

    remaining = await _jti_set(db_session_factory, tenant_id=tenant_a)
    assert remaining == {"acme-valid", "acme-revoked"}, (
        f"有效令牌与已撤销但未过期的令牌都必须保留，实际 {remaining}"
    )


async def test_cleanup_only_affects_target_tenant(db_session_factory, _schema):
    """★ 矩阵 9 的落地点：清理租户 A 不能动到租户 B 的行。

    若任务没设上下文（或设错），钩子不过滤，这条 DELETE 会把
    **两个租户**的过期令牌都删掉——而且不报错。
    """
    tenant_a, tenant_b = await _seed_two_tenants_with_tokens(db_session_factory)

    cleanup_tenant_refresh_tokens.delay(tenant_id=tenant_a).get()

    # B 租户三条全在（一条都没被误删）
    remaining_b = await _jti_set(db_session_factory, tenant_id=tenant_b)
    assert remaining_b == {"globex-expired", "globex-valid", "globex-revoked"}, (
        f"租户 B 的数据不该被租户 A 的清理影响，实际 {remaining_b}"
    )


async def test_platform_task_fans_out_to_every_tenant(db_session_factory, _schema):
    """平台级任务列出所有租户并逐个扇出（不自己设上下文）。"""
    await _seed_two_tenants_with_tokens(db_session_factory)

    result = cleanup_expired_refresh_tokens.delay().get()

    assert result["mode"] == "all"
    assert result["dispatched"] == 2, f"应扇出到 2 个租户，实际 {result}"


async def test_platform_task_accepts_single_tenant(db_session_factory, _schema):
    """给定 tenant_id 时只扇出该租户（手动补救用）。"""
    tenant_a, _tenant_b = await _seed_two_tenants_with_tokens(db_session_factory)

    result = cleanup_expired_refresh_tokens.delay(tenant_id=tenant_a).get()

    assert result == {"mode": "single", "dispatched": 1}


async def test_cleanup_uses_retention_window(db_session_factory, _schema):
    """保留期内的「刚过期」行不删——留缓冲容忍时钟偏差。

    造一条 1 天前过期的行（保留期 7 天 → 仍在缓冲内，不该删）。
    """
    session = db_session_factory()
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        tenant = TenantRepository(session).create(
            code="recent", name="团队", status="ACTIVE", max_members=10
        )
        await session.flush()
        set_context(RequestContext(tenant_id=tenant.id, data_scope=DataScope.ALL))
        user = UserRepository(session).create(username="u", password_hash="x", is_active=True)
        await session.flush()
        session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=user.id,
                jti="just-expired",
                expires_at=now - timedelta(days=1),
                revoked=False,
            )
        )
        await session.commit()
        tenant_id = tenant.id
    finally:
        await session.close()

    result = cleanup_tenant_refresh_tokens.delay(tenant_id=tenant_id).get()

    assert result["removed"] == 0, "保留期内的行不该被删"
    remaining = await _jti_set(db_session_factory, tenant_id=tenant_id)
    assert remaining == {"just-expired"}
