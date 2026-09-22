"""仓储守卫回归测试。

★ 这个文件存在的理由：第 2 周为了支持「登录时查用户」把 repository 基类
  拆成了 `BaseRepository`（全局表，无守卫）与 `TenantAwareRepository`（租户表，有守卫）。
  拆基类是**动隔离地基**的操作，所以必须有一组测试钉住两件事：

    1. 全局表确实可以无上下文访问（新能力，登录路径依赖它）
    2. 租户表**仍然**拒绝无上下文访问（老保证，不能被拆分悄悄削弱）

  第 1 条如果坏了，表现为「登录 500」；第 2 条如果坏了，表现为
  **跨租户静默泄露**——后者不会报错，只会某天被人发现。所以第 2 条
  的重要性远高于第 1 条，断言也写得更死。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope
from app.core.db.context import (
    current_data_scope,
    current_dept_id,
    current_tenant_id,
    current_user_id,
    is_bypass,
)
from app.core.exceptions import TenantContextMissingError
from app.models import Project
from app.repositories import TenantAwareRepository, bypass_context
from app.repositories.rbac import RoleRepository
from app.repositories.tenant import TenantMemberRepository
from app.repositories.user import UserRepository

pytestmark = pytest.mark.asyncio


class ProjectRepository(TenantAwareRepository[Project]):
    """测试替身：一个最小的租户级 repository。"""

    model = Project


# ----------------------------------------------------------------------
# 1. 全局表：无上下文可访问（登录路径的前提）
# ----------------------------------------------------------------------
async def test_global_repository_reads_without_tenant_context(db_session_factory, tenant_ctx):
    """`User` 是全局表，无租户上下文时必须能查。

    登录发生在「还没有租户上下文」的时刻——上下文要靠 access token 才能设，
    而 token 正是登录要签发的产物。给这条查询套租户守卫会让登录永远失败。
    """
    s = db_session_factory()
    tenant_ctx(tenant_id=None)

    rows = await UserRepository(s).list_all()
    assert rows == []
    await s.close()


async def test_global_repository_writes_without_tenant_context(db_session_factory, tenant_ctx):
    """注册的第一笔写入（建 User）也必须能在无上下文下完成。"""
    s = db_session_factory()
    tenant_ctx(tenant_id=None)

    user = UserRepository(s).create(username="bootstrap", password_hash="x")
    await s.flush()

    assert user.id is not None
    # 全局表没有 tenant 维度，不该被塞进一个 tenant_id
    assert not hasattr(user, "tenant_id") or getattr(user, "tenant_id", None) is None
    await s.close()


# ----------------------------------------------------------------------
# 2. 租户表：**仍然**拒绝无上下文（不可被拆分削弱的老保证）
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "repo_cls",
    [TenantMemberRepository, RoleRepository, ProjectRepository],
    ids=["TenantMember", "Role", "Project"],
)
async def test_tenant_repository_still_refuses_without_context(
    db_session_factory, tenant_ctx, repo_cls
):
    """租户级仓储在无上下文时必须抛错——**这是隔离的地基，不能被削弱**。

    如果这条坏了，后果不是报错而是静默跨租户泄露：钩子在空上下文下不过滤，
    守卫又放行，查询会返回**所有租户**的数据。所以断言写成「必须抛」，
    而不是「返回空」。
    """
    s = db_session_factory()
    tenant_ctx(tenant_id=None)

    with pytest.raises(TenantContextMissingError):
        await repo_cls(s).list_all()

    await s.close()


async def test_tenant_repository_refuses_primary_key_get_without_context(
    db_session_factory, tenant_ctx
):
    """主键查询同样要拒——`get()` 不能因为「只是个 id 查询」就绕过守卫。"""
    s = db_session_factory()
    tenant_ctx(tenant_id=None)

    with pytest.raises(TenantContextMissingError):
        await ProjectRepository(s).get(1)

    await s.close()


# ----------------------------------------------------------------------
# 3. bypass 作用域：借用后必须还原
# ----------------------------------------------------------------------
async def test_bypass_context_restores_previous_context(db_session_factory, tenant_ctx):
    """bypass 退出后必须**还原**外层上下文，而不是清空。

    ★ 这条钉的是第 2 周修掉的一个 bug：第一版实现退出时硬编码
      `set_context(RequestContext(tenant_id=None))`，于是嵌套调用会把外层
      已有租户上下文一起抹掉——**静默降级为全量可见，且没有任何日志**。
      改动后语义是「借用一下，用完还回去」。
    """
    tenant_ctx(tenant_id=42, user_id=7, dept_id=99, scope=DataScope.DEPT)

    async with bypass_context():
        assert is_bypass() is True, "bypass 作用域内开关必须打开"

    assert is_bypass() is False, "退出后 bypass 必须关闭"
    # ★ 关键断言：外层上下文必须原样还在
    assert current_tenant_id.get() == 42, "bypass 退出后不能把外层租户上下文清掉"
    assert current_user_id.get() == 7
    assert current_dept_id.get() == 99
    assert current_data_scope.get() == DataScope.DEPT


async def test_bypass_context_restores_after_exception(db_session_factory, tenant_ctx):
    """异常路径同样要还原——否则一次失败的 bypass 会污染后续所有请求。"""
    tenant_ctx(tenant_id=42, user_id=7, dept_id=99, scope=DataScope.ALL)

    with pytest.raises(RuntimeError):
        async with bypass_context():
            raise RuntimeError("boom")

    assert is_bypass() is False
    assert current_tenant_id.get() == 42, "异常退出也必须还原上下文"
    assert current_data_scope.get() == DataScope.ALL


async def test_bypass_create_requires_explicit_tenant_id(db_session_factory, tenant_ctx):
    """bypass 下写租户表必须显式传 tenant_id。

    ★ 原因：bypass 上下文里没有租户身份，`require_tenant_context()` 返回 0。
      如果不拦，`create()` 会把 `tenant_id=0` 写进去——违反外键、写坏数据，
      而且错误信息会指向「外键约束失败」这种与真实原因无关的地方。
      直接报「必须显式传 tenant_id」比让它写出去再炸掉清楚得多。
    """
    s = db_session_factory()
    tenant_ctx(tenant_id=None)

    async with bypass_context():
        with pytest.raises(TenantContextMissingError):
            ProjectRepository(s).create(code="X-1", name="缺租户", owner_id=1, dept_id=1)

    await s.close()
