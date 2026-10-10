"""请求指标采集中间件（Prometheus，PROJECT-PLAN 11.4）。

  链路位置：CORS → RequestID → **Metrics** → TenantContext → RateLimit → 路由

★ 为什么放在 TenantContext **外**层：

  · 想让**被限流拒绝的请求（429）也计入指标**——限流发生在内层，
    若 Metrics 在它里面，429 就成了监控盲区，而那恰恰是要盯的指标之一。
  · 本中间件**不需要**租户上下文：指标标签刻意不带 `tenant_id`
    （基数 + 泄露，理由见 `core/metrics.py`）。

★ 异常路径也要计入：未捕获异常若不记录，监控里会表现为「这个接口
  从来没被调用过」，与「调用很多但都失败了」是完全不同的结论。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.metrics import (
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_IN_FLIGHT,
    HTTP_REQUESTS_TOTAL,
    route_label,
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        method = request.method
        # path 只在**响应之后**才准确：请求进来时路由还没匹配，
        # 拿不到路由模板（那时才知道要走哪个 endpoint）。
        HTTP_REQUESTS_IN_FLIGHT.inc()
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            self._record(
                method=method, path=route_label(request.scope), status="500", started=started
            )
            raise
        finally:
            HTTP_REQUESTS_IN_FLIGHT.dec()

        self._record(
            method=method,
            path=route_label(request.scope),
            status=str(response.status_code),
            started=started,
        )
        return response

    @staticmethod
    def _record(*, method: str, path: str, status: str, started: float) -> None:
        elapsed = time.perf_counter() - started
        HTTP_REQUEST_DURATION.observe(elapsed, method=method, path=path)
        HTTP_REQUESTS_TOTAL.inc(method=method, path=path, status=status)
