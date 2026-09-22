"""唯一数据入口层。业务代码不得直接使用 session.execute(select(...))。

两个基类按「模型是否带 tenant_id」分流：

    BaseRepository        全局表（User / Tenant / Permission）
    TenantAwareRepository 租户级表（继承 TenantScopedMixin 的全部业务表）

选错基类的后果：全局表套守卫 → 登录等无上下文场景永远失败；
租户表漏守卫 → 跨租户泄露。判断依据是模型有没有继承 TenantScopedMixin。
"""

from app.repositories.base import (
    BaseRepository,
    TenantAwareRepository,
    bypass_context,
)
from app.repositories.rbac import (
    PermissionRepository,
    RolePermissionRepository,
    RoleRepository,
    UserRoleRepository,
)
from app.repositories.refresh_token import RefreshTokenRepository
from app.repositories.tenant import TenantMemberRepository, TenantRepository
from app.repositories.user import UserRepository

__all__ = [
    # 基类
    "BaseRepository",
    "TenantAwareRepository",
    "bypass_context",
    # 全局表
    "UserRepository",
    "TenantRepository",
    "PermissionRepository",
    # 租户级表
    "TenantMemberRepository",
    "RefreshTokenRepository",
    "RoleRepository",
    "RolePermissionRepository",
    "UserRoleRepository",
]
