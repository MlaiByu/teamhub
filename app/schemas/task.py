"""任务领域 schema。

★ `owner_id` / `dept_id` 同样不出现在入参里（理由同 project schema）。

★ 关于「负责人」的约定：任务的 `owner_id` 取**被分配人**（没分配时取创建者），
  `dept_id` 取被分配人所在部门。这样 SELF 数据范围的语义是
  「分配给我的任务」而不是「我创建的任务」——后者会让被管理员分配任务的
  普通成员看不到自己的任务，是明显的可用性事故。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import TaskPriority, TaskStatus


class TaskCreateRequest(BaseModel):
    """创建并（可选）分配任务。"""

    project_id: int = Field(gt=0, description="所属项目；必须是本租户的项目")
    title: str = Field(min_length=1, max_length=200, examples=["实现租户切换接口"])
    description: str | None = Field(None, max_length=4000)
    assignee_id: int | None = Field(None, description="执行人 user_id；必须是本团队的 ACTIVE 成员")
    status: TaskStatus = Field(TaskStatus.TODO)
    priority: TaskPriority = Field(TaskPriority.MEDIUM)
    due_at: datetime | None = None


class TaskUpdateRequest(BaseModel):
    """局部更新任务。状态流转走独立接口 `PATCH /tasks/{id}/status`。

    ★ 为什么不在这里改 `status`：状态受**流转规则**约束
      （见 core/constants.TASK_STATUS_TRANSITIONS），必须校验合法性。
      把它混进通用 PATCH 会让「改优先级」和「推进状态」共享一条路径，
      审计日志里就分不清哪次改动是状态推进，也无法对状态流转单独加权限。
    """

    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)
    assignee_id: int | None = Field(None, description="改派执行人（必须是本团队成员）")
    priority: TaskPriority | None = None
    due_at: datetime | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> TaskUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("至少要指定一个要修改的字段")
        return self


class TaskStatusUpdateRequest(BaseModel):
    """状态流转。"""

    status: TaskStatus = Field(description="目标状态；必须满足流转规则")


class TaskOut(BaseModel):
    """任务完整信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    description: str | None
    assignee_id: int | None
    status: str
    priority: str
    due_at: datetime | None
    owner_id: int
    dept_id: int | None
    created_at: datetime
    updated_at: datetime
