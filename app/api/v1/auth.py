"""认证路由（PROJECT-PLAN 8.4）。

    POST /auth/register   注册（并开通个人团队）
    POST /auth/login      登录
    POST /auth/refresh    用 refresh 换新令牌对（轮换）
    POST /auth/logout     登出（撤销整条令牌链）
    GET  /auth/me         当前身份（含租户与授权信息）

★ 响应统一用 `response_model=Envelope[X]` 声明：
  `/docs` 里展示的是真实结构，前端可以据此生成类型。
  业务码由 `ok()` 填充，HTTP 状态码与业务码的映射在 `core/constants.BizCode`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_claims, get_current_tenant_id
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.auth import (
    AuthUser,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.schemas.tenant import TenantBrief
from app.schemas.user import UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=Envelope[RegisterResponse],
    status_code=status.HTTP_201_CREATED,
    summary="注册（并开通个人团队）",
    description=(
        "注册一个账号，并为其开通个人团队、绑定团队管理员角色。\n\n"
        "**为什么要同时建团队**：`refresh_tokens` 是租户级表（`tenant_id` 非空），"
        "没有团队就无法签发 refresh token，登录会走进死胡同。"
        "自助注册即「开一个团队」，平台管理员补开租户的接口另行提供。"
    ),
)
async def register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    result = await auth_service.register(session, payload=payload)
    return ok(
        RegisterResponse(
            user=UserOut.model_validate(result.user),
            tenant=TenantBrief.model_validate(result.tenant),
            token=result.token,
        )
    )


@router.post(
    "/login",
    response_model=Envelope[TokenResponse],
    summary="登录",
    description=(
        "用户名 + 密码换取令牌对。\n\n"
        "`tenant_id` 只在账号属于**多个**团队时才需要传——"
        "单一团队会自动选中，多团队不传会返回 409 提示指定。"
    ),
)
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    result = await auth_service.login(session, payload=payload)
    return ok(result.token)


@router.post(
    "/refresh",
    response_model=Envelope[TokenResponse],
    summary="刷新令牌（轮换）",
    description=(
        "用 refresh token 换取新的 access + refresh，**旧的立即失效**（rotate on use）。\n\n"
        "如果传入一个已经被撤销的 refresh token，会被判定为令牌泄露，"
        "该账号在当前团队内的**全部**会话将被撤销。"
    ),
)
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await auth_service.refresh(session, refresh_token=payload.refresh_token))


@router.post(
    "/logout",
    response_model=Envelope[None],
    summary="登出",
    description=(
        "撤销该账号在当前团队内的整条令牌链。\n\n"
        "**幂等**：令牌已失效时同样返回成功——登出接口报错会让前端"
        "陷入「想登出但登不出去」的状态。"
    ),
)
async def logout(
    payload: LogoutRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    await auth_service.logout(session, refresh_token=payload.refresh_token)
    return ok(None, message="已登出")


@router.get(
    "/me",
    response_model=Envelope[AuthUser],
    # 用 dependencies=[...] 而不是把值接进参数：这里只需要「必须有租户」这个
    # 副作用，不需要用到那个 id。接进参数反而会留下一个未使用变量。
    dependencies=[Depends(get_current_tenant_id)],
    summary="当前身份",
    description=(
        "返回当前登录账号、所属团队与授权信息。\n\n"
        "`data_scope` 与 `roles` 直接来自 token 声明——与后端实际执行的过滤同源，"
        "所以前端看到的权限必然等于后端会执行的权限，不会错位。"
    ),
)
async def read_me(
    claims: dict = Depends(get_current_claims),
    session: AsyncSession = Depends(get_db),
) -> dict:
    user = await auth_service.load_user(session, int(claims["sub"]))
    tenant = await auth_service.load_current_tenant(session)
    return ok(
        AuthUser(
            user=UserOut.model_validate(user),
            tenant=TenantBrief.model_validate(tenant),
            data_scope=str(claims.get("data_scope") or "SELF"),
            roles=list(claims.get("roles") or []),
        )
    )
