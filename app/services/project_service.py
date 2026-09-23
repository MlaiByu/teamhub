"""项目服务。

★ 本模块最重要的一条纪律：**`owner_id` / `dept_id` 绝不接受客户端传入。**
  这两个字段决定该行落在谁的数据范围内（SELF 按 owner_id 比、DEPT 按 dept_id 比）。
  如果允许请求体指定它们，任何成员都能把项目「种」到别人的范围里，
  或者把自己的数据伪装成别的部门的——数据权限就完全失效了。
  所以它们只从「当前用户身份 + 当前上下文」推导。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ProjectStatus
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.models import Project
from app.repositories.project import ProjectRepository
from app.services import audit_service

logger = get_logger(__name__)


async def create_project(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str | None,
    status: ProjectStatus,
    owner_id: int,
    dept_id: int | None,
) -> Project:
    """创建项目（要求已设租户上下文）。"""
    repo = ProjectRepository(session)

    # 预检给友好 409；真正的保证是 UNIQUE(tenant_id, code) 约束
    if await repo.get_by_code(code) is not None:
        raise ConflictError("项目编码已存在")

    project = repo.create(
        code=code,
        name=name,
        description=description,
        status=str(status),
        owner_id=owner_id,
        dept_id=dept_id,
    )
    await session.flush()
    await session.commit()

    logger.info("project_created", project_id=project.id, code=code)
    await audit_service.record(
        session,
        action="project.create",
        entity_type="project",
        entity_id=project.id,
        detail={"code": code, "name": name},
    )
    return project


async def list_projects(
    session: AsyncSession,
    *,
    status: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Project], int]:
    """分页列出项目。租户过滤与数据范围过滤都由钩子注入，这里不手写。"""
    items, total = await ProjectRepository(session).paginate_filtered(
        status=status, page=page, page_size=page_size
    )
    return list(items), total


async def get_project(session: AsyncSession, project_id: int) -> Project:
    """按主键取项目。跨租户或超出数据范围时返回 404（不是 403）。"""
    return await ProjectRepository(session).get_or_404(project_id)


async def update_project(session: AsyncSession, project_id: int, *, changes: dict) -> Project:
    """局部更新项目。

    `changes` 由路由用 `model_dump(exclude_unset=True, mode="json")` 产出，
    因此它**只含客户端显式传了的字段**——这样「传 null 想清空描述」与
    「压根没传描述」才能区分开。用 `if field is not None` 的写法会把
    前者当成后者，导致字段永远清不掉。

    `code` 不在可改字段里（见 schemas/project.py 的说明）。
    """
    project = await ProjectRepository(session).get_or_404(project_id)

    for field, value in changes.items():
        setattr(project, field, value)

    await session.commit()

    # （无 refresh：updated_at 已改为 Python 侧 onupdate，SQLAlchemy 知道新值，
    #   UPDATE 后不会标记过期，也就不再需要 refresh 来避免 MissingGreenlet。
    #   见 core/db/base.py::TimestampMixin 的说明。）

    logger.info("project_updated", project_id=project_id, fields=sorted(changes))
    await audit_service.record(
        session,
        action="project.update",
        entity_type="project",
        entity_id=project_id,
        detail={"fields": sorted(changes)},
    )
    return project
