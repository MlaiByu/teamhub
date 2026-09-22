"""任务领域 service。第 1 周只落地跨租户外键校验（矩阵 8），
完整的状态流转在第 4 周实现（PROJECT-PLAN 十二）。
"""

from __future__ import annotations

from typing import Protocol

from app.core.constants import TASK_STATUS_TRANSITIONS, TaskStatus
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class _TenantScoped(Protocol):
    tenant_id: int


def ensure_same_tenant(obj: _TenantScoped | None, *, expected_tenant_id: int) -> None:
    """校验关联对象与当前租户一致。

    ★ 为什么必须有这个函数：
        task.project_id 的外键只保证「projects 里存在这个 id」，
        **不保证这个 project 属于同一租户**。数据库层完全合法，
        但结果是把 A 租户的任务挂到了 B 租户的项目上——
        多租户里最隐蔽的一类数据污染。

    跨租户时抛 NotFound 语义（不是 403），避免存在性泄露。
    """
    from app.core.exceptions import TenantContextMissingError

    if obj is None:
        raise NotFoundError("关联资源不存在或无权访问")
    if obj.tenant_id != expected_tenant_id:
        logger.warning(
            "cross_tenant_mount_attempt",
            object_tenant=getattr(obj, "tenant_id", None),
            expected_tenant=expected_tenant_id,
        )
        # 注意：这里抛 TenantContextMissingError 而非 NotFoundError，
        # 因为它是**编程错误/恶意构造**，需要开发期立刻可见。
        # 面向外部 API 时由 api 层转成 404。
        raise TenantContextMissingError(
            f"拒接跨租户挂载：对象属于租户 {getattr(obj, 'tenant_id', None)}，"
            f"当前租户 {expected_tenant_id}"
        )


def validate_status_transition(current: str, target: str) -> None:
    """校验任务状态流转合法性（10.3：非法跳转被拒绝）。

    状态是固定枚举流转，不做可配置流程图（1.4 边界）。
    """
    try:
        cur, tgt = TaskStatus(current), TaskStatus(target)
    except ValueError as exc:
        raise ConflictError(f"非法的状态值：{exc}") from exc

    if cur == tgt:
        return
    allowed = TASK_STATUS_TRANSITIONS.get(cur, set())
    if tgt not in allowed:
        raise ConflictError(
            f"不允许从 {cur} 流转到 {tgt}；可选：{[s.value for s in allowed] or '无（终态）'}"
        )
