"""core/db 包入口。

★ 关键：这里必须「一条语句导入全部四件套」，保证任何 import app.core.db 的地方
都已经完成：Base 定义 → Mixin 定义 → 模型注册 → 钩子注册。
顺序不能调换，否则钩子在引擎创建后才注册，第一条查询会漏过滤。
"""

# 必须在最后导入：它把 do_orm_execute 监听器挂到 Session 类上。
from app.core.db import tenant_hook  # noqa: E402, F401
from app.core.db.base import Base, TimestampMixin, pk_column
from app.core.db.context import (
    RequestContext,
    current_data_scope,
    current_dept_id,
    current_tenant_id,
    current_user_id,
    is_bypass,
    reset_context,
    set_context,
)
from app.core.db.mixins import DataScopedMixin, TenantScopedMixin

__all__ = [
    "Base",
    "TimestampMixin",
    "pk_column",
    "TenantScopedMixin",
    "DataScopedMixin",
    "RequestContext",
    "set_context",
    "reset_context",
    "is_bypass",
    "current_tenant_id",
    "current_user_id",
    "current_dept_id",
    "current_data_scope",
]
