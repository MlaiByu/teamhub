"""Celery 任务的租户上下文守卫（PROJECT-PLAN 九·风险 2 / 矩阵 9）。

★ 这是整个异步链路里**最危险的一处**，必须先把问题说清楚：

  Web 请求里，租户上下文由中间件从 JWT 里解析并写入 contextvar。
  **Celery worker 完全没有这个环节**——它是独立进程，不经过任何中间件。
  于是「忘了设上下文」的后果是：

      钩子不过滤 → 任务查到**所有租户**的数据 → 批量操作把别家公司的数据也改了

  这就是方案里排第二的风险。它比"查不到数据"危险得多，因为**不会报错**。

★ 对策（三条，缺一不可）：

  1. **任务入参显式携带 `tenant_id`**，而不是让任务自己去"想办法"拿。
     显式入参让「这个任务需要租户」成为签名的一部分，调用方无法忽略。
  2. **入口 set_context、出口 reset**，用装饰器统一做，
     而不是靠每个任务自己记得写 try/finally（漏一个就是一次事故）。
  3. **缺 `tenant_id` 直接拒绝执行**，而不是"没有上下文就不过滤"。
     宁可任务失败并留下清晰报错，也不要在无过滤状态下跑完。

★ 为什么 `data_scope` 固定设 `ALL`（而不是 SELF / 继承调用方）：

  后台任务代表**系统**，不是某个用户。它要做的是「处理这个租户的全部过期令牌」
  这类跨用户操作。若设成 SELF：

      钩子对 DataScoped 表的条件是 `owner_id == user_id`，
      而任务没有 user_id（None）→ 条件退化成**恒假** → 什么都查不到。

  这不是理论推演，是 `tenant_hook.py` 里写明的行为（SELF 且无 user_id 时
  必须拒绝而不是放行，否则 SELF 会退化成 ALL）。所以任务必须显式声明
  「我要的是这个租户内的全量」，而不是让默认值决定。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.core.constants import DataScope
from app.core.db.context import RequestContext, reset_context, set_context
from app.core.logging import get_logger

logger = get_logger(__name__)


class TenantContextMissingError(RuntimeError):
    """任务没有携带 tenant_id。

    继承 RuntimeError 而非业务异常：这是**编程错误**（调用方忘了传），
    不是可恢复的业务失败。必须让它在 worker 日志里显眼，
    而不是被当成"任务失败"重试三次后静默丢弃。
    """


def tenant_task[F: Callable[..., Any]](fn: F) -> F:
    """给租户级任务套上上下文生命周期。

    被装饰的任务**必须**接受关键字参数 `tenant_id`：

        @celery_app.task
        @tenant_task
        def some_task(*, tenant_id: int, xxx: str) -> None: ...

    执行流程：校验 tenant_id → set_context → 执行 → finally reset_context。

    ★ `finally` 复位不能省：Celery worker 的进程/线程会被后续任务复用，
      不复位会把租户 A 的上下文带给租户 B 的任务——**跨租户写入**。
      这与 `middleware/tenant_context.py` 请求结束复位是同一个道理。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        tenant_id = kwargs.get("tenant_id")
        if tenant_id is None:
            raise TenantContextMissingError(
                f"任务 {fn.__module__}.{fn.__name__} 未携带 tenant_id。"
                "Celery worker 没有请求上下文，必须由调用方显式传入租户 ID，"
                "否则钩子不过滤、任务会操作**所有租户**的数据。"
            )

        set_context(
            RequestContext(
                tenant_id=int(tenant_id),
                # 任务代表系统而非某个用户，故 user_id 留空 + 范围 ALL。
                # 详见模块 docstring 里对「为什么不能设 SELF」的说明。
                user_id=None,
                data_scope=DataScope.ALL,
            )
        )
        try:
            return fn(*args, **kwargs)
        finally:
            reset_context()

    return wrapper  # type: ignore[return-value]


__all__ = ["TenantContextMissingError", "run_async", "tenant_task"]


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """在**同步**的 Celery 任务里执行 async 代码（调 service / 仓储）。

    ★ 为什么不能直接写 `asyncio.run(...)`：

      Celery worker 里任务是同步执行、线程中没有 event loop，`asyncio.run` 正常。

      但 **eager 模式**（本地开发的默认配置 `CELERY_ALWAYS_EAGER=true`）下，
      `.delay()` 是**在当前线程同步执行**的。如果调用方本身在 async 上下文里
      （最典型：FastAPI 的 async 路由里发邮件），此刻线程里已经有 running loop，
      `asyncio.run()` 会直接抛：

          RuntimeError: asyncio.run() cannot be called from a running event loop

      这个组合一点都不罕见——「开发时 eager + 在 async 接口里触发异步任务」
      正是最日常的用法。所以必须两条路径都支持。

    ★ 换线程时**必须把 contextvar 一起带过去**（这是本函数最容易写错的地方）：

      `ThreadPoolExecutor` 的新线程**不继承** contextvar。如果只是
      `pool.submit(asyncio.run, coro)`，新线程里 `current_tenant_id` 是 None
      → 钩子不过滤 → 任务里的查询扫到**所有租户**的数据。

      所以先用 `contextvars.copy_context()` 取当前 context 快照，
      再在新线程里以该 context 运行——这样 `@tenant_task` 设的租户上下文
      才真正传递到 service 层。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的 loop：worker 的正常路径
        return asyncio.run(coro)

    # 有 loop（eager 模式且调用方在 async 上下文里）：另起线程，
    # 并把当前 context 快照带过去。
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, asyncio.run, coro).result()
