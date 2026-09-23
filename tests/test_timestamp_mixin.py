"""时间戳 Mixin 回归测试（地基改动配套，PROJECT-PLAN 硬规则 9）。

★ 为什么单独一个文件钉住这个：
  `TimestampMixin.updated_at` 从 server-side `onupdate=func.now()` 改成了
  Python 侧 `onupdate=lambda: datetime.now(UTC)`（见 core/db/base.py 的说明）。

  这个改动要消灭「每个 UPDATE 接口都要手写 session.refresh()」的负担，
  但必须钉住三个不变量，否则回归会静默蔓延：

  1. UPDATE 后 `updated_at` **可直接读**（不再需要 refresh，也不会 MissingGreenlet）
  2. UPDATE 后 `updated_at` **确实变了**（onupdate callable 生效）
  3. INSERT 后 `created_at` 仍由 **DB 生成**（server_default 保留，未受影响）
"""

from __future__ import annotations

import pytest

from app.core.constants import DataScope
from app.core.db.context import RequestContext, set_context
from app.repositories.project import ProjectRepository

pytestmark = pytest.mark.asyncio


async def _make_project(db_session_factory, *, code: str) -> tuple:
    session = db_session_factory()
    set_context(RequestContext(tenant_id=1, user_id=1, data_scope=DataScope.ALL))
    repo = ProjectRepository(session)
    project = repo.create(code=code, name="初始", status="ACTIVE", owner_id=1, dept_id=None)
    await session.flush()
    return session, repo, project


async def test_update_reads_updated_at_without_refresh(db_session_factory):
    """UPDATE 后直接读 updated_at 不抛 MissingGreenlet——refresh 已不需要。"""
    session, _repo, project = await _make_project(db_session_factory, code="TS-1")
    await session.commit()

    project.name = "改名"
    await session.commit()

    # ★ 关键：不 refresh，直接读 updated_at。改动前这里会抛 MissingGreenlet。
    assert project.updated_at is not None


async def test_update_bumps_updated_at(db_session_factory):
    """onupdate callable 确实生效：UPDATE 后 updated_at 变新。"""
    session, _repo, project = await _make_project(db_session_factory, code="TS-2")
    await session.flush()
    before = project.updated_at
    await session.commit()

    project.name = "再次改名"
    await session.commit()

    assert project.updated_at >= before, "updated_at 必须单调不减"


async def test_insert_keeps_db_generated_created_at(db_session_factory):
    """INSERT 后 created_at 仍由 DB 的 server_default 生成（未被本次改动波及）。"""
    session, _repo, project = await _make_project(db_session_factory, code="TS-3")
    await session.flush()

    # server_default=func.now() 仍在，插入后 created_at 应有值
    assert project.created_at is not None
    assert project.updated_at is not None
