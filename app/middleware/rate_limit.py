"""Redis 滑动窗口限流，按租户配额（阶段 3 完整实现）。

第 1 周只放占位，保证中间件栈顺序固定；阶段 3 用 Lua 脚本实现原子的
ZSET 滑动窗口（ZREMRANGEBYSCORE + ZCARD + ZADD + EXPIRE）。

★ 缓存/限流 key 必须带租户前缀，否则跨租户串数据（PROJECT-PLAN 九·风险 9）。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings
from app.core.db.context import current_tenant_id

RATE_LIMIT_KEY_PREFIX = "ratelimit"


def build_key(tenant_id: int, identity: str = "global") -> str:
    """集中构造 key，杜绝手写漏前缀。

    形如 ratelimit:tenant:1:global
    """
    return f"{RATE_LIMIT_KEY_PREFIX}:tenant:{tenant_id}:{identity}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """阶段 1 空实现：仅确保调用链与 key 规范先确立。"""

    enabled: bool = False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.enabled:
            return await call_next(request)

        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            return await call_next(request)

        key = build_key(tenant_id)
        # TODO(阶段3): Redis 滑动窗口；超限抛 RateLimitedError(BizCode.RATE_LIMITED)
        _ = (key, settings.rate_limit_per_minute)
        return await call_next(request)
