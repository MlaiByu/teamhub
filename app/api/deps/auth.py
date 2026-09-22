"""鉴权依赖。

★ 分层：**中间件只做「解析 + 写上下文」，依赖层做「拒绝」**。

    中间件（tenant_context.py）：验签成功就写 claims 与 contextvar；失败只记日志
    依赖层（本文件）：      按接口要求决定「必须有登录」「必须有租户」「必须有角色」

  这样分工的理由：匿名接口（/health、/docs、/auth/login）也要经过中间件，
  如果中间件直接拒绝，这些接口就得开白名单——白名单最容易漏配，
  而且漏配的后果是「本该公开的接口被拦」这种一眼可见的问题，
  反过来「本该受保护的接口被放过」才是灾难。把拒绝权交给逐个接口
  显式声明的依赖，漏配会被 `require_*` 的名字暴露出来。
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Depends, Request

from app.core.db.context import current_data_scope, current_dept_id, current_tenant_id
from app.core.exceptions import ForbiddenError, UnauthenticatedError


async def get_current_claims(request: Request) -> dict[str, Any]:
    """取经过验签的 token 声明。没有有效 token 就 401。

    读的是 `request.state.claims` 而不是重新解一次 token——中间件已经做过
    验签，重复解析纯属浪费，而且两处实现容易漂移。
    """
    claims = getattr(request.state, "claims", None)
    if not claims:
        raise UnauthenticatedError("请先登录")
    return claims


async def get_current_user_id(claims: dict[str, Any] = Depends(get_current_claims)) -> int:
    """当前用户 ID。`User` 是全局表，这个依赖不要求租户上下文。"""
    sub = claims.get("sub")
    if not sub:
        raise UnauthenticatedError("令牌缺少用户标识")
    return int(sub)


async def get_current_tenant_id() -> int:
    """当前租户 ID。需要租户的接口用这个。

    ★ 从 **contextvar** 取而不是从 claims 取：两者都源自同一个 token，
      但 contextvar 是「钩子实际用来过滤的那个值」。从这里取能保证
      「鉴权看到的租户」与「查询过滤用的租户」永远是同一个，
      不会出现双方各读一份、某天漂移的错位。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise UnauthenticatedError("当前令牌未绑定团队，请重新登录并选择团队")
    return tenant_id


async def get_current_dept_id() -> int | None:
    """当前用户在当前租户内的部门 ID。DEPT 数据范围的判定依据。"""
    return current_dept_id.get()


async def get_current_data_scope() -> str:
    return str(current_data_scope.get())


def require_roles(*role_codes: str) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """生成「必须拥有其中任一角色」的依赖。

    用法：
        @router.post("/roles", dependencies=[Depends(require_roles(TENANT_ADMIN_ROLE))])

    ★ 角色码来自 token 声明，不查库。代价是「改了角色后旧 token 仍然带着老角色，
      直到 access token 过期（默认 30 分钟）」。这是有意的取舍：
      每个请求查一次角色表会把鉴权变成热路径上的额外往返。
      需要立即生效的场景（禁用管理员）靠「撤销 refresh token + 缩短 access 有效期」
      来处理，而不是靠每请求查库。
    """
    if not role_codes:
        raise ValueError("require_roles 至少要传一个角色码")

    async def _dependency(claims: dict[str, Any] = Depends(get_current_claims)) -> dict[str, Any]:
        granted = set(claims.get("roles") or [])
        if not granted.intersection(role_codes):
            # 403 而非 404：这里没有存在性泄露问题（角色是调用者自己的属性），
            # 明确告知「权限不足」比含糊其辞更有助于前端给出正确提示。
            raise ForbiddenError(f"需要以下角色之一：{', '.join(role_codes)}")
        return claims

    return _dependency
