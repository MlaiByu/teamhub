"""项目路由（PROJECT-PLAN 8.4）。

    GET   /projects?status=&page=&page_size=   项目列表（分页 + 过滤）
    POST  /projects                            创建项目
    GET   /projects/{id}                       项目详情
    PATCH /projects/{id}                       局部更新

★ 这是 `DataScopedMixin` 首次挂上真实接口——`Project` 同时受
  「租户过滤」与「数据范围过滤」两层约束，两级都由 do_orm_execute 钩子注入。
  所以同一个 `GET /projects`，不同角色的用户看到的结果集天然不同：
      TENANT_ADMIN（ALL）→ 全租户
      DEPT_MANAGER（DEPT）→ 本部门
      MEMBER（SELF）→ 自己名下的
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_dept_id, get_current_tenant_id, get_current_user_id
from app.api.deps.pagination import pagination
from app.core.constants import ProjectStatus
from app.core.db.session import get_db
from app.core.responses import Envelope, PageData, ok, paged
from app.schemas.common import PageParams
from app.schemas.project import ProjectCreateRequest, ProjectOut, ProjectUpdateRequest
from app.services import project_service

router = APIRouter(prefix="/projects", tags=["项目"])


@router.get(
    "",
    response_model=Envelope[PageData[ProjectOut]],
    summary="项目列表",
    description=(
        "分页列出当前租户内、且落在**当前用户数据范围**内的项目。\n\n"
        "两级过滤都由查询层钩子自动注入，业务代码不手写 `where`：\n"
        "- 租户过滤：`tenant_id = 当前租户`\n"
        "- 数据范围：按 token 里的 `data_scope` 决定 SELF / DEPT / ALL\n\n"
        "`total` 与 `items` 用同一条查询算，不会出现「总数与列表对不上」。"
    ),
)
async def list_projects(
    status: ProjectStatus | None = Query(None, description="按项目状态过滤"),
    page_params: PageParams = Depends(pagination),
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    items, total = await project_service.list_projects(
        session,
        status=str(status) if status is not None else None,
        page=page_params.page,
        page_size=page_params.page_size,
    )
    return paged(
        [ProjectOut.model_validate(p) for p in items],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


@router.post(
    "",
    response_model=Envelope[ProjectOut],
    status_code=http_status.HTTP_201_CREATED,
    summary="创建项目",
    description=(
        "在当前租户下创建项目。\n\n"
        "`owner_id` 与 `dept_id` 由服务端从**当前身份**推导，不接受请求体传入——"
        "它们决定这行落在谁的数据范围内，允许客户端指定等于让数据权限失效。\n\n"
        "同一租户内项目编码唯一（唯一约束含 tenant_id，两个租户可以用同一个编码）。"
    ),
)
async def create_project(
    payload: ProjectCreateRequest,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    dept_id: int | None = Depends(get_current_dept_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    project = await project_service.create_project(
        session,
        code=payload.code,
        name=payload.name,
        description=payload.description,
        status=payload.status,
        owner_id=user_id,
        dept_id=dept_id,
    )
    return ok(ProjectOut.model_validate(project), message="已创建")


@router.get(
    "/{project_id}",
    response_model=Envelope[ProjectOut],
    summary="项目详情",
    description=(
        "按主键取项目。**跨租户或超出数据范围时返回 404**（不是 403）——"
        "返回 403 等于告知调用方「这个 ID 存在，只是你没权限」，造成存在性泄露。"
    ),
)
async def read_project(
    project_id: int,
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    project = await project_service.get_project(session, project_id)
    return ok(ProjectOut.model_validate(project))


@router.patch(
    "/{project_id}",
    response_model=Envelope[ProjectOut],
    summary="更新项目",
    description=(
        "局部更新：**只传要改的字段**。\n\n"
        "`code` 不可改——它是租户内的业务标识，改它会让外部引用失效；"
        "要改就新建项目并归档旧的。\n\n"
        '显式传 `"description": null` 可以清空字段（与「不传」区分开）。'
    ),
)
async def update_project(
    project_id: int,
    payload: ProjectUpdateRequest,
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    # exclude_unset：只取客户端显式传了的字段，从而区分「传 null」与「没传」
    changes = payload.model_dump(exclude_unset=True, mode="json")
    project = await project_service.update_project(session, project_id, changes=changes)
    return ok(ProjectOut.model_validate(project), message="已更新")
