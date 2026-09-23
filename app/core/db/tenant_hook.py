"""多租户 + 数据权限的查询层统一注入（本项目灵魂）。

机制：SQLAlchemy 的 Session 事件 `do_orm_execute` + `with_loader_criteria`，
在查询执行前自动追加过滤条件，业务代码零感知（PROJECT-PLAN 4.2 / 5.3）。

两层在同一个钩子里一次性注入，形成 AND 关系：
    WHERE tenant_id = 1
    WHERE tenant_id = 1 AND dept_id  = 5
    WHERE tenant_id = 1 AND owner_id = 42

★ 四个已知边界（4.3），实现里逐条对应：
    坑 1 未设上下文 = 全量泄露  → 钩子不过滤是「机制」；由 repository 入口拒绝兜底
    坑 2 插入不补 tenant_id     → 由 repository.create() 统一写入 + NOT NULL 兜底
    坑 3 relationship 不在过滤内 → 业务统一走实体查询，不深层遍历
    坑 4 Session.get() 走缓存    → 隔离测试必须用全新会话（见 tests/conftest.py）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.constants import DataScope
from app.core.db import context as ctx
from app.core.db.mixins import DataScopedMixin, TenantScopedMixin

logger = logging.getLogger(__name__)

# 单次查询里每张表的过滤条件累加器。
_CRITERIA_KEY = "_teamhub_loader_criteria"


def _build_criteria_map() -> dict[type, Callable[[type], Any]]:
    """按当前上下文，为两个 Mixin 各自生成过滤条件。

    返回 {Mixin 类: 接收实体类、返回 SQL 条件的可调用对象}。

    ★ 为什么必须是 callable 而不是直接算出 Column：

    Mixin 上的字段是用 `declared_attr` 声明的，只有被某个实体类继承时
    才生成真正的 Column 并绑定名字。在 Mixin 类本身上访问 `.tenant_id`
    得到的是一个**未绑定的占位对象**，交给 SQL 编译器会直接抛
    `Cannot compile Column object until its 'name' is assigned`。

    所以这里只捕获「值」，把列引用推迟到 SQLAlchemy 把实体类交回来时再求值。
    with_loader_criteria 支持传 callable，正是为这种场景设计的。
    """
    criteria: dict[type, Callable[[type], Any]] = {}

    if ctx.is_bypass():
        # 显式 bypass（平台运营路径）。审计由 repository 层强制写入（4.5）。
        return criteria

    tenant_id = ctx.current_tenant_id.get()
    user_id = ctx.current_user_id.get()
    dept_id = ctx.current_dept_id.get()

    # ★ 总开关：完全空上下文 ⇒ 什么都不注入。
    #
    #   语义必须精确定义成「无上下文 = 不过滤」，不能是「无上下文 = 用默认值过滤」。
    #   原因有二：
    #   1. 这是风险面 1 的定义：未设上下文就会查到全量。把这条行为显式钉住，
    #      测试才能写反向验证（清空上下文 → 应看到全部）。
    #      如果这里退化成「默认 SELF、owner_id = NULL」，反向验证永远返回 0 条，
    #      「隔离开关真的在起作用」这条最重要的验收标准就没法证明。
    #   2. Alembic 迁移、管理脚本这类无租户语境的场景，本来就该看到全量数据。
    #
    #   真正的防线不在这里，而在 repositories/base.py 的 require_tenant_context()——
    #   业务查询一律先过守卫，过不了就 raise，不给「静默全量」的机会。
    if tenant_id is None and user_id is None and dept_id is None:
        return criteria

    if tenant_id is not None:
        # ★ 这些字段由 Mixin 的 declared_attr 在实体类上生成；静态类型里 `cls`
        #   是裸 `type`、没有 `.tenant_id`。运行时完全合法（SQLAlchemy 会把实体类
        #   交进来求值），所以对 mypy 的 attr-defined 报错精确 ignore。
        #   不能用 getattr(cls, "tenant_id") 替代——ruff B009 会把它「修」回属性访问。
        criteria[TenantScopedMixin] = lambda cls: cls.tenant_id == tenant_id  # type: ignore[attr-defined]

    # 数据范围只在有租户上下文时才生效——跨租户的裸查不叠数据范围。
    if tenant_id is not None:
        scope = ctx.current_data_scope.get()
        if scope == DataScope.SELF:
            # 有租户但没用户 ⇒ 无法判定「自己」，此时必须拒绝而不是放行，
            # 否则 SELF 会退化成 ALL。用恒假条件表达「一条都别给」。
            criteria[DataScopedMixin] = (
                (lambda cls: cls.owner_id == user_id)  # type: ignore[attr-defined]
                if user_id is not None
                else (lambda cls: False)
            )
        elif scope == DataScope.DEPT:
            criteria[DataScopedMixin] = (
                (lambda cls: cls.dept_id == dept_id)  # type: ignore[attr-defined]
                if dept_id is not None
                else (lambda cls: False)
            )
        # ALL：不加额外条件，但仍受上层租户过滤约束。

    return criteria


def _apply(stmt, criteria: dict[type, Callable[[type], Any]]):
    for mixin_cls, build_condition in criteria.items():
        stmt = stmt.options(
            with_loader_criteria(
                mixin_cls,
                build_condition,  # callable → 延迟求值，不提前碰未绑定的列
                include_aliases=True,
            )
        )
    return stmt


@event.listens_for(Session, "do_orm_execute")
def _inject_tenant_and_data_scope(state: ORMExecuteState) -> None:
    """查询执行前的统一拦截点。"""
    if not state.is_select or state.is_column_load:
        return
    # 坑 3：关系加载不注入，否则会破坏 join 与懒加载。
    # 代价是通过 relationship 一路点过去的对象不受保护——业务不依赖它。
    if state.is_relationship_load:
        return

    criteria = _build_criteria_map()
    if not criteria:
        return

    state.statement = _apply(state.statement, criteria)
