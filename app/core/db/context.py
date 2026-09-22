"""租户/数据范围的上下文变量（contextvar）。

放在 core/db/ 内，供钩子与 repository 共用。
请求进入时由 middlewar/tenant_context.py 写入，请求结束复位。
Celery 任务必须在其入口显式 set_context()，出口 reset()（PROJECT-PLAN 九·风险2）。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from app.core.constants import DataScope

# 当前请求/任务的租户 ID。None 表示「未设置上下文」——此时钩子不过滤，
# 会查到全量数据。这是本项目最大的风险面，由 repository 入口兜底拒绝。
current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)
current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_dept_id: ContextVar[int | None] = ContextVar("current_dept_id", default=None)
# 数据范围取多角色并集中最宽的那个，由 rbac 在签发 token 时算好写入。
current_data_scope: ContextVar[DataScope] = ContextVar("current_data_scope", default=DataScope.SELF)
# 平台级绕过开关。只有显式 bypass 路径会打开，且必须落审计。
_current_bypass: ContextVar[bool] = ContextVar("current_bypass", default=False)


@dataclass(frozen=True)
class RequestContext:
    """一次性快照，便于测试与任务入口整体设置。"""

    tenant_id: int | None
    user_id: int | None = None
    dept_id: int | None = None
    data_scope: DataScope = DataScope.SELF
    bypass: bool = False


def set_context(ctx: RequestContext) -> None:
    current_tenant_id.set(ctx.tenant_id)
    current_user_id.set(ctx.user_id)
    current_dept_id.set(ctx.dept_id)
    current_data_scope.set(ctx.data_scope)
    _current_bypass.set(ctx.bypass)


def reset_context() -> None:
    """清空上下文。注意：清空 ≠ 安全，反而会变成全量可见（风险 1）。

    后台任务与脚本结束前必须复位，避免污染后续复用的线程/连接。
    """
    current_tenant_id.set(None)
    current_user_id.set(None)
    current_dept_id.set(None)
    current_data_scope.set(DataScope.SELF)
    _current_bypass.set(False)


def is_bypass() -> bool:
    return _current_bypass.get()


def snapshot_context() -> RequestContext:
    """把当前上下文整体取出，用于「借用后恢复」。

    ★ 存在的理由：`bypass_context` 这类作用域必须能**还原**调用前的状态，
      而不是退出时一律清空。清空会把外层已有租户上下文一起抹掉——
      静默降级为全量可见，且没有日志。
    """
    return RequestContext(
        tenant_id=current_tenant_id.get(),
        user_id=current_user_id.get(),
        dept_id=current_dept_id.get(),
        data_scope=current_data_scope.get(),
        bypass=_current_bypass.get(),
    )


def restore_context(snapshot: RequestContext) -> None:
    """恢复 snapshot_context() 取出的上下文。"""
    set_context(snapshot)
