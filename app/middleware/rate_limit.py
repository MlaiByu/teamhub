"""Redis 滑动窗口限流中间件，按租户配额（PROJECT-PLAN 8.4 / 风险 10）。

  链路位置：RequestID → TenantContext → **RateLimit** → 路由

  放在 TenantContext **内层**，是为了能读到 contextvar 里的租户 ID。
  没有租户上下文的请求（`/health`、登录、无租户令牌）**不参与租户配额**——
  它们不属于任何租户，按 IP 限流是另一套机制，不在这里做。

★★ 超限时为什么**自己构造响应**而不是抛 `RateLimitedError`：

  FastAPI 的 `exception_handler` 由 `ExceptionMiddleware` 执行，而它位于
  中间件栈的**内层**。中间件在 `call_next` 之前抛出的异常根本到不了那里，
  会直接冒泡成 500（或由 ServerErrorMiddleware 兜底成纯文本），
  **业务码 42900 与信封格式全丢**。所以必须自己返回 `JSONResponse`。

  （这一点与路由层不同：路由里抛异常是能被 handler 接住的，
  因为那时执行流已经进入 ExceptionMiddleware 的内层。）

★ 限流 key 带租户前缀（`ratelimit:tenant:<id>:global`），
  由 `core/rate_limit.py::build_key` 集中构造——见该模块对「不用 Lua」的说明。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.constants import BizCode
from app.core.db.context import current_tenant_id
from app.core.logging import get_logger
from app.core.rate_limit import check_rate_limit
from app.core.responses import fail

logger = get_logger(__name__)

# 剩余配额/上限回给客户端的响应头（便于前端做退避提示与联调）
HEADER_LIMIT = "X-RateLimit-Limit"
HEADER_REMAINING = "X-RateLimit-Remaining"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            # 无租户上下文：不参与租户配额（不属于任何租户）
            return await call_next(request)

        limit = settings.rate_limit_per_minute
        allowed, remaining = await check_rate_limit(tenant_id, limit=limit)

        if not allowed:
            logger.warning(
                "rate_limited",
                tenant_id=tenant_id,
                limit=limit,
                path=request.url.path,
            )
            return JSONResponse(
                status_code=BizCode.http_status(BizCode.RATE_LIMITED),
                content=fail(
                    BizCode.RATE_LIMITED,
                    f"请求过于频繁（限额 {limit} 次/分钟），请稍后再试",
                ),
                headers={HEADER_LIMIT: str(limit), HEADER_REMAINING: "0"},
            )

        response = await call_next(request)
        response.headers[HEADER_LIMIT] = str(limit)
        response.headers[HEADER_REMAINING] = str(remaining)
        return response
