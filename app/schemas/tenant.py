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


class MemberOut(BaseModel):
    """成员 + 用户信息（member 列表返回）。

    ★ 不直接用 `TenantMemberOut`：管理界面要展示的是**用户名与邮箱**，
      而它们存在全局 `users` 表。所以这里手工组装两表的字段，
      而不是把 ORM join 结果原样吐出去——「哪些字段能出去」在一处显式可见。
    """

    id: int
    user_id: int
    username: str
    email: str | None = None
    status: str
    dept_id: int | None = None
    created_at: datetime


class MemberAddRequest(BaseModel):
    """把**已注册**的用户加入当前租户。

    ★ 按 username 而不是 user_id：管理员记的是用户名，不是数据库主键。
      「邀请未注册的人」属于邀请流（阶段 5 的异步任务），这里只处理
      「把一个已有账号拉进团队」。
    """

    username: str = Field(min_length=1, max_length=64, examples=["guotao"])
    dept_id: int | None = Field(None, description="加入后归属的部门")


class AssignRoleRequest(BaseModel):
    """给成员绑定角色。"""

    role_id: int = Field(gt=0, description="要绑定的角色 ID")
