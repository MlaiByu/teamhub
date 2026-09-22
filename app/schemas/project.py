"""项目领域 schema。

★ `owner_id` / `dept_id` **不出现在入参里**：它们决定这一行落在谁的
  数据范围（SELF / DEPT）内，必须由服务端从当前上下文推导。
  让客户端传这两个值，等于允许把数据「种」到别人的范围里——
  那样数据权限就形同虚设。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import ProjectStatus


class ProjectCreateRequest(BaseModel):
    """创建项目。`code` 在租户内唯一。"""

    code: str = Field(min_length=2, max_length=64, examples=["PROJ-1"])
    name: str = Field(min_length=1, max_length=200, examples=["协作平台重构"])
    description: str | None = Field(None, max_length=2000)
    status: ProjectStatus = Field(ProjectStatus.PLANNING)

    @property
    def is_default_status(self) -> bool:
        return self.status == ProjectStatus.PLANNING


class ProjectUpdateRequest(BaseModel):
    """局部更新。只传要改的字段，None 表示不改。

    ★ `code` 不可改：它是租户内的业务标识，改它会让外部引用（集成、
      报表、链接）失效。真要改就建新项目 + 归档旧的。
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    status: ProjectStatus | None = None


class ProjectOut(BaseModel):
    """项目完整信息。带出 owner_id / dept_id 便于前端展示归属。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    status: str
    owner_id: int
    dept_id: int | None
    created_at: datetime
    updated_at: datetime


class ProjectBrief(BaseModel):
    """项目摘要，用于嵌套在任务响应里。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
