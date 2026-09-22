"""组织（部门）服务：建部门 + 建树。

★ 物化路径（path）的两点用法（models/org.py 已说明 why）：
  - parent_id 便于挂接（`WHERE parent_id = ?`）
  - path 便于取子树（`WHERE path LIKE '/1/5/%'`），避免递归 CTE
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models import Department
from app.repositories.org import DepartmentRepository
from app.schemas.org import DepartmentNode

logger = get_logger(__name__)


def _build_path(parent: Department | None, dept_id: int) -> str:
    """按父部门拼物化路径。根部门是 `/id/`，子部门是 `parent.path + id/`。

    `parent.path` 本身以 `/` 结尾，所以直接拼接即可，不用额外分隔符。
    """
    return (parent.path if parent is not None else "/") + f"{dept_id}/"


async def create_department(
    session: AsyncSession,
    *,
    name: str,
    parent_id: int | None,
    leader_id: int | None,
) -> Department:
    """创建部门（要求已设租户上下文，由路由依赖保证）。"""
    repo = DepartmentRepository(session)

    parent: Department | None = None
    if parent_id is not None:
        parent = await repo.get(parent_id)
        # ★ get() 走守卫 + 钩子过滤，所以**别的租户的部门 ID 会返回 None**——
        #   「把部门挂到别的租户下」的跨租户挂载在这里被自然拦下，
        #   不需要额外写 ensure_same_tenant。返回 404 而不是 403，避免存在性泄露。
        if parent is None:
            raise NotFoundError("上级部门不存在或无权访问")

    # 同一上级下不允许重名（给用户一个明确的 409，而不是撞库时的抽象报错）
    if await repo.sibling_named(name=name, parent_id=parent_id) is not None:
        raise ConflictError("同级下已存在同名部门")

    dept = repo.create(name=name, parent_id=parent_id, leader_id=leader_id, path="")
    await session.flush()  # 拿 dept.id
    dept.path = _build_path(parent, dept.id)
    await session.flush()
    await session.commit()

    logger.info("department_created", dept_id=dept.id, parent_id=parent_id)
    return dept


async def get_department_tree(session: AsyncSession) -> list[DepartmentNode]:
    """返回当前租户的完整部门树。

    ★ 建树为什么不递归查库：
      path 按字符串排序后**父节点一定排在子节点之前**（`/1/` < `/1/5/`），
      所以一次查询 + 一遍遍历就能拼出整棵树，不用 N 次查询，也不用递归 CTE。
    """
    repo = DepartmentRepository(session)
    departments = await repo.list_ordered()

    nodes: dict[int, DepartmentNode] = {
        d.id: DepartmentNode(
            id=d.id,
            name=d.name,
            parent_id=d.parent_id,
            path=d.path,
            leader_id=d.leader_id,
        )
        for d in departments
    }

    roots: list[DepartmentNode] = []
    for d in departments:  # path 排序保证父在前
        node = nodes[d.id]
        if d.parent_id is None or d.parent_id not in nodes:
            # 根部门；或上级不在本租户可见范围内（理论不该发生，保守挂到根）
            roots.append(node)
        else:
            nodes[d.parent_id].children.append(node)
    return roots
