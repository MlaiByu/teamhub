"""租户路由（PROJECT-PLAN 8.4）。

    GET  /tenants/current        当前租户信息
    POST /tenants/{id}/switch    切换租户（返回新 token）

`POST /tenants`（平台管理员开通租户）安排在 RBAC 接口层落地时一起做——
它需要平台级权限校验，单独成步更清晰。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_claims, get_current_tenant_id
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.auth import SwitchTenantResponse
from app.schemas.tenant import TenantBrief, TenantOut
from app.services import auth_service

router = APIRouter(prefix="/tenants", tags=["租户"])


@router.get(
    "/current",
    response_model=Envelope[TenantOut],
    summary="当前租户信息",
    description="返回当前令牌绑定的租户。需要带租户作用域的 access token。",
)
async def read_current_tenant(
    session: AsyncSession = Depends(get_db),
    # 只借它的「必须有租户」副作用，不用那个 id——租户实体由 service 从 contextvar 读
    _: int = Depends(get_current_tenant_id),
) -> dict:
    tenant = await auth_service.load_current_tenant(session)
    return ok(TenantOut.model_validate(tenant))


@router.post(
    "/{tenant_id}/switch",
    response_model=Envelope[SwitchTenantResponse],
    summary="切换租户（返回新 token）",
    description=(
        "重新签发一张目标租户作用域的 access + refresh。\n\n"
        "**切换即换身份**：access token 是租户作用域的，tenant_id 写进声明后"
        "它就是「这个用户在这个租户里的身份」。所以切换 = 换一张新 token。\n\n"
        "你不是目标租户的成员时返回 404（不是 403）——避免通过报错推断租户是否存在。"
    ),
)
async def switch_tenant(
    tenant_id: int,
    claims: dict = Depends(get_current_claims),
    session: AsyncSession = Depends(get_db),
) -> dict:
    result = await auth_service.switch_tenant(
        session, user_id=int(claims["sub"]), target_tenant_id=tenant_id
    )
    return ok(
        SwitchTenantResponse(
            tenant=TenantBrief.model_validate(result.tenant),
            token=result.token,
        )
    )
