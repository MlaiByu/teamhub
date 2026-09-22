"""系统路由：健康检查 / 上下文自检。

`/system/context` 是一个**诊断接口**，用于手工确认「钩子当前是否在生效」——
不依赖测试代码就能看到当前租户上下文与过滤状态。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.db.context import (
    current_data_scope,
    current_dept_id,
    current_tenant_id,
    current_user_id,
    is_bypass,
)

router = APIRouter(prefix="/system", tags=["系统"])


@router.get("/context", summary="查看当前租户上下文（诊断用）")
async def read_context() -> dict:
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "tenant_id": current_tenant_id.get(),
            "user_id": current_user_id.get(),
            "dept_id": current_dept_id.get(),
            "data_scope": str(current_data_scope.get()),
            "bypass": is_bypass(),
            "isolation_active": current_tenant_id.get() is not None and not is_bypass(),
        },
    }
