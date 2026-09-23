"""Celery 任务包。

★ 本包是「**薄层，只调用 services**」（PROJECT-PLAN 结构图 359）。
  任务只负责「什么时候跑」，业务规则留在 services——否则同一段逻辑
  会在「接口路径」与「任务路径」各写一份，迟早漂移。

模块划分：

    celery_app.py   Celery 实例与配置（worker / beat 的入口）
    context.py      租户上下文守卫 —— **风险 2 的落地点**，改任务前先读它
    maintenance.py  定期维护（beat 调度）
    email.py        邮件异步发送
    audit.py        审计异步落库（可选路径）

★ 写新任务前必须知道的两件事：

  1. 任务**必须**经 `@tenant_task` 声明租户依赖，并在签名里带 `tenant_id`。
     Celery worker 没有请求上下文，忘了设 = 钩子不过滤 = 操作所有租户的数据。
  2. 任务函数是**同步**的，调 async 的 service 用 `asyncio.run(...)`。
     实测过：`asyncio.run` 之前 set 的 contextvar，协程内（含嵌套 task）
     读得到，所以 `@tenant_task` 的 set/reset 覆盖得到 service 层。
"""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
