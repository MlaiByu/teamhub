"""RBAC schema。

`Permission` 是平台维护的**全局字典**（租户只能组合不能新增，7.3-2），
所以这里只有「租户可创建」的 Role 相关 schema，没有 Permission 的创建接口。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import DataScope


class RoleCreateRequest(BaseModel):
    """创建角色。`data_scope` 决定这个角色的成员能看到多大范围的数据。"""

    code: str = Field(min_length=2, max_length=64, examples=["PROJECT_LEAD"])
    name: str = Field(min_length=1, max_length=128, examples=["项目组长"])
    data_scope: DataScope = Field(description="数据范围：SELF / DEPT / ALL")

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, v: str) -> str:
        """角色编码统一大写、只含字母数字下划线。

        ★ 编码是程序里的引用键（出现在 token 声明、require_roles 的比对里），
          必须是一个稳定的、无歧义的标识符——大小写混用或含空格会让
          「数据库里的编码」和「代码里写的编码」对不上，而且这种不一致
          只在运行期才暴露。
        """
        v = v.strip().upper()
        if not v.replace("_", "").isalnum() or not v[0].isalpha():
            raise ValueError("角色编码只能含字母、数字、下划线，且以字母开头")
        return v


class RoleOut(BaseModel):
    """角色信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    data_scope: str
    created_at: datetime


class PermissionOut(BaseModel):
    """权限点信息（全局字典，只读）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    module: str
