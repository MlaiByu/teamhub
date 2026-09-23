"""通知服务：事件的落库与投递。

★ 这是「事件被处理」的唯一入口。每个事件类型最终都走到 `emit`：
      emit → 按类型校验 payload → 落库 notifications → 推送 WebSocket

★ 三条关于时序的硬约定，写在最前面，改这个模块前先读：

  1. **必须在业务变更 commit 之后调用 `emit`。**
     通知不是关键数据——它的写入失败绝不该让「任务已创建」这类操作回滚
     或返回 500。所以 `emit` 用**独立事务**落库，且异常全部吞掉只留日志。

  2. **推送必须在落库之后。**
     反过来（先推后存）一旦落库失败，客户端就收到一条永远不会出现在
     通知列表里的「幽灵通知」。先存后推最坏情况是「存了但没推到」——
     用户下次拉列表仍能看到，是自愈的。

  3. **调用 `emit` 时，业务变更应已提交。**
     `emit` 会 `commit()`，如果调用方还留着未提交的变更，会被一并提交。
     这是使用本模块唯一的注意事项。

  为什么不把通知塞进业务事务（看似更「原子」）：那样业务成功但通知写不进去时，
  整个业务操作要因为「通知失败」而失败——本末倒置。通知是可降级的旁路。

  若要严格做到「不丢通知 + 不幽灵推送」，需要 outbox 模式（通知先落库，
  再由 Celery 扫描投递）。本项目当前阶段不做，但 `emit` 的签名与调用位置
  已经为它留好了位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.constants import MemberStatus
from app.core.db.context import current_tenant_id
from app.core.logging import get_logger
from app.realtime.events import EventType, NotificationEvent, render_title
from app.realtime.manager import ConnectionManager, manager
from app.repositories.notification import NotificationRepository
from app.repositories.tenant import TenantMemberRepository
from app.schemas.notification import NotificationOut

logger = get_logger(__name__)


@dataclass(frozen=True)
class MarkReadResult:
    """标记已读的结果。

    `found=False` 表示「不存在或不属于该用户」→ 调用方报 404；
    `found=True, updated=0` 表示「本来就是已读」→ 幂等成功。
    """

    found: bool
    updated: int


async def emit(
    session: AsyncSession,
    *,
    event_type: EventType | str,
    target_user_id: int,
    actor_id: int | None = None,
    connection_manager: ConnectionManager | None = None,
    **payload: Any,
) -> NotificationEvent | None:
    """落库一条通知并推送给目标用户。返回落库成功的事件；失败返回 None。

    ★ 为什么失败返回 None 而不是抛异常：
      `emit` 的调用点在业务操作**成功之后**。此时因为「通知没写进去」
      而向上抛错，会让客户端以为业务操作失败了——实际上数据已经变了，
      用户会重复提交。所以这里降级：记 ERROR 日志、返回 None，让业务照常返回成功。

      注意 ERROR 级别的选择是有意的：静默降级会掩盖真实故障
      （比如租户上下文丢失——那通常意味着调用点写错了），
      所以要让它在日志里显眼，而不是 debug 掉。

    ★ 租户来自 **contextvar**，不接参数：
      单一来源，且强制调用方（含将来的 Celery 任务）先 `set_context`——
      这正是 PROJECT-PLAN 风险 2「Celery 任务没有请求上下文」要求做的事。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        logger.error(
            "notification_emit_without_tenant_context",
            event_type=str(event_type),
            target_user_id=target_user_id,
            hint="emit 必须在已设租户上下文的场景调用；Celery 任务需在入口 set_context()",
        )
        return None

    try:
        # 校验 payload 并构造事件（类型未注册 / 字段不对会在这里抛）
        event = NotificationEvent.create(
            event_type=event_type,
            tenant_id=tenant_id,
            target_user_id=target_user_id,
            actor_id=actor_id,
            **payload,
        )
    except Exception:
        # payload 不合规是**开发期错误**，但同样不能让业务接口 500
        logger.exception(
            "notification_payload_invalid",
            event_type=str(event_type),
            target_user_id=target_user_id,
        )
        return None

    # ★ 结构性保证：通知只发给**当前租户的 ACTIVE 成员**。
    #
    #   放在 emit 这个唯一入口，而不是让每个触发点各自校验——触发点会越来越多
    #   （任务分配、状态流转、评论、提及、成员加入、角色授予…），靠人记得必然漏一处。
    #   漏掉的后果很具体：给已经离开团队 / 属于别的公司的人发通知。
    #   虽然对方因为拿不到该租户的 token 而看不到，但通知行会真的写进库、
    #   甚至真的推到 socket 上（若连接还挂着）。
    #
    #   放在这里之后，整个系统这条不变量只依赖一处代码。
    #
    #   ★ 顺序必须是**落库之前**。若放在落库之后，行已经写进库了，
    #     守卫只能做到「不推送」——库里会留下一批本不该存在的通知，
    #     用户下次拉列表照样看得到。校验要在写之前。
    #   代价是每次 emit 多一次按索引的主键查询——相对正确性可以接受。
    try:
        member = await TenantMemberRepository(session).get_membership(target_user_id)
    except Exception:
        # 上下文/会话异常等：按「无法确认，故不发」处理，不向上抛
        logger.exception("notification_membership_check_failed", target_user_id=target_user_id)
        return None

    if member is None or member.status != str(MemberStatus.ACTIVE):
        logger.info(
            "notification_target_not_active_member",
            event_type=str(event_type),
            target_user_id=target_user_id,
        )
        return None

    try:
        repository = NotificationRepository(session)
        repository.create(
            user_id=event.target_user_id,
            type=str(event.type),
            payload=event.payload,
            read_at=None,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception(
            "notification_persist_failed",
            event_type=str(event.type),
            target_user_id=event.target_user_id,
        )
        return None

    # 落库成功后才推送——顺序反了会产生「幽灵通知」
    active_manager = connection_manager or manager
    delivered = await active_manager.send_to_user(
        tenant_id=event.tenant_id,
        user_id=event.target_user_id,
        message=event.to_message(),
    )
    logger.info(
        "notification_emitted",
        event_type=str(event.type),
        tenant_id=event.tenant_id,
        target_user_id=event.target_user_id,
        connections=delivered,
    )

    # 失效该用户的未读计数缓存（下面「读时缓存」的配套写时失效）
    await invalidate_unread_cache(tenant_id=event.tenant_id, user_id=event.target_user_id)

    return event


async def emit_to_many(
    session: AsyncSession,
    *,
    event_type: EventType | str,
    target_user_ids: list[int],
    actor_id: int | None = None,
    connection_manager: ConnectionManager | None = None,
    **payload: Any,
) -> list[NotificationEvent]:
    """给多个接收人各发一条通知（如评论里 @ 了多个人）。

    ★ 去重是必须的：同一条评论里 `@张三 @张三` 不该产生两条通知；
      在 API 层做去重意味着「客户端传什么就发什么」，所以在这里兜住。
      另外**排除触发者自己**——@ 自己不该给自己发通知。
    """
    unique_targets = []
    seen: set[int] = set()
    for uid in target_user_ids:
        if uid == actor_id or uid in seen:
            continue
        seen.add(uid)
        unique_targets.append(uid)

    events: list[NotificationEvent] = []
    for uid in unique_targets:
        event = await emit(
            session,
            event_type=event_type,
            target_user_id=uid,
            actor_id=actor_id,
            connection_manager=connection_manager,
            **payload,
        )
        if event is not None:
            events.append(event)
    return events


# ----------------------------------------------------------------------
# 通知查询（接口层用）
# ----------------------------------------------------------------------
def _to_out(row) -> NotificationOut:
    """ORM 行 → 对外结构，顺带渲染标题。"""
    return NotificationOut(
        id=row.id,
        event=row.type,
        title=render_title(row.type, row.payload or {}),
        payload=row.payload or {},
        is_read=row.read_at is not None,
        read_at=row.read_at,
        created_at=row.created_at,
    )


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: int,
    unread_only: bool,
    page: int,
    page_size: int,
) -> tuple[list[NotificationOut], int]:
    rows, total = await NotificationRepository(session).paginate_for_user(
        user_id=user_id, unread_only=unread_only, page=page, page_size=page_size
    )
    return [_to_out(r) for r in rows], total


UNREAD_CACHE_TTL = 30


async def invalidate_unread_cache(*, tenant_id: int, user_id: int) -> None:
    """失效某用户的未读计数缓存。

    ★ 所有会改变未读数的路径都必须调它，否则角标会显示旧值：
      新通知落库（`emit` / `emit_to_many`）、标记单条已读、全部已读。
      TTL 只是兜底（防止漏失效时永久错），不是主要机制。
    """
    await cache.cache_delete(tenant_id, "notifications", "unread", user_id)


async def count_unread(session: AsyncSession, *, user_id: int) -> int:
    """当前租户内该用户的未读数（带缓存）。

    ★ 为什么给它加缓存：这是前端角标的轮询接口——打开着页面就会
      持续请求，是真正的热点读。而它后面是一次 COUNT 查询（全表扫该用户的通知），
      在通知量大的租户里不便宜。

    ★ 一致性策略：**读时缓存 + 写时失效**。TTL 取 30s 只是兜底；
      真正的正确性靠上面所有写路径主动 `invalidate`（含 WebSocket 推送之前），
      所以用户看到角标变化的延迟是「一次缓存删除」而不是 30 秒。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        # 无租户上下文：不做缓存（缓存 key 需要租户前缀，无租户无从构造）
        return await NotificationRepository(session).count_unread(user_id=user_id)

    async def factory() -> int:
        return await NotificationRepository(session).count_unread(user_id=user_id)

    return await cache.cache_get_or_set(
        tenant_id,
        "notifications",
        "unread",
        user_id,
        factory=factory,
        ttl=UNREAD_CACHE_TTL,
    )


async def mark_read(session: AsyncSession, *, user_id: int, notification_id: int) -> MarkReadResult:
    """标记单条已读。

    ★ 先查再改，而不是只看 UPDATE 的 rowcount：
      rowcount=0 有两种含义——「不存在/不是你的」和「本来就是已读」。
      前者该报 404，后者该幂等成功。只看 rowcount 会把「重复标记已读」
      误报成 404，让前端的重试逻辑以为通知丢了。

      查询本身带 `user_id` 条件，所以别人的通知会走到「不存在」分支 ——
      统一 404，不区分「存在但归别人」，避免存在性泄露。
    """
    repository = NotificationRepository(session)
    row = await repository.get_for_user(notification_id=notification_id, user_id=user_id)
    if row is None:
        return MarkReadResult(found=False, updated=0)

    if row.read_at is not None:
        return MarkReadResult(found=True, updated=0)

    updated = await repository.mark_one_read(notification_id=notification_id, user_id=user_id)
    await session.commit()

    if updated:
        tenant_id = current_tenant_id.get()
        if tenant_id is not None:
            await invalidate_unread_cache(tenant_id=tenant_id, user_id=user_id)

    return MarkReadResult(found=True, updated=updated)


async def mark_all_read(session: AsyncSession, *, user_id: int) -> int:
    updated = await NotificationRepository(session).mark_all_read(user_id=user_id)
    await session.commit()

    tenant_id = current_tenant_id.get()
    if tenant_id is not None:
        await invalidate_unread_cache(tenant_id=tenant_id, user_id=user_id)

    return updated


__all__ = [
    "MarkReadResult",
    "emit",
    "emit_to_many",
    "list_notifications",
    "count_unread",
    "invalidate_unread_cache",
    "mark_read",
    "mark_all_read",
]
