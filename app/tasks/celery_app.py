"""Celery 应用（PROJECT-PLAN 结构图 359-360：`tasks/` 是「Celery 薄层，只调用 services」）。

★ 本模块只做「组装」：实例 + 配置 + 定时表 + 任务发现。
  具体任务写在各 `app/tasks/*.py` 里，且**只调用 services**，
  不直接写业务逻辑——否则同一段逻辑会在「接口路径」与「任务路径」各写一份，
  迟早漂移（这也是方案把它称为「薄层」的原因）。

★ `task_always_eager` 的降级语义（PROJECT-PLAN 10.3 / 11.2）：

  本地与测试没有 Redis broker。`always_eager=True` 时 `.delay()` 会**同步执行**，
  结果直接返回，不需要 broker 与 worker。这样「零依赖跑测试」成立，
  且调用方的代码（`.delay(...)`）与生产完全一致，切换只靠配置。

  注意 eager 是**执行方式**的降级，不是**上下文**的降级：
  任务依然必须携带 tenant_id（见 tasks/context.py），eager 不会替你补上下文。

★ `task_eager_propagates=True`：eager 模式下任务抛的异常直接向上抛。
  默认是 False（异常被存进结果对象，调用方看不到），那样测试里
  「任务失败了」会表现为「什么都没发生」——非常难查。本地开发就该炸出来。
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "teamhub",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # ★ 显式列出任务模块（Celery 在 finalize 时导入它们，任务才会注册）。
    #
    #   不用 `autodiscover_tasks(["app.tasks"])` 的原因：它的语义是
    #   「导入 `<package>.<related_name>`」，related_name 默认是 "tasks"，
    #   于是它会去找 `app.tasks.tasks`（不存在）→ **一个任务都注册不上**。
    #   实测确认过：配了 autodiscover 之后 `celery_app.tasks` 里空无一物。
    #
    #   显式列出除了正确，还顺带让「本服务有哪些任务」在入口处一目了然。
    include=[
        "app.tasks.maintenance",
    ],
)

celery_app.conf.update(
    # --- 序列化：JSON 足矣，且比 pickle 安全（不执行任意对象构造）---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # --- 时区：全项目统一 UTC（与 datetime.now(UTC) 一致）---
    timezone="UTC",
    enable_utc=True,
    # --- 本地/测试降级 ---
    task_always_eager=settings.celery_always_eager,
    task_eager_propagates=True,
    # --- 可靠性 ---
    # Celery 6 起默认不再自动重连 broker，显式打开避免启动期瞬时故障直接退出
    broker_connection_retry_on_startup=True,
    # 任务被 worker 取走后进程崩溃 → 消息重回队列（默认 acks_late=False 会丢）
    task_acks_late=True,
    # 一次只取一条，避免某个 worker 预取一堆任务、崩溃后别人要等超时才拿到
    worker_prefetch_multiplier=1,
    # 单任务硬超时，防止一个卡死的任务永久占住 worker
    task_time_limit=600,
    task_soft_time_limit=540,
    # --- 定时任务（beat 进程读这张表；compose 里的 beat 服务依赖它）---
    beat_schedule={
        "cleanup-expired-refresh-tokens": {
            "task": "app.tasks.maintenance.cleanup_expired_refresh_tokens",
            # 每天 UTC 03:00（业务低峰）清理过期/已撤销的 refresh token。
            # 不清理的后果：该表随登录次数无限增长，最终拖慢 auth 相关查询。
            "schedule": crontab(hour=3, minute=0),
        },
    },
)
