"""认证服务：注册 / 登录 / 令牌轮换 / 登出撤销 / 复用检测。

★ 事务边界在 service 层（PROJECT-PLAN 6.1）。API 层不 commit，
  service 方法返回时数据已落库。`get_db` 只负责异常时 rollback。

★ 三条设计决策，都在这里说明白：

  1. **注册会一并创建个人租户，而不是只建用户。**
     `refresh_tokens` 是租户级表（`tenant_id NOT NULL`，受租户过滤保护）。
     如果注册只建用户，这个用户就处于「没有租户 → 无法签发 refresh token」
     的状态，登录走进死胡同——而让他加入租户需要邀请流（阶段 5+）。
     所以自助注册 = 「开一个团队」，这也是 Slack / 飞书这类产品的常见形态。
     平台管理员补开租户的路径（`POST /tenants`）仍然保留，两者共用
     `provision_default_roles`。

  2. **登录时「我属于哪些租户」必须无守卫查询。**
     这是多租户经典的引导查询：租户要从查询结果里得出，而守卫要求先有租户。
     详见 `repositories/tenant.py::list_memberships_for_user` 的安全论证。

  3. **刷新与登出自己设上下文，不依赖中间件。**
     这两个接口的请求里没有可用的 access token（一个已过期、一个是登出），
     所以中间件设不了上下文。但 **refresh JWT 自身带 tenant_id 且经过验签**，
     所以可以「先验签 → 用声明设上下文 → 再走受守卫查询」。
     注意顺序不能反：先设上下文，就不要在无上下文状态下查租户表。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    TENANT_ADMIN_ROLE,
    DataScope,
    MemberStatus,
    TenantStatus,
)
from app.core.db.context import (
    RequestContext,
    current_tenant_id,
    restore_context,
    set_context,
    snapshot_context,
)
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnauthenticatedError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import Tenant, TenantMember, User
from app.repositories.refresh_token import RefreshTokenRepository
from app.repositories.tenant import TenantMemberRepository, TenantRepository
from app.repositories.user import UserRepository
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)
from app.services.rbac_service import (
    UserAuthorities,
    bind_role,
    get_user_authorities,
    provision_default_roles,
)

logger = get_logger(__name__)


# ----------------------------------------------------------------------
# 返回结构
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RegisterResult:
    user: User
    tenant: Tenant
    token: TokenResponse


@dataclass(frozen=True)
class LoginResult:
    user: User
    tenant: Tenant
    token: TokenResponse


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
_timing_guard_hash: str | None = None


def _get_timing_guard_hash() -> str:
    """用户不存在时用来「等时化」的假哈希。

    ★ 为什么需要它：如果用户名不存在就直接返回 401，攻击者可以用响应时间
      区分「用户不存在」（快）和「密码错误」（慢，要跑一次 bcrypt），
      从而枚举出有效用户名。跑一次同成本的 verify 把两条路径拉平。

      用惰性初始化而不是模块级常量：bcrypt hash 要 ~100ms，
      放在模块加载期会拖慢每次进程启动（含每次 pytest 收集）。
    """
    global _timing_guard_hash
    if _timing_guard_hash is None:
        _timing_guard_hash = hash_password(secrets.token_urlsafe(16))
    return _timing_guard_hash


def _as_aware(dt: datetime) -> datetime:
    """把可能为 naive 的时间戳补上 UTC。

    ★ SQLite 不保存时区信息，`DateTime(timezone=True)` 读回来是 naive 的；
      PostgreSQL 读回来是 aware 的。直接比较两种会抛 TypeError，
      而且只在 SQLite（本地/测试）下才暴露——所以必须显式归一化。
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _slugify(username: str) -> str:
    """把用户名压成可读的租户编码前缀。只保留 ASCII 字母数字。

    中文用户名会被压成空串（`isascii()` 过滤掉），此时回退到 "team"——
    编码要进 URL 和日志，保持 ASCII 更省事，显示名走 `Tenant.name`。
    """
    chars = [ch.lower() if (ch.isascii() and ch.isalnum()) else "-" for ch in username]
    slug = "".join(chars).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:48] or "team"


async def _allocate_tenant_code(session: AsyncSession, base: str) -> str:
    """生成全局唯一的租户编码。

    `tenants.code` 是**全局唯一**（租户根表没有 tenant_id 维度）。
    加 6 位随机十六进制后缀，碰撞概率约 1/16.7M；仍显式检查并重试，
    因为「撞了就用重复值」会在 flush 时炸掉整个注册事务。
    """
    repo = TenantRepository(session)
    for _ in range(5):
        code = f"{base}-{secrets.token_hex(3)}"
        if await repo.get_by_code(code) is None:
            return code
    raise ConflictError("租户编码分配失败，请稍后重试")


async def _issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    tenant_id: int,
    dept_id: int | None,
    authorities: UserAuthorities,
) -> tuple[TokenResponse, str]:
    """签发 access + refresh，并把 refresh 的 jti 落库。

    要求调用前已设好该租户的上下文（RefreshToken 是租户级表）。
    返回 (令牌对, refresh 的 jti)——jti 用于轮换时写 `replaced_by_jti`。
    """
    access_token = create_access_token(
        user_id=user.id,
        tenant_id=tenant_id,
        dept_id=dept_id,
        data_scope=authorities.data_scope,
        roles=authorities.roles,
    )
    refresh_token, jti = create_refresh_token(user_id=user.id, tenant_id=tenant_id)

    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)
    RefreshTokenRepository(session).create(
        user_id=user.id,
        jti=jti,
        expires_at=expires_at,
        revoked=False,
    )
    await session.flush()

    return (
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            # noqa S106：与 schemas/auth.py 同源误判——"Bearer" 是 RFC 6750
            # 规定的 scheme 名，不是口令，必须逐字固定。
            token_type="Bearer",  # noqa: S106
            expires_in=settings.access_token_expire_minutes * 60,
        ),
        jti,
    )


def _payload_from_refresh_token(refresh_token: str) -> tuple[int, int, str]:
    """验签并取出 (user_id, tenant_id, jti)。

    ★ refresh JWT 的 tenant_id 是**签名保护**的，所以可以放心用来设上下文。
      这也是不需要「无守卫按 jti 查库」的原因——那条路会让查询瞬间
      对整个租户维度敞开。
    """
    try:
        payload = decode_token(refresh_token)
    except jwt.PyJWTError as exc:
        raise UnauthenticatedError("refresh token 无效或已过期") from exc

    if payload.get("type") != "refresh":
        raise UnauthenticatedError("令牌类型不正确")

    tenant_id = payload.get("tenant_id")
    if tenant_id is None:
        raise UnauthenticatedError("refresh token 缺少租户信息")
    if not payload.get("sub") or not payload.get("jti"):
        raise UnauthenticatedError("refresh token 结构不完整")

    return int(payload["sub"]), int(tenant_id), str(payload["jti"])


async def _load_active_membership(
    session: AsyncSession, *, user_id: int
) -> tuple[TenantMember, Tenant]:
    """取用户当前租户的成员关系与租户实体（要求已设上下文）。"""
    member = await TenantMemberRepository(session).get_membership(user_id)
    if member is None:
        raise NotFoundError("当前租户不存在或你已不是其成员")
    if member.status != str(MemberStatus.ACTIVE):
        raise UnauthenticatedError("你在该租户内的成员状态不可用")

    tenant = await TenantRepository(session).get(member.tenant_id)
    if tenant is None:
        raise NotFoundError("租户不存在")
    return member, tenant


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------
async def register(session: AsyncSession, *, payload: RegisterRequest) -> RegisterResult:
    """注册：建用户 + 建个人租户 + 绑管理员角色 + 签发令牌。

    全程一个事务：任何一步失败都不应该留下「有用户但没租户」的半成品。
    """
    user_repo = UserRepository(session)

    # 预检只为了给出友好报错；真正的唯一性保证是 users.username 的 UNIQUE 约束
    if await user_repo.username_taken(payload.username):
        raise ConflictError("用户名已被占用")

    user = user_repo.create(
        username=payload.username,
        password_hash=hash_password(payload.password),
        email=payload.email,
        is_active=True,
    )

    try:
        await session.flush()  # 拿 user.id
    except IntegrityError as exc:
        # 并发注册同名用户：预检都通过了，靠约束兜住
        await session.rollback()
        raise ConflictError("用户名已被占用") from exc

    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.create(
        code=await _allocate_tenant_code(session, _slugify(payload.username)),
        name=payload.tenant_name or f"{payload.username} 的团队",
        status=str(TenantStatus.ACTIVE),
        max_members=50,
    )
    await session.flush()  # 拿 tenant.id

    # 从这里开始要写租户级表（TenantMember / Role / UserRole / RefreshToken），
    # 必须先设上下文。数据范围给 SELF——引导阶段只需要租户维度，
    # 给宽范围没有任何好处。
    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant.id, user_id=user.id, data_scope=DataScope.SELF))
    try:
        TenantMemberRepository(session).create(
            user_id=user.id,
            status=str(MemberStatus.ACTIVE),
            dept_id=None,
        )
        await session.flush()

        role_ids = await provision_default_roles(session, tenant_id=tenant.id)
        await bind_role(session, user_id=user.id, role_id=role_ids[TENANT_ADMIN_ROLE])

        authorities = await get_user_authorities(session, user.id)
        token, _ = await _issue_token_pair(
            session,
            user=user,
            tenant_id=tenant.id,
            dept_id=None,
            authorities=authorities,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("注册失败：资源冲突，请重试") from exc
    finally:
        restore_context(previous)

    logger.info("user_registered", user_id=user.id, tenant_id=tenant.id)
    return RegisterResult(user=user, tenant=tenant, token=token)


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
async def login(session: AsyncSession, *, payload: LoginRequest) -> LoginResult:
    """登录：校验口令 → 定位租户 → 算授权 → 签发令牌。"""
    user = await UserRepository(session).get_by_username(payload.username)

    if user is None:
        # 等时化：跑一次同成本的 bcrypt，避免用响应时间枚举有效用户名
        verify_password(payload.password, _get_timing_guard_hash())
        raise UnauthenticatedError("用户名或密码不正确")

    if not verify_password(payload.password, user.password_hash):
        raise UnauthenticatedError("用户名或密码不正确")

    if not user.is_active:
        raise UnauthenticatedError("账号已被禁用")

    # ★ 无守卫查询：此刻还没有租户上下文，见模块 docstring 决策 2
    memberships = await TenantMemberRepository(session).list_memberships_for_user(user.id)
    active = [m for m in memberships if m.status == str(MemberStatus.ACTIVE)]
    if not active:
        raise ConflictError("该账号尚未加入任何团队，请先创建或加入一个团队")

    if payload.tenant_id is not None:
        chosen = next((m for m in active if m.tenant_id == payload.tenant_id), None)
        if chosen is None:
            # 不区分「租户不存在」与「你不是成员」——避免存在性泄露
            raise UnauthenticatedError("租户不存在或你无权访问")
    elif len(active) == 1:
        chosen = active[0]
    else:
        raise ConflictError(f"你属于 {len(active)} 个团队，请在 tenant_id 中指定要登录哪一个")

    previous = snapshot_context()
    set_context(
        RequestContext(
            tenant_id=chosen.tenant_id,
            user_id=user.id,
            dept_id=chosen.dept_id,
            data_scope=DataScope.SELF,
        )
    )
    try:
        tenant = await TenantRepository(session).get(chosen.tenant_id)
        if tenant is None:
            raise NotFoundError("租户不存在")
        if tenant.status != str(TenantStatus.ACTIVE):
            raise UnauthenticatedError(f"租户当前状态为 {tenant.status}，无法登录")

        authorities = await get_user_authorities(session, user.id)
        token, _ = await _issue_token_pair(
            session,
            user=user,
            tenant_id=chosen.tenant_id,
            dept_id=chosen.dept_id,
            authorities=authorities,
        )
        await session.commit()
    finally:
        restore_context(previous)

    logger.info("user_logged_in", user_id=user.id, tenant_id=chosen.tenant_id)
    return LoginResult(user=user, tenant=tenant, token=token)


# ----------------------------------------------------------------------
# 刷新（轮换 + 复用检测）
# ----------------------------------------------------------------------
async def refresh(session: AsyncSession, *, refresh_token: str) -> TokenResponse:
    """用 refresh 换新令牌对，并让旧的立即失效（rotate on use）。

    ★ 复用检测：已撤销的 refresh token 再次出现，只有两种可能——
      令牌被窃取，或者客户端并发刷新。两种都按泄露处理：
      撤销该用户在当前租户内的**整条**令牌链，让持有者重新登录。
      宁可误伤一次并发刷新，也不放过一次真实泄露。
    """
    user_id, tenant_id, jti = _payload_from_refresh_token(refresh_token)

    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.SELF))
    try:
        repo = RefreshTokenRepository(session)
        row = await repo.get_by_jti(jti)
        if row is None:
            raise UnauthenticatedError("refresh token 已失效，请重新登录")

        if row.revoked:
            # ★ 只有 ROTATED 才是「复用」。
            #
            #   令牌被撤销有三种原因，语义完全不同：
            #     ROTATED   已被轮换、且**签发过后继令牌** → 它再次出现说明
            #               有人拿着作废的旧票来换新票：要么被窃取，要么客户端
            #               并发刷新。两种都按泄露处理，撤销整条链。
            #     LOGOUT    用户主动登出 → 就是一张废票，普通 401 即可。
            #     EXPIRED   过期 → 同上。
            #
            #   把 LOGOUT/EXPIRED 也当成复用，会让每次「登出后再试一次」
            #   都触发全量撤销和一条 warning 日志——告警疲劳会让真正的
            #   泄露事件淹没在噪音里。
            if row.revoked_reason == "ROTATED":
                await repo.revoke_chain(user_id, reason="REUSE_DETECTED")
                # ★ 必须 commit：后面要 raise，异常会让 get_db 触发 rollback，
                #   撤销动作就白做了。安全响应必须落库才能生效。
                await session.commit()
                logger.warning(
                    "refresh_token_reuse_detected",
                    user_id=user_id,
                    tenant_id=tenant_id,
                    jti=jti,
                    replaced_by=row.replaced_by_jti,
                )
                raise UnauthenticatedError("检测到令牌复用，已撤销该账号全部会话，请重新登录")

            raise UnauthenticatedError("refresh token 已失效，请重新登录")

        if _as_aware(row.expires_at) <= datetime.now(UTC):
            row.revoked = True
            row.revoked_reason = "EXPIRED"
            await session.commit()
            raise UnauthenticatedError("refresh token 已过期，请重新登录")

        user = await UserRepository(session).get(user_id)
        if user is None or not user.is_active:
            raise UnauthenticatedError("账号不可用")

        member, _tenant = await _load_active_membership(session, user_id=user_id)

        authorities = await get_user_authorities(session, user_id)
        token, new_jti = await _issue_token_pair(
            session,
            user=user,
            tenant_id=tenant_id,
            dept_id=member.dept_id,
            authorities=authorities,
        )

        # 旧令牌立即失效，并记录被谁替换（用于定位泄露链）
        row.revoked = True
        row.revoked_reason = "ROTATED"
        row.replaced_by_jti = new_jti

        await session.commit()
    finally:
        restore_context(previous)

    logger.info("refresh_token_rotated", user_id=user_id, tenant_id=tenant_id)
    return token


# ----------------------------------------------------------------------
# 登出
# ----------------------------------------------------------------------
async def logout(session: AsyncSession, *, refresh_token: str) -> None:
    """登出：撤销该用户在当前租户内的整条令牌链。

    ★ 为什么撤销整条链而不是单个令牌：
      登出的语义是「结束这次会话」。用户可能同时开着多个标签页，
      每个都持有从同一条链轮换出来的令牌——只撤销当前那个，
      其余标签页仍然有效，用户会觉得「登出了但还登录着」。

    ★ 幂等：令牌已经无效时也返回成功。登出接口报错会让前端陷入
      「想登出但登不出去」的尴尬状态。
    """
    try:
        user_id, tenant_id, _jti = _payload_from_refresh_token(refresh_token)
    except UnauthenticatedError:
        logger.info("logout_with_invalid_token")
        return

    previous = snapshot_context()
    set_context(RequestContext(tenant_id=tenant_id, user_id=user_id, data_scope=DataScope.SELF))
    try:
        await RefreshTokenRepository(session).revoke_chain(user_id, reason="LOGOUT")
        await session.commit()
    finally:
        restore_context(previous)

    logger.info("user_logged_out", user_id=user_id, tenant_id=tenant_id)


# ----------------------------------------------------------------------
# 查询辅助
# ----------------------------------------------------------------------
async def load_user(session: AsyncSession, user_id: int) -> User:
    user = await UserRepository(session).get(user_id)
    if user is None:
        raise UnauthenticatedError("账号不存在")
    return user


async def load_current_tenant(session: AsyncSession) -> Tenant:
    """取当前上下文对应的租户（要求已设上下文）。"""
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise UnauthenticatedError("当前请求没有租户上下文")
    tenant = await TenantRepository(session).get(tenant_id)
    if tenant is None:
        raise NotFoundError("租户不存在")
    return tenant


__all__ = [
    "RegisterResult",
    "LoginResult",
    "register",
    "login",
    "refresh",
    "logout",
    "load_user",
    "load_current_tenant",
]
