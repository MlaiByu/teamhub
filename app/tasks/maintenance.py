"""定期维护任务（beat 调度）。

★ 本模块的形态是「**平台级扇出 + 租户级执行**」两层，这个形态是被
  PROJECT-PLAN 风险 2 逼出来的，不是随便拆的：

  定期清理要处理**所有租户**，但每个租户的数据只能在**该租户的上下文**下操作。
  如果在同一个任务里循环所有租户、反复 set/reset 上下文，
  只要有一次忘了 reset，下一个租户就会带着上一个租户的上下文执行
  ——**跨租户误删**，而且不会有任何报错。

  拆成两层后：
      平台级任务：只负责「列出该处理哪些租户」，然后把每个租户扇出成一个独立任务
      租户级任务：单次只服务一个租户，上下文由 `@tenant_task` 统一
                  set → 执行 → finally reset

  这样「上下文串味」在结构上就不可能发生。这也正是方案矩阵 9
  「Celery 任务携带 tenant_id，只处理本租户数据」要验的性质。
"""

from __future__ import annotations

from app.core.db.session import SessionLocal
from app.core.logging import get_logger
from app.repositories.tenant import TenantRepository
from app.services import token_service
from app.tasks.celery_app import celery_app
from app.tasks.context import run_async, tenant_task

logger = get_logger(__name__)


async def _list_tenant_ids() -> list[int]:
    """列出所有租户 ID。

    ★ 这一步**不需要**租户上下文：`Tenant` 是全局表（本身不带 tenant_id），
      走 `BaseRepository` 的无守卫查询。全局表是「平台级任务」唯一被允许
      触及的数据面——它拿不到任何租户的业务数据。
    """
    async with SessionLocal() as session:
        tenants = await TenantRepository(session).list_all()
        return [t.id for t in tenants]


async def _purge_current_tenant() -> int:
    """在**调用方已设好的租户上下文**内执行清理。

    本函数自己不管上下文——那是 `@tenant_task` 的职责。
    这样分工后，「上下文正确」只需要在一个地方保证。
    """
    async with SessionLocal() as session:
        return await token_service.purge_expired_refresh_tokens(session)


@celery_app.task(name="app.tasks.maintenance.cleanup_expired_refresh_tokens")
def cleanup_expired_refresh_tokens(*, tenant_id: int | None = None) -> dict:
    """平台级入口：决定清理哪些租户并逐个扇出。

    - `tenant_id` 给定：只处理该租户（可用于手动补救）
    - 未给定（beat 定期调用）：列出全部租户，逐个扇出

    ★ 本任务**不设租户上下文**，因为它不碰任何租户级表。
      它只读全局表 `tenants`，然后把工作交给带上下文的租户级任务。
    """
    if tenant_id is not None:
        cleanup_tenant_refresh_tokens.delay(tenant_id=tenant_id)
        return {"mode": "single", "dispatched": 1}

    tenant_ids = run_async(_list_tenant_ids())
    for tid in tenant_ids:
        cleanup_tenant_refresh_tokens.delay(tenant_id=tid)

    logger.info("cleanup_refresh_tokens_dispatched", tenants=len(tenant_ids))
    return {"mode": "all", "dispatched": len(tenant_ids)}


@celery_app.task(name="app.tasks.maintenance.cleanup_tenant_refresh_tokens")
@tenant_task
def cleanup_tenant_refresh_tokens(*, tenant_id: int) -> dict:
    """租户级：只清理**本租户**过期的 refresh token。

    ★ 上下文由 `@tenant_task` 保证——这是风险 2 的落地点。
      少了它，下面那条 DELETE 在 worker 里会扫到**所有租户**的令牌表
      （钩子不过滤 = 全量），而且不报错。

    ★ `tenant_id` 在函数体里没被直接使用：它已经被装饰器转成了上下文。
      仍然保留这个参数，是因为它是**契约的一部分**——调用方必须提供租户，
      装饰器才能校验；签名上看得见，调用方就无法忽略。
    """
    removed = run_async(_purge_current_tenant())
    return {"tenant_id": tenant_id, "removed": removed}
