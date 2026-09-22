"""唯一数据入口（架构规则 1）。

★ 本文件承担三个不可替代的职责：

1. **全局表 vs 租户表的分流**
   按设计不带 `tenant_id` 的表（`User` / `Tenant` / `Permission`）不在租户过滤
   范围内，用 `BaseRepository`；租户级表必须用 `TenantAwareRepository`，
   它在查询起点强制校验上下文。

2. **上下文强制校验**（坑 1 的兜底）
   钩子的逻辑是「有 tenant_id 才过滤」，所以未设上下文就会查到全量。
   靠人记得设上下文是记不住的——在唯一入口处直接 raise，把「忘了」变成
   「启动即报错」。

3. **显式 bypass**
   平台运营、Alembic 数据迁移等需要跨租户的场景，走 bypass_* 方法，
   而不是「忘了设上下文」。所有 bypass 调用强制落审计（4.5）。

★ 为什么必须有 BaseRepository（而不是全都走 TenantAwareRepository）：

   `User` 是全局表，**登录时还没有租户上下文**——中间件要靠 token 才能设上下文，
   而 token 正是登录要签发的东西，存在循环依赖。所以「按用户名查用户」这条
   查询必须在无租户上下文下可用。给它套租户守卫会让登录永远失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from sqlalchemy import CursorResult, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.base import Base
from app.core.db.context import (
    RequestContext,
    current_tenant_id,
    current_user_id,
    is_bypass,
    restore_context,
    set_context,
    snapshot_context,
)
from app.core.exceptions import NotFoundError, TenantContextMissingError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 需要预加载的关系集中在这里声明，避免懒加载抛 MissingGreenlet（风险 5）。
# 形如：{Project: ("owner", "department")}
EAGER_LOADS: dict[type, tuple[str, ...]] = {}


class BaseRepository[ModelT: Base]:
    """无租户守卫的仓储基类。

    ★ 只给「全局表」用：`User` / `Tenant` / `Permission`。
      这三张表按设计不带 `tenant_id`（见 PROJECT-PLAN 7.3），
      本来就不在租户过滤范围内，套租户守卫是概念错位。

    ★ 租户级表**必须**继承 `TenantAwareRepository`，不要直接继承这个。
      判断标准：模型是否继承 `TenantScopedMixin`。
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _apply_eager_loads(self, stmt: Select) -> Select:
        """统一预加载策略。集中在 repository，避免各 service 各写一遍。"""
        from sqlalchemy.orm import selectinload

        for relation in EAGER_LOADS.get(self.model, ()):
            stmt = stmt.options(selectinload(getattr(self.model, relation)))
        return stmt

    def base_select(self) -> Select:
        """查询起点。

        注意：这里**不手写** `where(tenant_id=...)`——过滤由 do_orm_execute
        钩子自动完成。手写会和钩子重复，且会在 bypass 时冲突。
        软删除在此显式排除。
        """
        return self.raw_select()

    def raw_select(self) -> Select:
        """不经任何守卫的查询起点。

        ★ 只给两处用：
          1. BaseRepository 内部实现
          2. 隔离测试的反向验证——必须先「清空上下文」看全量，
             才能证明钩子确实在工作，而不是碰巧没数据

        ★ 业务代码禁止在租户表上直接调用。判断标准很简单：
          调用点附近如果找不到 require_tenant_context() 或 bypass_context()，
          那就是漏用。这类调用应在 review 中被拦下。
        """
        stmt = select(self.model)
        # getattr 而非直接属性访问：is_deleted 声明在 TimestampMixin 上，
        # 静态类型看不到，但运行期存在。返回 Any，交给 where() 接受。
        is_deleted_col = getattr(self.model, "is_deleted", None)
        if is_deleted_col is not None:
            stmt = stmt.where(is_deleted_col.is_(False))
        return self._apply_eager_loads(stmt)

    async def raw_list_all(self) -> Sequence[ModelT]:
        """raw_select 的执行版，仅供隔离测试的反向验证使用。"""
        result = await self.session.execute(self.raw_select())
        return result.scalars().unique().all()

    async def list_all(self) -> Sequence[ModelT]:
        result = await self.session.execute(self.base_select())
        return result.scalars().unique().all()

    async def paginate(
        self, *, page: int = 1, page_size: int = 20, stmt: Select | None = None
    ) -> tuple[Sequence[ModelT], int]:
        """统一分页（8.5）：返回 (items, total)。上限 200 由 schema 层约束。"""
        stmt = stmt if stmt is not None else self.base_select()
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        result = await self.session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
        return result.scalars().unique().all(), int(total)

    async def get(self, entity_id: int) -> ModelT | None:
        """按主键查询。

        ★ 不用 Session.get()：它命中 identity map 时不发 SQL，会让隔离测试
        测到缓存而非过滤逻辑（坑 4）。这里强制走 select，保证每次都被过滤。
        """
        stmt = self.base_select().where(self.model.id == entity_id)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def get_or_404(self, entity_id: int) -> ModelT:
        """跨租户访问返回 404 而非 403——避免存在性泄露（PLAN 十二·第3条）。"""
        obj = await self.get(entity_id)
        if obj is None:
            # 消息里不带实体真实名称，避免通过报错措辞推断存在性
            raise NotFoundError("资源不存在或无权访问")
        return obj

    async def find_one_by(self, **filters: Any) -> ModelT | None:
        """按等值条件查单条。全局表用得最多（如按 username 查用户）。"""
        stmt = self.base_select()
        for field, value in filters.items():
            stmt = stmt.where(getattr(self.model, field) == value)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create(self, **values: Any) -> ModelT:
        """新建实体。不注入 tenant_id——全局表没有这个维度。"""
        if hasattr(self.model, "created_by") and not values.get("created_by"):
            values["created_by"] = current_user_id.get()

        obj = self.model(**values)
        self.session.add(obj)
        return obj

    async def soft_delete(self, entity_id: int) -> None:
        obj = await self.get_or_404(entity_id)
        if not hasattr(obj, "is_deleted"):
            raise TypeError(f"{self.model.__name__} 不支持软删除")
        obj.is_deleted = True  # type: ignore[attr-defined]


class TenantAwareRepository[ModelT: Base](BaseRepository[ModelT]):
    """租户感知仓储基类。**所有租户级业务 repository 继承它。**

    与 BaseRepository 的差别只有两处，但这两处是隔离的地基：
        - `base_select()` 先过上下文守卫，再查
        - `create()` 从上下文注入 `tenant_id`，并拒绝越权写入
    """

    # ------------------------------------------------------------------
    # 上下文守卫
    # ------------------------------------------------------------------
    @staticmethod
    def require_tenant_context() -> int:
        """取当前租户 ID，未设置则直接拒绝。

        这个方法存在的意义：把「漏设上下文 → 静默全量泄露」变成
        「抛错 + 立刻可见」。绝不允许在业务查询路径上绕过它。
        """
        if is_bypass():
            return 0  # bypass 模式下 tenant_id 不参与过滤，由调用方显式指定
        tenant_id = current_tenant_id.get()
        if tenant_id is None:
            raise TenantContextMissingError(
                f"{__name__}: 租户上下文未设置，拒绝执行查询。"
                "如需跨租户访问，请显式使用 bypass 方法（会写审计日志）。"
            )
        return tenant_id

    def base_select(self) -> Select:
        """带上下文校验的查询起点（覆盖基类的无守卫版本）。"""
        self.require_tenant_context()
        return self.raw_select()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def create(self, **values: Any) -> ModelT:
        """新建实体。

        坑 2：INSERT 不经过钩子，tenant_id 必须在此显式写入。
        同时校验调用方传的 tenant_id 与上下文一致，防止越权写入别人租户。
        """
        # bypass 下上下文没有 tenant_id（守卫返回 0）。此时**不能**把 0 写进去——
        # 那会违反外键并静默写坏数据。要求调用方显式指定。
        if is_bypass():
            if hasattr(self.model, "tenant_id") and "tenant_id" not in values:
                raise TenantContextMissingError(
                    "bypass 模式下写入租户表必须显式传 tenant_id，"
                    "不能依赖上下文注入（bypass 上下文里没有租户身份）。"
                )
            return super().create(**values)

        tenant_id = self.require_tenant_context()

        if "tenant_id" in values and values["tenant_id"] != tenant_id:
            raise TenantContextMissingError("拒绝写入：入参 tenant_id 与当前上下文不一致")
        if hasattr(self.model, "tenant_id"):
            values["tenant_id"] = tenant_id

        return super().create(**values)

    # ------------------------------------------------------------------
    # 显式 bypass（平台级路径）
    # ------------------------------------------------------------------
    def bypass_select(self, *, audit_action: str) -> Select:
        """跨租户查询的唯一合法入口。

        ★ 所有调用点在 code review 中需重点确认。审计由 audit_service 承担，
        此处强制要求传入 audit_action，避免「悄悄绕过」。
        """
        logger.warning(
            "cross_tenant_query",
            model=self.model.__name__,
            audit_action=audit_action,
            user_id=current_user_id.get(),
        )
        return self.raw_select()

    async def bypass_update(self, entity_id: int, *, audit_action: str, **values: Any) -> int:
        """跨租户更新。返回受影响行数。"""
        logger.warning(
            "cross_tenant_update",
            model=self.model.__name__,
            entity_id=entity_id,
            audit_action=audit_action,
        )
        stmt = (
            update(self.model)
            .where(self.model.id == entity_id)  # type: ignore[attr-defined]
            .values(**values)
        )
        # session.execute(update(...)) 运行期返回 CursorResult（带 rowcount），
        # 但静态类型标注是 Result[Any]——此处 cast 反映运行期事实。
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)


@asynccontextmanager
async def bypass_context(*, tenant_id: int | None = None, user_id: int | None = None):
    """在受控作用域内打开 bypass。

    用于管理脚本 / Alembic 数据迁移。**不要**在请求处理路径里用。

    ★ 必须保存并恢复原上下文，不能直接清空：

        第一版实现退出时硬编码 `set_context(RequestContext(tenant_id=None))`。
        结果是**嵌套调用会静默抹掉外层租户上下文**——退出后隔离就没了，
        而且没有任何日志。任何「bypass 里再调用带上下文的代码」都会中招。
        改成快照 + 恢复，语义变成「借用一下，用完还回去」。
    """
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, bypass=True))
    try:
        yield
    finally:
        restore_context(previous)
