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
from app.core.db.context import RequestContext, reset_context, set_context
from app.core.logging import get_logger
from app.core.security import decode_token

logger = get_logger(__name__)


class TenantContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
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
