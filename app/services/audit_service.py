"""操作审计服务。

★ 审计的正确性**不能**依赖异步是否成功——所以这里同步落库，
  失败降级记 ERROR 日志但**绝不向上抛**。理由：审计是「事后可查」的旁路，
  如果因为审计写失败而让「任务已创建」这类业务操作回滚或 500，
  是本末倒置。这与 `notification_service.emit` 的降级策略完全一致。

  （方案十二把 Celery 异步任务排在**第 7 周**；第 5 周的验收标准是「审计可查」。
  等第 7 周引入 Celery 时，可以在这层把 `record` 换成 `record.delay(...)`，
  签名与调用点不变——这正是「旁路可以后置替换」的体现。）

★ 谁来记录：**只记录「有明确操作者 + 明确实体」的写操作**。
  读取操作不记（否则日志被刷爆、真正要查的事故被淹没）。
  实体类型用点分形式（`task` / `project` / `member` / `role` / `comment` /
  `attachment`），action 用「领域.动作」点分形式，与事件类型保持一致。

★ `detail` 里**不存**大字段（如评论全文、文件字节）——只存
  「能帮助事后回答『谁在什么时候对什么做了什么』」的最小键集。
  详情里含敏感信息，所以查询接口只给管理员开（下一步）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.context import current_client_ip, current_tenant_id, current_user_id
from app.core.logging import get_logger
from app.repositories.audit import AuditLogRepository

logger = get_logger(__name__)


async def record(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    detail: dict[str, Any] | None = None,
    user_id: int | None = None,
    ip: str | None = None,
) -> None:
    """落一条审计。任何失败都降级，绝不抛异常。

    ★ 为什么失败要吞掉：
      审计在业务操作**成功之后**调用（与 emit 同位置）。此时抛错会让客户端
      以为业务失败并重复提交。审计是可降级的旁路。

    ★ user_id 默认取 contextvar（当前登录用户），但允许显式覆盖：
      bypass 场景 / 系统级操作可能没有「当前用户」，需要显式指定或留空。
    """
    try:
        actor = user_id if user_id is not None else current_user_id.get()
        tenant_id = current_tenant_id.get()
        # 没有租户上下文就不记：审计行的 tenant_id 是隔离的关键，
        # 无上下文的记录没法归到任何租户，也查不到，等于白写还占库。
        if tenant_id is None:
            logger.warning("audit_skipped_no_tenant_context", action=action)
            return

        ip_value = ip if ip is not None else current_client_ip.get()
        AuditLogRepository(session).create(
            user_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail or {},
            ip=ip_value,
        )
        await session.commit()
    except Exception:
        # 审计失败绝不能回滚业务（业务变更已 commit，这里 rollback 只回审计）
        await session.rollback()
        logger.exception("audit_write_failed", action=action, entity_type=entity_type)


__all__ = ["record", "record_deferred"]


def record_deferred(
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    detail: dict[str, Any] | None = None,
    user_id: int | None = None,
    ip: str | None = None,
) -> None:
    """把审计交给 Celery **异步**落库（注意：**可能丢**）。

    ★ 与 `record()` 的取舍（这是本函数存在的唯一理由，用之前先读）：

      `record()`：同步落库，**可靠**。审计的价值就在于此，所以它是默认。
      `record_deferred()`：异步落库，不阻塞调用方，但 worker 故障 / 队列积压 /
        进程被杀时，这条审计就没了。

      所以只用它做「量大且可容忍丢失」的记录（高频读取埋点、用量统计）。
      **业务写操作一律用 `record()`。**

    ★ 为什么是普通函数而不是 `async def`：
      它只做「投递到队列」，没有需要 await 的 IO。

    ★ eager 模式（本地开发）下 `.delay()` 会**同步执行**，
      所以本地看到的仍然是「立刻落库」——方便调试，但别因此以为生产也是同步。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        # 没有租户上下文就不投递：审计行的 tenant_id 是隔离关键，
        # 无主的记录写了也查不到。与 record() 的处理保持一致。
        logger.warning("audit_deferred_skipped_no_tenant_context", action=action)
        return

    # 函数内 import：避免 services 模块在导入期就把 Celery 实例拉起来
    # （测试里只想用 services 时没必要初始化消息队列客户端）。
    from app.tasks.audit import persist_audit_log

    persist_audit_log.delay(
        tenant_id=tenant_id,
        payload={
            "user_id": user_id if user_id is not None else current_user_id.get(),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "detail": detail or {},
            "ip": ip if ip is not None else current_client_ip.get(),
        },
    )
