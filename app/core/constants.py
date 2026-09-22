"""全局枚举与常量。放在 core 层，任何层都可引用。"""

from __future__ import annotations

from enum import StrEnum


class DataScope(StrEnum):
    """数据范围三级（PROJECT-PLAN 5.2）。绑在 Role 上，多角色取最宽。"""

    SELF = "SELF"
    DEPT = "DEPT"
    ALL = "ALL"

    @property
    def width(self) -> int:
        """用于「多角色取最宽」的比较。数值越大范围越宽。"""
        return {"SELF": 1, "DEPT": 2, "ALL": 3}[self.value]


def widest_scope(scopes) -> DataScope:
    """多角色取最宽数据范围。

    收敛到一处集中计算——散在各个 service 里算，早晚会有一处算错
    （PROJECT-PLAN 十四「如果重做」）。
    """
    return max(scopes, key=lambda s: s.width, default=DataScope.SELF)


class TenantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TRIAL = "TRIAL"


class MemberStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVITED = "INVITED"
    DISABLED = "DISABLED"


class ProjectStatus(StrEnum):
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DONE = "DONE"
    ARCHIVED = "ARCHIVED"


class TaskStatus(StrEnum):
    """固定枚举流转，不做可配置流程图（1.4 边界）。"""

    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


# 任务状态合法流转表。非法跳转由 service 拒绝（PLAN 10.3）。
TASK_STATUS_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.TODO: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.REVIEW, TaskStatus.TODO, TaskStatus.CANCELLED},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.IN_PROGRESS},
    TaskStatus.DONE: set(),  # 终态
    TaskStatus.CANCELLED: set(),  # 终态
}


class BizCode:
    """业务码 → HTTP 状态码映射（PROJECT-PLAN 8.1）。"""

    OK = 0
    PARAM_INVALID = 40000
    UNAUTHENTICATED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    RATE_LIMITED = 42900
    INTERNAL_ERROR = 50000

    HTTP_MAP: dict[int, int] = {
        OK: 200,
        PARAM_INVALID: 422,
        UNAUTHENTICATED: 401,
        FORBIDDEN: 403,
        NOT_FOUND: 404,
        CONFLICT: 409,
        RATE_LIMITED: 429,
        INTERNAL_ERROR: 500,
    }

    @classmethod
    def http_status(cls, code: int) -> int:
        return cls.HTTP_MAP.get(code, 500)


PLATFORM_ADMIN_ROLE = "PLATFORM_ADMIN"
TENANT_ADMIN_ROLE = "TENANT_ADMIN"
DEPT_MANAGER_ROLE = "DEPT_MANAGER"
MEMBER_ROLE = "MEMBER"
