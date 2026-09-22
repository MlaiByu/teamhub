"""实时通知的 WebSocket 端点（PROJECT-PLAN 8.4 / 风险 8）。

    WS /api/v1/ws

★ 握手必须校验 JWT 与租户（PROJECT-PLAN 风险 8：连接未校验租户 → 跨租户推送）。

  连接在 manager 里按 **(tenant_id, user_id)** 索引，所以握手阶段拿到的
  tenant_id 就是这条连接的身份。**校验和注册必须在 accept 之前完成**——
  先 accept 再校验的话，未认证的连接已经占住了服务端资源，
  而且一旦某条代码路径忘了断开，它就成了一条永久可推送的通道。

  ⚠️ **可观测行为**：在 `accept()` 之前 `close()`，按 ASGI 规范会变成
  **HTTP 403 响应**（而不是 WebSocket 关闭帧），所以客户端拿不到下面那个
  4401 关闭码，看到的是握手阶段 403。这是有意的取舍：宁可让未认证的连接
  完全建不起来，也不为了给一个「更漂亮的关闭码」而先 accept 再关。
  客户端应当把「握手 403」当作「令牌无效，去刷新后重连」处理。

★ 令牌两种传法：

  1. `Authorization: Bearer <token>` —— 非浏览器客户端（脚本、移动端）用
  2. `?token=<token>` —— **浏览器必须用这种**：WebSocket API 不允许设置自定义请求头

  查询串传令牌的代价是它可能进访问日志。生产环境更稳的做法是签发一次性的
  「WS 票据」（短有效期、用后即焚）来换连接，本项目当前阶段不做，
  但接口形态已经留好位置（换成读 `ticket` 参数即可）。

★ 令牌类型必须是 `access`：refresh token 换连接的话，等于把长有效期的凭据
  暴露在 URL 里，而且 refresh 只该用于换令牌这一个用途。
"""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.db.context import RequestContext, reset_context, set_context
from app.core.logging import get_logger
from app.core.security import decode_token
from app.realtime.manager import ConnectionManager, manager

logger = get_logger(__name__)

router = APIRouter(tags=["实时"])

# WebSocket 关闭码：4000-4999 区间留给应用自定义
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_BAD_REQUEST = 4400


def _extract_token(websocket: WebSocket, token_param: str | None) -> str | None:
    """优先用查询参数，其次 Authorization 头。

    两种都支持是为了兼顾「浏览器没法设请求头」与「脚本习惯用请求头」。
    """
    if token_param:
        return token_param
    header = websocket.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def _resolve_identity(raw_token: str) -> tuple[int, int]:
    """验签并取出 (tenant_id, user_id)。任何问题都抛 jwt 异常。"""
    payload = decode_token(raw_token)

    if payload.get("type") != "access":
        # refresh token 不该用来建立长连接：它有效期长，且唯一用途是换 access
        raise jwt.InvalidTokenError("not an access token")

    tenant_id = payload.get("tenant_id")
    if tenant_id is None:
        # 没有租户作用域的令牌无法确定该把连接挂在哪个租户下，
        # 而连接索引必须带租户（风险 8），所以只能拒绝。
        raise jwt.InvalidTokenError("token has no tenant scope")

    sub = payload.get("sub")
    if not sub:
        raise jwt.InvalidTokenError("token has no subject")

    return int(tenant_id), int(sub)


async def _serve(
    websocket: WebSocket,
    *,
    token_param: str | None,
    connection_manager: ConnectionManager,
) -> None:
    """握手 + 保持连接。抽成独立函数是为了能在测试里注入独立的 ConnectionManager。

    ★ 为什么不直接用模块级单例做测试：单例内部的 `asyncio.Lock` 会绑定到
      第一个使用它的 event loop。测试与生产、或不同测试之间跑在不同 loop 上时，
      复用同一个单例会抛「attached to a different loop」。
      所以真正需要隔离的地方（测试）自己 new 一个。
    """
    raw_token = _extract_token(websocket, token_param)
    if not raw_token:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="缺少访问令牌")
        return

    try:
        tenant_id, user_id = _resolve_identity(raw_token)
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        logger.info("ws_handshake_rejected", reason=str(exc))
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="令牌无效或已过期")
        return

    # 校验通过后才 accept —— 未认证的连接不该先占住服务端资源
    await websocket.accept()

    # 连接期间保持租户上下文：本连接后续若在同任务内触发查询
    # （将来推送前要查未读数等）也能拿到正确的租户。退出时必须复位，
    # 否则会污染复用该任务上下文的后续协程。
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id))
    await connection_manager.connect(tenant_id=tenant_id, user_id=user_id, websocket=websocket)

    try:
        await websocket.send_json(
            {"event": "connected", "tenant_id": tenant_id, "user_id": user_id}
        )
        # 保持连接：客户端的心跳/任意消息都会被读到并忽略。
        # 这个循环同时是「探测断开」的手段——客户端关闭时 receive 会抛
        # WebSocketDisconnect，我们才能清理连接表。
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # 例如客户端在 accept 之后立刻断开、或重复 receive 导致的协议错误
        logger.info("ws_receive_error", tenant_id=tenant_id, user_id=user_id, reason=str(exc))
    finally:
        await connection_manager.disconnect(
            tenant_id=tenant_id, user_id=user_id, websocket=websocket
        )
        reset_context()


@router.websocket("/ws")
async def realtime_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None, description="access token；浏览器无法设请求头时用它"),
) -> None:
    await _serve(websocket, token_param=token, connection_manager=manager)
