"""用户领域 schema。

`User` 是**全局表**（同一账号可加入多个租户），所以这里的 schema 不带
`tenant_id`——租户维度体现在 `TenantMember` 与 token 声明上。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    """对外暴露的用户信息。

    ★ 绝不包含 `password_hash`——用独立的出参 schema 而不是把 ORM 对象
      直接序列化，就是为了让「哪些字段能出去」在一处显式可见。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None = None
    is_active: bool
    created_at: datetime


class UserBrief(BaseModel):
    """只含展示所需最小集，用于嵌套在其它响应里（成员列表、任务负责人等）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str


class ChangePasswordRequest(BaseModel):
    """改密。按 8.2，改密后必须撤销该用户全部 refresh token。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
