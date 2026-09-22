"""密码哈希与 JWT 签发/校验。

refresh token 必须落库记录 jti 与撤销状态，否则登出后旧 token 仍有效（7.3-3）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.constants import DataScope

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def _encode(
    *,
    user_id: int,
    token_type: TokenType,
    tenant_id: int | None = None,
    dept_id: int | None = None,
    data_scope: DataScope = DataScope.SELF,
    roles: list[str] | None = None,
    jti: str | None = None,
    expires_delta: timedelta,
) -> tuple[str, str]:
    """返回 (token, jti)。jti 需落库才能支持撤销与复用检测（8.2）。"""
    now = datetime.now(UTC)
    jti = jti or uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        # access token 带 tenant_id：token 即租户作用域（8.3 推荐方案）
        "tenant_id": tenant_id,
        "dept_id": dept_id,
        "data_scope": str(data_scope),
        "roles": roles or [],
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), jti


def create_access_token(
    *,
    user_id: int,
    tenant_id: int | None = None,
    dept_id: int | None = None,
    data_scope: DataScope = DataScope.SELF,
    roles: list[str] | None = None,
) -> str:
    token, _ = _encode(
        user_id=user_id,
        token_type="access",
        tenant_id=tenant_id,
        dept_id=dept_id,
        data_scope=data_scope,
        roles=roles,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )
    return token


def create_refresh_token(*, user_id: int, tenant_id: int | None = None) -> tuple[str, str]:
    return _encode(
        user_id=user_id,
        token_type="refresh",
        tenant_id=tenant_id,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str) -> dict[str, Any]:
    """解析并校验签名/过期。失败一律抛 jwt 异常，由 deps 转成 40100。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
