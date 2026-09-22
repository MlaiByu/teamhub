"""请求 ID 注入 + 日志绑定（链路第一步，PROJECT-PLAN 6.1）。"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger, new_request_id, request_id_var

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """优先复用上游传入的 X-Request-ID（便于跨服务串联），否则新生成。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(rid)
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
