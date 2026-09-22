"""解析 JWT → 写入 contextvar（租户/用户/部门/数据范围）。

★ 关键设计：中间件**不拒绝**匿名请求，只负责「有 token 就设上下文」。
    是否必须登录由 api/deps/auth.py 决定。
    这样 /health、/docs、/auth/login 这些接口可以正常匿名访问。

★ 请求结束必须 reset_context()，否则 contextvar 会污染同线程的后续请求。
"""

from __future__ import annotations

import jwt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.constants import DataScope
from app.core.db.context import (
    RequestContext,
    current_client_ip,
    reset_context,
    set_context,
)
from app.core.logging import get_logger
from app.core.security import decode_token

logger = get_logger(__name__)


def _client_ip(request: Request) -> str | None:
    """取客户端 IP，优先可信代理头。

    ★ 直连部署时 `request.client.host` 就是真实 IP；但一旦前面有反代/Nginx，
      它就变成反代的 IP。所以优先读 `X-Forwarded-For` 的第一个（最靠近客户端的）。
      注意：这个头**客户端可伪造**，只用于审计留痕、不做安全决策——
      真正做限流/封禁时要用「可信代理白名单」来判定，不能盲信。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        current_client_ip.set(_client_ip(request))
        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")

        if scheme.lower() == "bearer" and token:
            try:
                payload = decode_token(token)
                if payload.get("type") != "access":
                    raise jwt.InvalidTokenError("not an access token")

                # ★ 无条件写入 claims，与租户无关。
                #   此前只有「带 tenant_id 的 token」才会写，导致不带租户的
                #   access token（刚注册、未加入任何团队）虽然验签通过了，
                #   依赖层却拿不到 claims，只能当匿名处理——用户会看到
                #   「已登录但访问 /auth/me 返回未认证」这种自相矛盾的行为。
                request.state.claims = payload

                tenant_id = payload.get("tenant_id")
                if tenant_id is not None:
                    set_context(
                        RequestContext(
                            tenant_id=int(tenant_id),
                            user_id=int(payload["sub"]) if payload.get("sub") else None,
                            dept_id=payload.get("dept_id"),
                            data_scope=DataScope(payload.get("data_scope") or "SELF"),
                        )
                    )
                # 无租户的 access token：不设租户上下文。
                # 此时钩子不注入租户过滤，但 repository 守卫会在需要租户的
                # 查询上直接拒绝——这正是依赖层想要的「需要租户的接口拒绝掉」。
                request.state.tenant_id = tenant_id
            except jwt.PyJWTError as exc:
                # 不在这里返回 401：交给 deps，保证错误信封统一
                logger.debug("jwt_decode_failed", reason=str(exc))

        try:
            return await call_next(request)
        finally:
            reset_context()
