"""组织（部门）schema。

部门树用**嵌套结构**返回（PROJECT-PLAN 8.4「部门树」）。
`path` 是物化路径（`/1/5/12/`），`parent_id` 便于客户端挂接——两者都给，
是因为「建树」与「按子树过滤」是两种不同的使用方式。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DepartmentCreateRequest(BaseModel):
    """创建部门。`parent_id` 为 None 表示根部门。"""

    name: str = Field(min_length=1, max_length=128, examples=["研发部"])
    parent_id: int | None = Field(None, description="上级部门 ID；不传表示根部门")
    leader_id: int | None = Field(None, description="部门负责人（用户 ID）")

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


class DepartmentOut(BaseModel):
    """单个部门（平铺，不带子节点）。用于创建结果与调试。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    parent_id: int | None
    path: str
    leader_id: int | None


class DepartmentNode(BaseModel):
    """部门树节点（嵌套）。

    ★ 服务端直接返回树而不是平铺列表：「部门树」就是这个接口的用途，
      让每个客户端各自实现一遍建树是重复劳动，而且各家实现容易不一致
      （比如排序规则、孤儿节点的处理）。一次返回一棵确定的树。
    """

    id: int
    name: str
    parent_id: int | None
    path: str
    leader_id: int | None
    children: list[DepartmentNode] = Field(default_factory=list)


# 自引用模型需要显式重建，让 Pydantic 解析 children: list[DepartmentNode]
DepartmentNode.model_rebuild()
