"""分页依赖（PROJECT-PLAN 8.5）。

★ 分页参数走依赖而不是让每个路由自己声明 `Query(...)`：
  上限（200）只在一处定义，新增接口不会因为抄漏而放开上限。
  真实的防护在 repository 的 `paginate()` 里还会再夹一次——
  两层是故意的，依赖层管「对外契约」，repository 管「任何调用方都拖不垮数据库」。
"""

from __future__ import annotations

from fastapi import Query

from app.schemas.common import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageParams


async def pagination(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(
        DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}"
    ),
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


def build_page_meta(total: int, page: int, page_size: int) -> dict:
    """按 total/page_size 算出总页数（向上取整）。

    单独抽出来是为了让「pages 怎么算」只有一处实现——
    前端分页组件的边界 bug（总数刚好整除时多出一页）多半来自各接口各算一遍。
    """
    pages = (total + page_size - 1) // page_size if page_size else 0
    return {"total": total, "page": page, "page_size": page_size, "pages": pages}
