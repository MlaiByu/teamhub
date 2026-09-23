"""refresh token 的生命周期维护。

★ 为什么单独一个 service 而不是塞进 auth_service：
  auth_service 管的是「签发 / 校验 / 轮换」这条**认证主链路**；
  这里是「清理已经没用的历史数据」，属于运维性质，两者的变更原因不同
  （改认证逻辑 vs 改清理策略），放一起会让 auth_service 继续膨胀。

★ 为什么 tasks 不直接调 repository：
  PROJECT-PLAN 结构图 359 规定 `tasks/` 是「薄层，只调用 services」。
  这层 service 存在的意义就是让「业务规则（保留几天、什么条件该删）」
  待在 service 里，任务只负责「什么时候跑」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.repositories.refresh_token import RefreshTokenRepository

logger = get_logger(__name__)


async def purge_expired_refresh_tokens(
    session: AsyncSession, *, retention_days: int | None = None
) -> int:
    """物理删除已过期的 refresh token，返回删除行数。

    **要求调用前已设好租户上下文**——`RefreshToken` 是租户级表，
    钩子只删本租户的行。这是矩阵 9「Celery 任务携带 tenant_id，
    只处理本租户数据」能够成立的前提。

    ★ cutoff 用 **naive UTC**：`DateTime(timezone=True)` 在 SQLite 下不保留时区，
      存进去的是 naive 值。若这里传 aware datetime，SQLite 比较时两侧形态不一致
      （一处带偏移、一处不带），边界行会漏判。与 `TimestampMixin.updated_at`
      的处理保持同一约定。
    """
    days = settings.refresh_token_retention_days if retention_days is None else retention_days
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)

    removed = await RefreshTokenRepository(session).purge_expired(cutoff=cutoff)
    # 事务边界在 service 层（与项目其它 service 一致）：
    # DELETE 不提交就等于没删，而调用方（任务）不该关心事务细节。
    await session.commit()
    if removed:
        logger.info("refresh_tokens_purged", removed=removed, retention_days=days)
    return removed
