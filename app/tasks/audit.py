"""审计异步落库任务。

★ 什么时候该用它，什么时候不该——这个取舍必须讲清楚，否则容易被误用：

  `audit_service.record()`（同步）是**默认**，也是本项目的实际选择。
  理由：审计是单行 INSERT，不是瓶颈（真正的瓶颈是邮件的网络 IO）；
  而且审计的价值恰恰在**可靠**——异步化会引入「worker 挂了这条审计就没了」，
  那等于把审计的意义削掉一半。

  `audit_service.record_deferred()`（本任务）留给**量大且可容忍丢失**的场景，
  例如高频读取埋点、用量统计。业务写操作不要用它。

  方案 62 写的是「审计异步落库」，这里保留能力但把默认设成同步，
  是**有意的偏离**，理由如上。
"""

from __future__ import annotations

from typing import Any

from app.core.db.session import SessionLocal
from app.core.logging import get_logger
from app.repositories.audit import AuditLogRepository
from app.tasks.celery_app import celery_app
from app.tasks.context import run_async, tenant_task

logger = get_logger(__name__)


async def _persist(payload: dict[str, Any]) -> None:
    """落一条审计。上下文由调用方（装饰器）保证。"""
    async with SessionLocal() as session:
        AuditLogRepository(session).create(
            user_id=payload.get("user_id"),
            action=payload["action"],
            entity_type=payload["entity_type"],
            entity_id=payload.get("entity_id"),
            detail=payload.get("detail") or {},
            ip=payload.get("ip"),
        )
        await session.commit()


@celery_app.task(name="app.tasks.audit.persist_audit_log")
@tenant_task
def persist_audit_log(*, tenant_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """把一条审计异步落库。

    ★ 必须带 `@tenant_task`：`AuditLog` 是**租户级表**，任务会写它。
      少了上下文，`AuditLogRepository.create()` 会因守卫拒绝而抛错
      （写路径的守卫比读路径更硬——写错租户比读错租户后果更严重）。
    """
    run_async(_persist(payload))
    logger.info("audit_persisted_async", action=payload.get("action"), tenant_id=tenant_id)
    return {"tenant_id": tenant_id, "action": payload.get("action")}
