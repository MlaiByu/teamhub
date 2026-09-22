"""通用 schema：分页参数与分页响应（PROJECT-PLAN 8.5）。

分页统一 `page` / `page_size`（默认 20，上限 200），返回
`items / total / page / page_size`。上限在这里约束一次，
repository 层再夹一次——两层都夹是故意的：schema 管「对外契约」，
repository 管「任何调用方都不能拖垮数据库」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200


class PageParams(BaseModel):
    """统一分页查询参数。"""

    page: int = Field(1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}"
    )


class PageData[T](BaseModel):
    """统一分页响应体。"""

    items: list[T]
    total: int
    page: int
    page_size: int
