"""任务领域 service。

★ 本模块有两个「钩子覆盖不到」的跨租户校验，必须显式写：

  1. **`project_id`** —— 走守卫查询（`ProjectRepository.get`），别的租户的
     项目 ID 自然返回 None。这一条是「免费」的。

  2. **`assignee_id`** —— **必须显式校验**。`assignee_id` 指向全局 `users` 表，
     而 `User` 不带 `tenant_id`、不在租户过滤范围内，钩子对它无能为力；
     数据库外键也只保证「users 里存在这个 id」。不校验的话，A 公司的任务
     可以分配给 B 公司的人，对方甚至会在自己的待办里看到它。
     这是本项目里唯一需要人工兜底的一类引用——因为它是**跨租户边界的
     唯一「全局表 → 租户表」引用**。

  另有 `ensure_same_tenant`：留给「对象不是通过守卫查询拿到」的场景
  （例如调用方自己持有对象、或走了 bypass）。当前 API 路径用不到它，
  但它是矩阵 8 的落地点，保留并持续被测试覆盖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    TASK_STATUS_TRANSITIONS,
    MemberStatus,
    TaskPriority,
    TaskStatus,
)
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Task, TenantMember
from app.realtime.events import EventType
from app.repositories.project import ProjectRepository
from app.repositories.task import TaskRepository
from app.repositories.tenant import TenantMemberRepository
from app.services import audit_service, notification_service

logger = get_logger(__name__)


class _TenantScoped(Protocol):
    tenant_id: int


def ensure_same_tenant(obj: _TenantScoped | None, *, expected_tenant_id: int) -> None:
    """校验关联对象与当前租户一致。

    ★ 为什么必须有这个函数：
        task.project_id 的外键只保证「projects 里存在这个 id」，
        **不保证这个 project 属于同一租户**。数据库层完全合法，
        但结果是把 A 租户的任务挂到了 B 租户的项目上——
        多租户里最隐蔽的一类数据污染。

    跨租户时抛 NotFound 语义（不是 403），避免存在性泄露。
    """
    from app.core.exceptions import TenantContextMissingError

    if obj is None:
        raise NotFoundError("关联资源不存在或无权访问")
    if obj.tenant_id != expected_tenant_id:
        logger.warning(
            "cross_tenant_mount_attempt",
            object_tenant=getattr(obj, "tenant_id", None),
            expected_tenant=expected_tenant_id,
        )
        # 注意：这里抛 TenantContextMissingError 而非 NotFoundError，
        # 因为它是**编程错误/恶意构造**，需要开发期立刻可见。
        # 面向外部 API 时由 api 层转成 404。
        raise TenantContextMissingError(
            f"拒接跨租户挂载：对象属于租户 {getattr(obj, 'tenant_id', None)}，"
            f"当前租户 {expected_tenant_id}"
        )


def validate_status_transition(current: str, target: str) -> None:
    """校验任务状态流转合法性（10.3：非法跳转被拒绝）。

    状态是固定枚举流转，不做可配置流程图（1.4 边界）。
    """
    try:
        cur, tgt = TaskStatus(current), TaskStatus(target)
    except ValueError as exc:
        raise ConflictError(f"非法的状态值：{exc}") from exc

    if cur == tgt:
        return
    allowed = TASK_STATUS_TRANSITIONS.get(cur, set())
    if tgt not in allowed:
        raise ConflictError(
            f"不允许从 {cur} 流转到 {tgt}；可选：{[s.value for s in allowed] or '无（终态）'}"
        )


async def ensure_assignee_is_member(session: AsyncSession, user_id: int) -> TenantMember:
    """校验被分配人是**当前租户**的 ACTIVE 成员。返回其成员关系。

    ★ 为什么必须显式做（钩子覆盖不到）：
      `assignee_id` 指向全局 `users` 表——`User` 不带 tenant_id，
      不在租户过滤范围内；外键也只保证「users 里存在这个 id」。
      不校验就能把任务分配给别的公司的人。

    返回 404 而不是 403：不泄露「这个 user_id 存在，只是不在你团队」。
    """
    member = await TenantMemberRepository(session).get_membership(user_id)
    if member is None or member.status != str(MemberStatus.ACTIVE):
        logger.warning("assignee_not_in_tenant", user_id=user_id)
        raise NotFoundError("被分配人不存在或不是本团队的成员")
    return member


def _resolve_owner(
    *, creator_id: int, creator_dept_id: int | None, assignee_member: TenantMember | None
) -> tuple[int, int | None]:
    """推导任务的 owner_id / dept_id（二者都描述「负责人」）。

    ★ owner 取**被分配人**而不是创建者：SELF 数据范围的语义因此是
      「分配给我的任务」。若取创建者，被管理员分配任务的普通成员
      会在自己的任务列表里看不到它——明显的可用性事故。

      没人被分配时，任务还没有负责人，退回创建者。
    """
    if assignee_member is not None:
        return assignee_member.user_id, assignee_member.dept_id
    return creator_id, creator_dept_id


async def create_task(
    session: AsyncSession,
    *,
    project_id: int,
    title: str,
    description: str | None,
    assignee_id: int | None,
    status: TaskStatus,
    priority: TaskPriority,
    due_at: datetime | None,
    creator_id: int,
    creator_dept_id: int | None,
) -> Task:
    """创建任务（要求已设租户上下文）。"""
    # 项目走守卫查询：跨租户 / 超出数据范围都返回 None → 404。
    # 保留对象（而不是只判空）：通知 payload 需要项目名。
    project = await ProjectRepository(session).get(project_id)
    if project is None:
        raise NotFoundError("项目不存在或无权访问")

    assignee_member: TenantMember | None = None
    if assignee_id is not None:
        assignee_member = await ensure_assignee_is_member(session, assignee_id)

    owner_id, dept_id = _resolve_owner(
        creator_id=creator_id, creator_dept_id=creator_dept_id, assignee_member=assignee_member
    )

    task = TaskRepository(session).create(
        project_id=project_id,
        title=title,
        description=description,
        assignee_id=assignee_id,
        status=str(status),
        priority=str(priority),
        due_at=due_at,
        owner_id=owner_id,
        dept_id=dept_id,
    )
    await session.flush()

    # 先把通知要用的值取出来：emit 会 commit，且它内部可能 rollback
    new_task_id = task.id
    new_task_title = task.title
    new_project_name = project.name

    await session.commit()

    logger.info("task_created", task_id=new_task_id, project_id=project_id, assignee_id=assignee_id)

    await audit_service.record(
        session,
        action="task.create",
        entity_type="task",
        entity_id=new_task_id,
        detail={"project_id": project_id, "assignee_id": assignee_id, "title": new_task_title},
    )

    # ★ 事件必须在业务 commit 之后发（见 notification_service 的时序约定）。
    #   排除「自己分配给自己」——那不该产生通知。
    if assignee_id is not None and assignee_id != creator_id:
        await notification_service.emit(
            session,
            event_type=EventType.TASK_ASSIGNED,
            target_user_id=assignee_id,
            actor_id=creator_id,
            task_id=new_task_id,
            task_title=new_task_title,
            project_id=project_id,
            project_name=new_project_name,
            assigned_by=creator_id,
            reassigned=False,
        )

    return task


async def list_tasks(
    session: AsyncSession,
    *,
    project_id: int | None,
    assignee_id: int | None,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Task], int]:
    """分页列出任务。租户过滤与数据范围过滤由钩子注入。"""
    items, total = await TaskRepository(session).paginate_filtered(
        project_id=project_id,
        assignee_id=assignee_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return list(items), total


async def get_task(session: AsyncSession, task_id: int) -> Task:
    """按主键取任务。跨租户或超出数据范围时 404。"""
    return await TaskRepository(session).get_or_404(task_id)


async def update_task(
    session: AsyncSession, task_id: int, *, changes: dict, actor_id: int | None = None
) -> Task:
    """局部更新任务。

    `changes` 由路由用 `model_dump(exclude_unset=True, mode="json")` 产出。
    若其中含 `assignee_id`，需要重新校验成员关系并重算 owner_id / dept_id——
    否则改派之后任务的数据归属还挂在原负责人名下，列表可见性会跟着错。
    """
    task = await TaskRepository(session).get_or_404(task_id)

    # 记下改派前的执行人：只有「真的换人了」才发通知。
    # 不加这个比较的话，把 assignee_id 设成同一个值也会触发一次改派通知。
    previous_assignee = task.assignee_id
    project_id = task.project_id

    if "assignee_id" in changes:
        new_assignee = changes["assignee_id"]
        if new_assignee is not None:
            member = await ensure_assignee_is_member(session, new_assignee)
            # 有新负责人 → 归属跟着负责人走
            task.owner_id, task.dept_id = member.user_id, member.dept_id
        else:
            # 取消分配 → 回到创建者名下。dept_id 保持不动：
            # 我们没存创建者当时的部门，猜一个值反而更不可预测。
            task.owner_id = task.created_by or task.owner_id

    for field, value in changes.items():
        setattr(task, field, value)

    task_title = task.title
    current_assignee = task.assignee_id

    await session.commit()

    logger.info("task_updated", task_id=task_id, fields=sorted(changes))

    await audit_service.record(
        session,
        action="task.update",
        entity_type="task",
        entity_id=task_id,
        detail={"fields": sorted(changes)},
    )

    # 改派事件：新执行人存在、确实换了人、且不是操作者本人给自己派活
    if (
        current_assignee is not None
        and current_assignee != previous_assignee
        and current_assignee != actor_id
    ):
        project = await ProjectRepository(session).get(project_id)
        await notification_service.emit(
            session,
            event_type=EventType.TASK_ASSIGNED,
            target_user_id=current_assignee,
            actor_id=actor_id,
            task_id=task_id,
            task_title=task_title,
            project_id=project_id,
            project_name=project.name if project is not None else "",
            assigned_by=actor_id,
            reassigned=True,
        )

    return task


async def change_status(
    session: AsyncSession, task_id: int, *, target: TaskStatus, actor_id: int | None = None
) -> Task:
    """推进任务状态（受 TASK_STATUS_TRANSITIONS 约束）。

    ★ 状态流转单独成一个方法/接口，而不是塞进通用 `update_task`：
      - 流转规则要集中校验，混在通用 PATCH 里会被「顺手也支持改 status」绕过
      - 审计上要能区分「改了优先级」与「推进了状态」——后者通常是
        需要单独通知与统计的业务事件（阶段 3 会在这里挂通知）
      - 将来若要对「谁能推进状态」单独设权限，独立的接口才好加

    `validate_status_transition` 允许「目标 == 当前」的幂等调用（直接返回），
    并拒绝所有未在流转表里的跳转。
    """
    task = await TaskRepository(session).get_or_404(task_id)

    validate_status_transition(task.status, str(target))

    if task.status == str(target):
        # 幂等：状态没变就不提交、也不发通知——没有变化就没有事件
        return task

    from_status = task.status
    assignee_id = task.assignee_id
    task_title = task.title

    task.status = str(target)
    await session.commit()
    logger.info("task_status_changed", task_id=task_id, status=str(target))

    await audit_service.record(
        session,
        action="task.status_changed",
        entity_type="task",
        entity_id=task_id,
        detail={"from": from_status, "to": str(target)},
    )

    # 通知执行人，但**排除操作者本人**——自己推进自己的任务不该收到通知
    if assignee_id is not None and assignee_id != actor_id:
        await notification_service.emit(
            session,
            event_type=EventType.TASK_STATUS_CHANGED,
            target_user_id=assignee_id,
            actor_id=actor_id,
            task_id=task_id,
            task_title=task_title,
            from_status=from_status,
            to_status=str(target),
            changed_by=actor_id,
        )

    return task
