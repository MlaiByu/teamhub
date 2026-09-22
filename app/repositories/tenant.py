"""租户仓储。

`Tenant` 是隔离的根（不带 tenant_id）→ `BaseRepository`
`TenantMember` 是租户级表（带 tenant_id）→ `TenantAwareRepository`

★ 本文件里有一个必须解释清楚的设计取舍：`list_memberships_for_user`。
  它是**受控的例外**——跨租户查询，但只返回「调用者自己的成员关系」。
  详见该方法上的注释。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import Tenant, TenantMember
from app.repositories.base import BaseRepository, TenantAwareRepository


class TenantRepository(BaseRepository[Tenant]):
    """租户根表。平台级操作（开通租户）走这里。"""

    model = Tenant

    async def get_by_code(self, code: str) -> Tenant | None:
        return await self.find_one_by(code=code)


class TenantMemberRepository(TenantAwareRepository[TenantMember]):
    """用户-租户成员关系。"""

    model = TenantMember

    async def list_memberships_for_user(self, user_id: int) -> Sequence[TenantMember]:
        """列出某个用户加入的全部租户关系。**跨租户查询，是有意的例外。**

        ★ 为什么它必须绕过租户守卫：

          登录时还不知道用户属于哪个租户（租户要从查询结果里得出），
          而守卫要求先有租户上下文——存在循环依赖。这是多租户系统
          经典的「引导查询」问题。

        ★ 为什么绕过它是安全的：

          1. 过滤键是 `user_id`，且该值来自**已验证的凭据**
             （登录时是用户名+密码校验通过的用户，刷新时是签名校验通过的 token）。
             调用方只能枚举**自己**的成员关系，拿不到别人的。
          2. 返回的行只有 `(tenant_id, user_id, status, dept_id)`，
             不含任何租户业务数据。最坏情况是知道自己属于哪些租户——
             这正是本方法要实现的功能。
          3. 方法名显式带 `for_user`，且只有认证服务会调用它。
             code review 时按敏感路径对待。

        ★ 与 `bypass_*` 的区别：bypass 打开的是「整个作用域的过滤全关」，
          用于平台运营跨租户操作。这里只需要一条按键取值的自查询，
          用 bypass 属于杀鸡用牛刀，还会让调用点失去 `audit_action` 的约束力。
        """
        stmt = self.raw_select().where(TenantMember.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().unique().all()

    async def get_membership(self, user_id: int) -> TenantMember | None:
        """取当前租户内某用户的成员关系。

        这个方法**是受守卫保护的**（走 base_select）——因为它已经在
        「某个具体租户」的语境下，用不到跨租户能力。
        """
        stmt = self.base_select().where(TenantMember.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def count_members(self) -> int:
        """当前租户的成员数（走守卫）。用于 max_members 配额校验。"""
        from sqlalchemy import func, select

        stmt = select(func.count()).select_from(self.model).where(self.model.is_deleted.is_(False))
        # 不过手写 tenant_id：租户过滤由钩子注入
        return int((await self.session.execute(stmt)).scalar_one())
