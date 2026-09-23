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

from datetime import datetime

from app.models import RefreshToken
from app.repositories.base import TenantAwareRepository


class RefreshTokenRepository(TenantAwareRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_jti(self, jti: str) -> RefreshToken | None:
        """按 jti 查令牌（受守卫保护，调用前必须先设好租户上下文）。"""
        stmt = self.base_select().where(RefreshToken.jti == jti)
        result = await self.session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def purge_expired(self, *, cutoff: datetime) -> int:
        """**物理删除** `expires_at < cutoff` 的令牌，返回删除行数。

        ★ 为什么是物理删除而不是软删除（`is_deleted=True`）：
          定期清理的目的是**释放空间**。软删除只是把行标记一下，表照样膨胀，
          查询还要多带一个 `is_deleted = false` 条件——达不到目的。

        ★ 为什么条件只看 `expires_at`，不看 `revoked`：
          已撤销但**未过期**的行必须保留——复用检测靠它。
          （见 `auth_service.refresh`：命中 `revoked_reason == "ROTATED"` 才判定
          「令牌被复用」并撤销该用户整条链。若把已撤销的行提前删掉，
          攻击者拿旧令牌只会得到「令牌不存在」，复用告警就永远不会触发。）

          而过期的行没有安全价值：JWT 验签阶段就会因 `exp` 失败，
          根本走不到复用检测，所以删掉它是安全的。

        ★ `cutoff` 由调用方给（通常是 `now - retention_days`）：
          留一段缓冲，避免因各节点时钟偏差把「刚过期」的行删掉后，
          某个仍在飞行中的请求又来查它。

        ★ 走 `bulk_delete()` 而不是裸 `delete()`：钩子**不作用于**批量写
          （实测：不带租户条件的 DELETE 会删掉所有租户的行）。
          租户条件由封装强制带上，见 base.py 的说明。
        """
        return await self.bulk_delete(RefreshToken.expires_at < cutoff)

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
        # ★ 走 bulk_update() 而不是裸 update()：钩子不作用于批量写。
        #   修复前这里会把该 user_id 在**所有租户**的令牌一起撤销
        #   （用户在一个租户登出，把他在别家公司的会话也踢掉了）。
        #   updated_at 交给列上的 onupdate 自动维护，不再手写。
        await self.bulk_update(
            {
                "revoked": True,
                "revoked_reason": reason,
                "replaced_by_jti": replaced_by_jti,
            },
            RefreshToken.user_id == user_id,
            RefreshToken.revoked.is_(False),
            RefreshToken.is_deleted.is_(False),
        )
