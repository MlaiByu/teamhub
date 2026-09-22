"""任务路由（PROJECT-PLAN 8.4）。

    GET   /tasks?project_id=&assignee_id=&status=&page=   任务列表
    POST  /tasks                                          创建并分配任务
    GET   /tasks/{id}                                     任务详情
    PATCH /tasks/{id}                                     局部更新（改派 / 优先级 / 截止）

状态流转走独立接口 `PATCH /tasks/{id}/status`（下一步实现），
理由见 schemas/task.py 里对 TaskUpdateRequest 的说明。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_dept_id, get_current_tenant_id, get_current_user_id
from app.api.deps.pagination import pagination
from app.core.constants import TaskStatus
from app.core.db.session import get_db
from app.core.responses import Envelope, PageData, ok, paged
from app.schemas.common import PageParams
from app.schemas.task import (
    TaskCreateRequest,
    TaskOut,
    TaskStatusUpdateRequest,
    TaskUpdateRequest,
)
from app.services import task_service

router = APIRouter(prefix="/tasks", tags=["任务"])


@router.get(
    "",
    response_model=Envelope[PageData[TaskOut]],
    summary="任务列表",
    description=(
        "分页列出当前租户内、且落在**当前用户数据范围**内的任务。\n\n"
        "过滤维度：`project_id`（某项目的任务）、`assignee_id`（某人的任务）、"
        "`status`。三者可组合。\n\n"
        "配合 `assignee_id=自己` 就是「我的任务」——"
        "注意 SELF 数据范围的语义正是「分配给我的」（见 schemas/task.py）。"
    ),
)
async def list_tasks(
    project_id: int | None = Query(None, description="按项目过滤"),
    assignee_id: int | None = Query(None, description="按执行人过滤"),
    status: TaskStatus | None = Query(None, description="按状态过滤"),
    page_params: PageParams = Depends(pagination),
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    items, total = await task_service.list_tasks(
        session,
        project_id=project_id,
        assignee_id=assignee_id,
        status=str(status) if status is not None else None,
        page=page_params.page,
        page_size=page_params.page_size,
    )
    return paged(
        [TaskOut.model_validate(t) for t in items],
        total=total,
        page=page_params.page,
        page_size=page_params.page_size,
    )


@router.post(
    "",
    response_model=Envelope[TaskOut],
    status_code=http_status.HTTP_201_CREATED,
    summary="创建任务",
    description=(
        "在指定项目下创建任务，可选直接分配执行人。\n\n"
        "**两类跨租户校验**（这是本接口最该注意的地方）：\n"
        "- `project_id`：走守卫查询，别的租户的项目 ID 返回 404\n"
        "- `assignee_id`：指向**全局** `users` 表，租户钩子覆盖不到，"
        "必须显式校验其为当前团队的 ACTIVE 成员，否则能把任务分配给别的公司的人\n\n"
        "任务的 `owner_id` 取**被分配人**（未分配时取创建者），"
        "因此 SELF 范围的语义是「分配给我的任务」。"
    ),
)
async def create_task(
    payload: TaskCreateRequest,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    dept_id: int | None = Depends(get_current_dept_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    task = await task_service.create_task(
        session,
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        assignee_id=payload.assignee_id,
        status=payload.status,
        priority=payload.priority,
        due_at=payload.due_at,
        creator_id=user_id,
        creator_dept_id=dept_id,
    )
    return ok(TaskOut.model_validate(task), message="已创建")


@router.get(
    "/{task_id}",
    response_model=Envelope[TaskOut],
    summary="任务详情",
    description="跨租户或超出数据范围时返回 404（不是 403），避免存在性泄露。",
)
async def read_task(
    task_id: int,
    session: AsyncSession = Depends(get_db),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    task = await task_service.get_task(session, task_id)
    return ok(TaskOut.model_validate(task))


@router.patch(
    "/{task_id}",
    response_model=Envelope[TaskOut],
    summary="更新任务",
    description=(
        "局部更新：标题 / 描述 / 执行人 / 优先级 / 截止时间。\n\n"
        "**改派会自动重算数据归属**：任务的 `owner_id` 跟着新执行人走，"
        "否则改派后任务在列表里的可见性还挂在原负责人名下。\n\n"
        "状态不在这里改——走 `PATCH /tasks/{id}/status` 以受流转规则约束。"
    ),
)
async def update_task(
    task_id: int,
    payload: TaskUpdateRequest,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    changes = payload.model_dump(exclude_unset=True, mode="json")
    task = await task_service.update_task(session, task_id, changes=changes, actor_id=user_id)
    return ok(TaskOut.model_validate(task), message="已更新")


@router.patch(
    "/{task_id}/status",
    response_model=Envelope[TaskOut],
    summary="推进任务状态",
    description=(
        "按固定流转规则推进状态：\n\n"
        "```\n"
        "TODO ──▶ IN_PROGRESS ──▶ REVIEW ──▶ DONE（终态）\n"
        "  │            │            │\n"
        "  └────────────┴────────────┴──▶ CANCELLED（终态）\n"
        "```\n"
        "完整规则见 `core/constants.TASK_STATUS_TRANSITIONS`。"
        "非法跳转返回 409（业务冲突），不是 422——请求格式没问题，是状态不允许。\n\n"
        "**幂等**：目标状态与当前相同时直接返回，不报错。\n\n"
        "为什么单独一个接口而不是塞进 `PATCH /tasks/{id}`：流转规则要集中校验，"
        "混在通用 PATCH 里容易被「顺手支持改 status」绕过；"
        "而且审计上要能区分「改了优先级」与「推进了状态」——"
        "后者是需要单独通知与统计的业务事件。"
    ),
)
async def change_task_status(
    task_id: int,
    payload: TaskStatusUpdateRequest,
    session: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
    _: int = Depends(get_current_tenant_id),
) -> dict:
    task = await task_service.change_status(
        session, task_id, target=payload.status, actor_id=user_id
    )
    return ok(TaskOut.model_validate(task), message="状态已更新")
