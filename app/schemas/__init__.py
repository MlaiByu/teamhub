"""Pydantic v2 入参/出参 schema，按领域分文件。

★ 出参一律用独立 schema（配 `from_attributes=True`），**不直接序列化 ORM 对象**。
  好处是「哪些字段可以出到 API」在一处显式可见——`password_hash` 这类字段
  只要没写进 schema，就不可能被漏出去。
"""

from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageData, PageParams
from app.schemas.tenant import (
    TenantBrief,
    TenantCreateRequest,
    TenantMemberOut,
    TenantOut,
)
from app.schemas.user import ChangePasswordRequest, UserBrief, UserOut

__all__ = [
    # auth
    "RegisterRequest",
    "RegisterResponse",
    "LoginRequest",
    "RefreshRequest",
    "LogoutRequest",
    "TokenResponse",
    "AuthUser",
    # common
    "PageParams",
    "PageData",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    # user
    "UserOut",
    "UserBrief",
    "ChangePasswordRequest",
    # tenant
    "TenantOut",
    "TenantBrief",
    "TenantCreateRequest",
    "TenantMemberOut",
]
