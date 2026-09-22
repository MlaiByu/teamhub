"""refresh token 仓储。

★ 刷新令牌必须落库，否则登出后旧 token 仍然有效——这是最常见的认证漏洞
  （PROJECT-PLAN 7.3-3）。落库的用途有三个：

    1. **撤销**：登出 / 改密 / 管理员禁用账号时能立即失效
    2. **轮换**：每次刷新签新令牌并标记旧的，旧令牌立刻不可用
    3. **复用检测**：已撤销的令牌再次出现 → 判定泄露 → 撤销该用户全部令牌

★ 为什么这里没有「按 jti 无守卫查询」：
    刷新时虽然还没有请求上下文，但**refresh JWT 自身带 tenant_id**
    （见 core/security._encode）。流程是「先验签 → 用 payload 里的
    tenant_id 设上下文 → 再走受守卫查询」，所以不需要绕过守卫。
    这比「先无守卫查库、再从中读出 tenant_id」安全，因为后者在查询那一刻
    整个租户维度是敞开的。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update

from app.models import RefreshToken
from app.repositories.base import TenantAwareRepository


class RefreshTokenRepository(TenantAwareRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        """按 jti 查令牌（受守卫保护，调用前必须先设好租户上下文）。"""
        stmt = self.base_select().where(RefreshToken.jti == jti)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def revoke_chain(
        self,
        user_id: int,
        *,
        reason: str,
        replaced_by_jti: str | None = None,
    ) -> None:
        """撤销某用户在当前租户内的**全部** refresh token。

        ★ 为什么是「当前租户内」而不是「全平台」：
          被泄露的 refresh token 是租户作用域的——它只能换取该租户的
          access token，攻击面不会外溢到别的租户。所以撤销该租户内的
          整条链足以掐死泄露。跨租户全量撤销需要 bypass，属于安全事件
          升级处置，留给后续的审计 + 人工流程。

        ★ 用批量 UPDATE 而不是逐个 ORM 对象改：
          复用检测可能命中很多行，逐个加载再改会有 N 次查询。
        """
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked.is_(False),
                RefreshToken.is_deleted.is_(False),
            )
            .values(
                revoked=True,
                revoked_reason=reason,
                replaced_by_jti=replaced_by_jti,
                updated_at=datetime.now(UTC),
            )
        )
        # 不手写 tenant_id：租户过滤由 do_orm_execute 钩子注入
        await self.session.execute(stmt)
