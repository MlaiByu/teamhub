"""全局配置。所有可变项从环境变量读取（pydantic-settings）。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 本地开发占位密钥。生产必须由 JWT_SECRET 环境变量覆盖，见 _guard_production_secrets。
# noqa S105：S105 按变量名判定「疑似硬编码口令」。这里是**故意的**占位值，
# 且已被 _guard_production_secrets 强制约束——它在 staging/prod 下会让应用拒绝启动，
# 所以不存在「悄悄用到线上」的路径。
#
# ★ 长度必须 ≥32 字节：HS256 的密钥长度下界由 RFC 7518 §3.2 规定，
#   短于该值时 PyJWT 会发 `InsecureKeyLengthWarning`。占位值也要守这条，
#   否则本地/测试会一直被这个告警刷屏，真正的告警反而被淹没。
_DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-in-production"  # noqa: S105

# RFC 7518 §3.2：HS256 的密钥长度至少与哈希输出等长（256 bit = 32 字节）
MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 应用 ---
    app_name: str = "TeamHub"
    app_env: Literal["local", "test", "staging", "prod"] = "local"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # --- 数据库 ---
    # 生产 PostgreSQL；本地/测试降级为 SQLite 内存库（11.2）
    # ★ 必须带 +aiosqlite：create_async_engine 不接受同步驱动，
    #   写成 sqlite+pysqlite:// 会在 `import app.main` 时直接抛
    #   InvalidRequestError（"loaded 'pysqlite' is not async"）。
    #   这里选内存库而非文件库，是为了保持「零依赖、跑完即弃」的降级语义；
    #   session.py 据此启用 StaticPool，让内存库能跨会话共享。
    database_url: str = "sqlite+aiosqlite:///:memory:"
    db_echo: bool = False

    # --- Redis ---
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 本地无 Redis 时用 fakeredis 顶替
    use_fakeredis: bool = True

    # --- 认证 ---
    # 默认值是纯粹的本地开发占位符；_guard_production_secrets 会在 staging/prod 下
    # 检测到它未被覆盖时直接拒绝启动，所以它不可能流到生产环境。
    jwt_secret: str = _DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    # 过期 refresh token 的保留天数（定期清理任务的缓冲期）。
    # 留缓冲是为了容忍各节点时钟偏差，避免刚过期的行被删后
    # 仍有飞行中的请求来查它。
    refresh_token_retention_days: int = 7

    # --- 限流（按租户配额，阶段 3 起生效）---
    rate_limit_per_minute: int = 600

    # --- Celery ---
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    # 本地/测试：task_always_eager=True，无需真实 broker（11.2）
    celery_always_eager: bool = True
    # 结果后端。`cache+memory://` 是 Celery 内置的内存后端，零依赖——
    # 本地与 eager 模式下结果就在进程内，用不到外部存储。
    # 生产换 redis：CELERY_RESULT_BACKEND=redis://redis:6379/2
    celery_result_backend: str = "cache+memory://"

    # --- 邮件 ---
    # console：把邮件内容写进日志（本地/测试的零依赖默认）
    # smtp：真实投递
    mail_backend: Literal["console", "smtp"] = "console"
    mail_from: str = "noreply@teamhub.local"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True

    # --- 附件存储 ---
    # 本地模式文件落在项目根下的 local_storage/（已被 .gitignore 忽略）。
    # 生产换 MinIO 时，这个值改为对象存储的挂载点或干脆走 storage 抽象的另一实现。
    storage_dir: str = "local_storage"

    # 上传约束
    max_upload_bytes: int = 10 * 1024 * 1024  # 10 MB
    # 允许的扩展名白名单。**只允许白名单**——任何不在列表里的都拒，
    # 防止把 .html/.svg 这种可执行内容当作附件存下来变成 XSS 载体。
    allowed_upload_exts: set[str] = Field(
        default_factory=lambda: {"png", "jpg", "jpeg", "gif", "pdf", "txt", "md", "zip"}
    )

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    @field_validator("database_url")
    @classmethod
    def _normalize_async_driver(cls, v: str) -> str:
        """把常见的同步 URL 改写成异步驱动，避免 create_async_engine 报错。

        ★ SQLite 有两种同步写法，都要覆盖：
              sqlite://            普通写法
              sqlite+pysqlite://   显式指定同步驱动（SQLAlchemy 的默认 sqlite 驱动名）
          此前只匹配 `sqlite://`，导致 `sqlite+pysqlite://` 原样透传，
          create_async_engine 直接抛 InvalidRequestError。
          改写时用「前缀替换」而不是 str.replace——后者会在
          `sqlite+aiosqlite://` 里再次命中内层的 `sqlite://`，拼出
          `sqlite+aiosqlite+aiosqlite://` 这种畸形 URL。
        """
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        if v.startswith("sqlite") and "aiosqlite" not in v:
            for prefix in ("sqlite+pysqlite://", "sqlite://"):
                if v.startswith(prefix):
                    return "sqlite+aiosqlite://" + v[len(prefix) :]
        return v

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """非本地/测试环境禁止使用占位密钥或过短的密钥。

        这类「默认值忘了改」是最常见的生产事故来源之一，
        所以让它变成**启动即失败**，而不是运行期才发现。

        ★ 除了「不能用占位值」，还要卡长度：只检查「不等于默认值」的话，
          有人设成 `JWT_SECRET=abc` 也能通过——那等于给 HS256 配了一个
          可暴力枚举的密钥，签名形同虚设。长度下界见 RFC 7518 §3.2。
        """
        if self.app_env in ("staging", "prod"):
            if self.jwt_secret == _DEV_JWT_SECRET:
                raise ValueError(
                    f"APP_ENV={self.app_env} 下必须通过环境变量设置 JWT_SECRET，"
                    "不能使用默认占位值（openssl rand -hex 32 生成）"
                )
            if len(self.jwt_secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
                raise ValueError(
                    f"JWT_SECRET 至少需要 {MIN_JWT_SECRET_BYTES} 字节"
                    f"（当前 {len(self.jwt_secret.encode('utf-8'))} 字节）。"
                    "HS256 的密钥长度下界见 RFC 7518 §3.2，"
                    "过短的密钥可被暴力枚举，签名保护形同虚设。"
                    "用 `openssl rand -hex 32` 生成。"
                )
        return self

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
