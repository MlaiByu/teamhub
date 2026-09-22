"""部门路由（PROJECT-PLAN 8.4）。

GET  /departments   部门树（当前租户）
POST /departments   创建部门（TENANT_ADMIN）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_tenant_id, require_roles
from app.core.constants import TENANT_ADMIN_ROLE
from app.core.db.session import get_db
from app.core.responses import Envelope, ok
from app.schemas.org import DepartmentCreateRequest, DepartmentNode, DepartmentOut
from app.services import org_service

router = APIRouter(prefix="/departments", tags=["组织"])


@router.get(
    "",
    response_model=Envelope[list[DepartmentNode]],
    summary="部门树",
    description=(
        "返回当前租户完整的部门树（嵌套结构）。\n\n"
        "服务端直接建树而不是返回平铺列表：让各客户端各自建树是重复劳动，"
        "且排序规则、孤儿节点处理容易不一致。"
    ),
)
async def list_departments(
    session: AsyncSession = Depends(get_db),
    # ★ 必须有这个依赖：部门树是租户级数据，没它的话匿名请求会一路打到
    #   repository 的 require_tenant_context() 才炸成 500——
    #   该在这里就拒绝成 401，而不是漏到守卫那层变成内部错误。
    _: int = Depends(get_current_tenant_id),
) -> dict:
    return ok(await org_service.get_department_tree(session))


@router.post(
    "",
    response_model=Envelope[DepartmentOut],
    status_code=status.HTTP_201_CREATED,
    summary="创建部门",
    description=(
        "在当前租户下创建部门。`parent_id` 不传表示根部门。\n\n"
        "需要 `TENANT_ADMIN` 角色。\n\n"
        "上级部门属于**别的租户**时返回 404（不是 403）——避免通过报错推断部门是否存在。"
    ),
)
async def create_department(
    payload: DepartmentCreateRequest,
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(require_roles(TENANT_ADMIN_ROLE)),
) -> dict:
    dept = await org_service.create_department(
        session,
        name=payload.name,
        parent_id=payload.parent_id,
        leader_id=payload.leader_id,
    )
    return ok(DepartmentOut.model_validate(dept), message="已创建")
