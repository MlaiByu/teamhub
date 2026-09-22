"""统一响应信封（PROJECT-PLAN 8.1）。

    { "code": 0, "message": "ok", "data": {...}, "request_id": "..." }

用泛型 `Envelope` 包装，让路由可以写
`response_model=Envelope[RegisterResponse]`——这样 `/docs` 里显示的是
真实结构，而不是一个笼统的 object。

★ 成功响应也要带 `request_id`：排查问题时最常做的是「拿响应里的 id 去日志里搜」，
  只给错误响应带 id 会导致「成功但结果不对」这类问题无从查起。
  失败响应的 id 由 `core/exceptions.py` 的 `_envelope()` 补上。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.logging import request_id_var


class Envelope[T](BaseModel):
    code: int = 0
    message: str = "ok"
    data: T | None = None
    request_id: str | None = None


class PageMeta(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int = Field(0, description="总页数，由 total/page_size 向上取整")


class PageData[T](BaseModel):
    items: list[T]
    total: int
    page: int
    page_size: int


def ok[T](data: T | None = None, message: str = "ok") -> dict:
    """构造成功信封。路由配 `response_model=Envelope[X]` 使用。"""
    return {
        "code": 0,
        "message": message,
        "data": data,
        "request_id": request_id_var.get(),
    }


def paged(items: list, total: int, page: int, page_size: int) -> dict:
    """构造分页成功信封，结构对齐 `PageData`（8.5）。"""
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        "request_id": request_id_var.get(),
    }
