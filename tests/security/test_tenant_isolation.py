"""第 1 周就建立的测试骨架（PROJECT-PLAN 十二·关键调整 3）。

★ 为什么这一周就建：
    隔离测试的价值不在于覆盖率，而在于它是验证「钩子真的生效」的**唯一手段**。
    晚一周写，就多一周的假通过风险——而假通过是看不见的。

★ 假通过长什么样（第 5 条验收标准为什么最关键）：
    如果该租户下本来就只剩 A 自己的数据，前几条断言会全部通过，
    你以为钩子在起作用，其实只是碰巧没别人的数据。
    只有**反向验证**（清空上下文后应看到全部）才能证明钩子真的在工作。

所以本文件的每个用例都成对出现：
    [正向] 设了上下文 → 只看到本租户
    [反向] 清空上下文 → 看到全部
没有反向验证的隔离断言，一律视为无效断言。
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope, ProjectStatus
from app.core.exceptions import TenantContextMissingError
from app.models import Project, Tenant
from app.repositories.base import TenantAwareRepository

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------
# 测试替身：一个最小的租户级 repository
# ----------------------------------------------------------------------
class ProjectRepository(TenantAwareRepository[Project]):
    model = Project


async def _seed_two_tenants(db, tenant_ctx) -> dict[str, int]:
    """造两个租户，各一个项目。返回关键 ID。

    注意写入路径：repo.create() 会从上下文取 tenant_id，
    所以造数据时必须先设对应上下文——这本身就是「坑 2」的验证。

    ★ 结尾必须清空上下文：种数据是「上帝视角」动作，
      结束后如果不复位，下一个查询会带着租户 2 的上下文跑，
      测试通过与否取决于种数据的顺序——这是最难查的一类假通过。
    """
    db.add(Tenant(id=1, code="acme", name="Acme"))
    db.add(Tenant(id=2, code="globex", name="Globex"))
    await db.flush()

    repo = ProjectRepository(db)

    # 租户 1 的项目
    tenant_ctx(tenant_id=1, user_id=11, dept_id=100, scope=DataScope.ALL)
    p1 = repo.create(
        code="P-1", name="租户1的项目", status=ProjectStatus.ACTIVE, owner_id=11, dept_id=100
    )
    await db.flush()

    # 租户 2 的项目
    tenant_ctx(tenant_id=2, user_id=22, dept_id=200, scope=DataScope.ALL)
    p2 = repo.create(
        code="P-1", name="租户2的项目", status=ProjectStatus.ACTIVE, owner_id=22, dept_id=200
    )
    await db.flush()

    # 种完立刻清空，把上下文交还给测试自己决定
    tenant_ctx(tenant_id=None)

    return {"tenant1": 1, "tenant2": 2, "project1": p1.id, "project2": p2.id}


# ----------------------------------------------------------------------
# 矩阵 1 / 2：租户级列表查询与主键查询
# ----------------------------------------------------------------------
async def test_list_only_returns_own_tenant(db_session_factory, tenant_ctx):
    """[正向] 租户 A 查列表，只看到自己的。"""
    s1 = db_session_factory()
    ids = await _seed_two_tenants(s1, tenant_ctx)
    await s1.commit()
    await s1.close()

    # ★ 全新会话：避开 identity map 缓存（坑 4）
    s2 = db_session_factory()
    tenant_ctx(tenant_id=ids["tenant1"], user_id=11, dept_id=100, scope=DataScope.ALL)
    rows = await ProjectRepository(s2).list_all()

    assert len(rows) == 1, f"租户1应只看到 1 条，实际 {len(rows)} 条"
    assert rows[0].name == "租户1的项目"
    assert all(r.tenant_id == ids["tenant1"] for r in rows)
    await s2.close()


async def test_list_returns_all_when_context_cleared(db_session_factory, tenant_ctx):
    """[反向验证] ★ 清空上下文后，同一查询应返回**全部**。

    这是整个矩阵里最重要的一条：它证明「隔离生效」而不是「恰好没数据」。
    如果这条失败而上面那条通过，说明你的隔离测试是假通过。
    """
    s1 = db_session_factory()
    await _seed_two_tenants(s1, tenant_ctx)
    await s1.commit()
    await s1.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=None)  # 清空租户上下文 → 钩子不过滤
    # 用 raw_list_all（不经守卫）才能走到「上下文为空」这条路径；
    # 走 list_all 会被 repository 的守卫拦下——那是另一条防线，这里要验的是钩子
    rows = await ProjectRepository(s2).raw_list_all()

    assert len(rows) == 2, (
        f"清空上下文后应看到全部 2 条，实际 {len(rows)} 条。"
        "若为 1 条，说明钩子根本没在过滤（或过滤条件写死了）"
    )
    await s2.close()


async def test_primary_key_get_across_tenant_returns_none(db_session_factory, tenant_ctx):
    """[矩阵 2] 用 B 的项目 ID 在主键查询下应返回 None。

    ★ 必须用全新会话 —— Session.get() 命中 identity map 时不发 SQL，
      会测到缓存而非过滤逻辑，产生假通过/假失败（坑 4）。
    """
    s1 = db_session_factory()
    ids = await _seed_two_tenants(s1, tenant_ctx)
    await s1.commit()
    await s1.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=ids["tenant1"], user_id=11, dept_id=100, scope=DataScope.ALL)
    got = await ProjectRepository(s2).get(ids["project2"])  # 用租户2的 ID

    assert got is None, "跨租户主键查询必须返回 None（对应接口层就是 404）"
    await s2.close()


async def test_get_own_project_still_works(db_session_factory, tenant_ctx):
    """[对照组] 用**自己的** ID 查得到。

    没有这条，上面那条「返回 None」可能是别的原因（比如查询压根写错了）
    导致的恒为 None——那就成了另一种假通过。
    """
    s1 = db_session_factory()
    ids = await _seed_two_tenants(s1, tenant_ctx)
    await s1.commit()
    await s1.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=ids["tenant1"], user_id=11, dept_id=100, scope=DataScope.ALL)
    got = await ProjectRepository(s2).get(ids["project1"])

    assert got is not None and got.id == ids["project1"]
    await s2.close()


# ----------------------------------------------------------------------
# 矩阵 7：未设上下文 = 全量泄露，钉死风险面
# ----------------------------------------------------------------------
async def test_repository_rejects_query_without_context(db_session_factory, tenant_ctx):
    """未设上下文时 repository 必须拒绝，而不是静默返回全量。

    这条是「坑 1」的守卫测试：钩子本身不过滤是机制设计，
    真正的防线是 repository 入口的那一 raise。
    """
    s = db_session_factory()
    tenant_ctx(tenant_id=None)
    with pytest.raises(TenantContextMissingError):
        await ProjectRepository(s).list_all()
    await s.close()


# ----------------------------------------------------------------------
# 矩阵 6：插入必须带 tenant_id
# ----------------------------------------------------------------------
async def test_create_inherits_tenant_id_from_context(db_session_factory, tenant_ctx):
    """create() 从上下文补 tenant_id，调用方不需要手动传。"""
    s = db_session_factory()
    s.add(Tenant(id=7, code="initech", name="Initech"))
    await s.flush()

    tenant_ctx(tenant_id=7, user_id=70, dept_id=700, scope=DataScope.ALL)
    p = ProjectRepository(s).create(code="X-1", name="自动补租户", owner_id=70, dept_id=700)
    await s.flush()

    assert p.tenant_id == 7, "tenant_id 必须由 repository 从上下文写入（坑 2）"
    await s.close()


async def test_create_rejects_mismatched_tenant_id(db_session_factory, tenant_ctx):
    """显式传入他人 tenant_id 必须被拒绝，防止越权写入。"""
    s = db_session_factory()
    s.add(Tenant(id=8, code="umbrella", name="Umbrella"))
    s.add(Tenant(id=9, code="wayne", name="Wayne"))
    await s.flush()

    tenant_ctx(tenant_id=8, user_id=80, dept_id=800, scope=DataScope.ALL)
    with pytest.raises(TenantContextMissingError):
        ProjectRepository(s).create(
            code="Y-1", name="越权写入", owner_id=80, dept_id=800, tenant_id=9
        )
    await s.close()


# ----------------------------------------------------------------------
# 数据范围：SELF / DEPT / ALL（矩阵 3/4/5 的提前占位，第 6 周补全）
# ----------------------------------------------------------------------
async def test_data_scope_self_only_sees_own(db_session_factory, tenant_ctx):
    """[矩阵 3] SELF 只能看到 owner_id == 自己的。"""
    s = db_session_factory()
    s.add(Tenant(id=1, code="acme", name="Acme"))
    await s.flush()
    repo = ProjectRepository(s)

    tenant_ctx(tenant_id=1, user_id=11, dept_id=100, scope=DataScope.ALL)
    repo.create(code="A", name="我的", owner_id=11, dept_id=100)
    repo.create(code="B", name="同部门同事的", owner_id=12, dept_id=100)
    await s.flush()
    await s.commit()
    await s.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=1, user_id=11, dept_id=100, scope=DataScope.SELF)
    rows = await ProjectRepository(s2).list_all()
    assert [r.name for r in rows] == ["我的"]
    await s2.close()


async def test_data_scope_dept_sees_whole_department(db_session_factory, tenant_ctx):
    """[矩阵 4] DEPT 能看到本部门所有人的，看不到别的部门。"""
    s = db_session_factory()
    s.add(Tenant(id=1, code="acme", name="Acme"))
    await s.flush()
    repo = ProjectRepository(s)

    tenant_ctx(tenant_id=1, user_id=11, dept_id=100, scope=DataScope.ALL)
    repo.create(code="A", name="我部门-我的", owner_id=11, dept_id=100)
    repo.create(code="B", name="我部门-同事", owner_id=12, dept_id=100)
    repo.create(code="C", name="别的部门", owner_id=13, dept_id=200)
    await s.flush()
    await s.commit()
    await s.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=1, user_id=11, dept_id=100, scope=DataScope.DEPT)
    names = {r.name for r in await ProjectRepository(s2).list_all()}
    assert names == {"我部门-我的", "我部门-同事"}
    await s2.close()


async def test_data_scope_all_still_bounded_by_tenant(db_session_factory, tenant_ctx):
    """[矩阵 5] ALL 看全租户，但**仍限于本租户**——两层是 AND 关系。"""
    s = db_session_factory()
    ids = await _seed_two_tenants(s, tenant_ctx)
    await s.commit()
    await s.close()

    s2 = db_session_factory()
    tenant_ctx(tenant_id=ids["tenant1"], user_id=11, dept_id=100, scope=DataScope.ALL)
    rows = await ProjectRepository(s2).list_all()
    assert len(rows) == 1 and rows[0].tenant_id == ids["tenant1"], (
        "ALL 只放开数据范围，不能突破租户边界"
    )
    await s2.close()


# ----------------------------------------------------------------------
# 矩阵 8：跨租户外键挂载
# ----------------------------------------------------------------------
async def test_cross_tenant_foreign_key_mount_is_rejected(db_session_factory, tenant_ctx):
    """[矩阵 8] 把 A 租户的任务挂到 B 租户的项目上，必须被应用层拒绝。

    ★ 这是多租户最隐蔽的坑：数据库外键只保证 id 存在，**不保证同租户**。
      PostgreSQL 会老老实实插入成功——只有应用层校验能拦住。
      对应实现在 services/task_service.py（第 4 周落地）。

    当前为骨架：先钉住「校验函数必须存在且行为正确」。
    """
    from app.services.task_service import ensure_same_tenant

    s1 = db_session_factory()
    ids = await _seed_two_tenants(s1, tenant_ctx)
    await s1.commit()
    # 取 proj2 的「快照」：expunge_all 后再 get，避免拿到 identity map 里的缓存对象。
    # 这里不关心取数过程，只关心拿到一个确实属于租户 2 的实体用于挂载校验。
    s1.expunge_all()
    tenant_ctx(tenant_id=ids["tenant2"], user_id=22, dept_id=200, scope=DataScope.ALL)
    proj2 = await s1.get(Project, ids["project2"])
    assert proj2 is not None, "前置条件：租户2的项目应能取到"

    # 切成租户 1 的上下文，此时 proj2.tenant_id 仍是 2 → 必须被拒绝
    tenant_ctx(tenant_id=ids["tenant1"], user_id=11, dept_id=100, scope=DataScope.ALL)
    with pytest.raises(TenantContextMissingError):
        ensure_same_tenant(proj2, expected_tenant_id=ids["tenant1"])

    await s1.close()
