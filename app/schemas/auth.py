"""认证领域 schema。

★ 为什么不用 `EmailStr`：
    它要求 `email-validator` 依赖，而 `pyproject.toml` 没有——为了一个可选字段
    引入新依赖不划算。这里只做长度与形态的轻校验，真实性验证交给后续的
    邮箱验证流程（阶段 2 的异步任务）。

★ 密码为什么只校验长度、不校验复杂度：
    复杂度规则（必须含大小写+数字+符号）在实践中会把用户推向
    `Password1!` 这类可预测口令，收益为负。真正该做的是长度下界 +
    撞库检测 + 限流，前两项已在此处和阶段 3 的限流里覆盖。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.schemas.tenant import TenantBrief
from app.schemas.user import UserOut

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 64


class RegisterRequest(BaseModel):
    """注册。

    ★ 注册会一并创建该用户的**个人租户**并把其绑为管理员。
      理由见 `services/auth_service.register` 的说明——简言之：
      `refresh_tokens.tenant_id` 是 NOT NULL 且受租户过滤保护，
      没有租户就没有可签发的 refresh token，登录会走进死胡同。
    """

    username: str = Field(
        min_length=MIN_USERNAME_LENGTH, max_length=MAX_USERNAME_LENGTH, examples=["guotao"]
    )
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        examples=["a-long-enough-passphrase"],
    )
    email: str | None = Field(None, max_length=255, examples=["me@example.com"])
    tenant_name: str | None = Field(
        None,
        max_length=128,
        description="个人租户名称；不传则用「{username} 的团队」",
    )

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, v: str) -> str:
        """去首尾空白并禁止内部空白。

        ★ 不限制为 ASCII：中文用户名是合理需求，硬卡 `[a-zA-Z0-9_]`
          会把「张三」这类正常输入拒掉，收益为负。
        """
        v = v.strip()
        if len(v) < MIN_USERNAME_LENGTH:
            raise ValueError(f"用户名至少 {MIN_USERNAME_LENGTH} 个字符")
        if any(ch.isspace() for ch in v):
            raise ValueError("用户名不能包含空白字符")
        return v

    @field_validator("password")
    @classmethod
    def _reject_trivial_password(cls, v: str) -> str:
        """拒绝「全数字」「全相同字符」这类一眼可猜的口令。"""
        if v.isdigit():
            raise ValueError("密码不能是纯数字")
        if len(set(v)) == 1:
            raise ValueError("密码不能是单一字符的重复")
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v and "@" not in v:
            raise ValueError("邮箱格式不正确")
        return v or None


class LoginRequest(BaseModel):
    """登录。

    `tenant_id` 只在用户属于**多个**租户时才必填：
    单一租户自动选中；多租户必须显式指定，避免「登录到哪个组织」靠猜。
    """

    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    tenant_id: int | None = Field(None, description="用户属多个租户时必填")


class RefreshRequest(BaseModel):
    """用 refresh token 换取新的 access + refresh（轮换）。"""

    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    """登出。撤销该 refresh token 所属的整条令牌链。"""

    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """令牌对。`expires_in` 是 access token 的剩余秒数，便于前端提前刷新。"""

    access_token: str
    refresh_token: str
    # noqa S105：S105 按字段名里的 "token" 判定「疑似硬编码口令」。
    # 这里的值必须逐字是 "Bearer"（RFC 6750 的 scheme 名），无法也不该外部化。
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int = Field(description="access token 有效期（秒）")


class AuthUser(BaseModel):
    """登录态下返回给前端的用户身份摘要，含当前租户与授权信息。

    `data_scope` / `roles` 是从 token 声明里读回来的——不是再查一次库。
    这样「前端看到的权限」与「后端实际执行的过滤」必然同源，
    不会出现前端以为能看、后端查不到的错位。
    """

    user: UserOut
    tenant: TenantBrief
    data_scope: str
    roles: list[str]


class RegisterResponse(BaseModel):
    """注册结果：用户 + 个人租户 + 直接可用的令牌对。"""

    user: UserOut
    tenant: TenantBrief
    token: TokenResponse


class SwitchTenantResponse(BaseModel):
    """切换租户的结果：目标租户 + 新的（目标租户作用域的）令牌对。

    ★ 返回新令牌而不是复用旧令牌：access token 是**租户作用域**的，
      切换租户等于换身份，必须用一张新 token 表达。
    """

    tenant: TenantBrief
    token: TokenResponse
