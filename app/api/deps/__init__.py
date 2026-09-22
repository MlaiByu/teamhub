"""依赖注入，按关注点分文件（架构规则 3）。

    auth.py        身份与权限（claims / 用户 / 租户 / 角色守卫）
    pagination.py  统一分页参数

新增依赖请放对应文件，不要在这里堆一个巨大的 deps.py。
"""

from app.api.deps.auth import (
    get_current_claims,
    get_current_data_scope,
    get_current_dept_id,
    get_current_tenant_id,
    get_current_user_id,
    require_roles,
)
from app.api.deps.pagination import build_page_meta, pagination

__all__ = [
    "get_current_claims",
    "get_current_user_id",
    "get_current_tenant_id",
    "get_current_dept_id",
    "get_current_data_scope",
    "require_roles",
    "pagination",
    "build_page_meta",
]
