# -*- coding: utf-8 -*-  # noqa: UP009 -- must stay: Alembic reads this file
# with the system locale codec (GBK on zh-CN Windows), so removing this
# declaration makes non-ASCII comments raise UnicodeDecodeError.
"""Alembic 环境。

★ 两个必须做对的地方：

1. **导入所有模型**
   `import app.models` 保证 Base.metadata 完整。漏了就会 autogenerate 出
   一个「删掉所有表」的迁移——而且不报错，只是安静地少表/删表。

2. **迁移时关闭租户钩子**
   Alembic 跑在无租户上下文的进程里。on_orm_execute 在上下文为空时不过滤
   （坑 1），所以迁移期间 SELECT 会扫全量——在 DDL 语境下这是期望行为，
   但**绝不能**在迁移脚本里用 ORM 做业务数据处理。
   数据迁移必须走 repositories.bypass_context（显式、有审计）。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 必须：让所有模型注册到 Base.metadata
import app.models  # noqa: F401
from app.core.config import settings
from app.core.db.base import Base

config = context.config

# 同步驱动 URL：Alembic 默认走同步引擎
_sync_url = settings.database_url.replace("+asyncpg", "+psycopg2").replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", _sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # 多租户下表很多，明确排序让迁移脚本 diff 稳定
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
