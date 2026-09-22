"""租户领域 schema。

`Tenant` 本身是**隔离的根**，不带 `tenant_id`（PROJECT-PLAN 7.3-1），
所以它对应的仓储用 `BaseRepository`；`TenantMember` 是租户级表，
用 `TenantAwareRepository`。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TenantOut(BaseModel):
    """租户完整信息（`/tenants/current` 等接口返回）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    status: str
    max_members: int
    created_at: datetime


class TenantBrief(BaseModel):
    """租户摘要，用于嵌套在 token / 登录响应里。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str


class TenantCreateRequest(BaseModel):
    """平台管理员开通租户（PROJECT-PLAN 8.4）。"""

    code: str = Field(min_length=2, max_length=64, examples=["acme"])
    name: str = Field(min_length=1, max_length=128, examples=["Acme 科技"])
    max_members: int = Field(50, ge=1, le=100_000)


class TenantMemberOut(BaseModel):
    """成员关系。`dept_id` 是 DEPT 数据范围的判定依据。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    status: str
    dept_id: int | None = None
    created_at: datetime
