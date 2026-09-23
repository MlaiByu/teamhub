"""迁移与模型一致性测试（第 9 周）。

★ 这个测试防的是一类**启动后才暴露**的事故：

    加了新模型 / 改了列，但忘了 `alembic revision --autogenerate` 生成迁移。

  后果是 `docker compose up` 的 api 服务（`alembic upgrade head && uvicorn`）
  对着**缺表/缺列**的库跑起来——服务能启动，但第一个碰该表的请求就 500。
  本地开发通常发现不了，因为内存库走的是 `create_all`（见
  `main.py::_bootstrap_local_schema`），压根不经过迁移。

  本测试把「迁移建出来的库」与「模型声明出来的库」逐表逐列逐索引对比，
  一旦两者漂移就立刻失败。

★ 为什么用 subprocess 跑 `alembic` 而不是 Alembic 的 Python API：
  `alembic/env.py` 从**已加载的 settings 单例**读 URL，进程内 monkeypatch
  它容易与缓存/连接状态纠缠；起独立进程 + 环境变量最干净，
  而且顺带验证了「命令行调用方式可用」——这正是容器里跑的那条命令。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic(
    args: list[str], *, database_url: str | None = None
) -> subprocess.CompletedProcess:
    env = {**os.environ, "APP_ENV": "test"}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    # noqa: S603 —— args 全部由本文件内部构造（固定子命令），不含外部输入
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _build_with_create_all(db_path: Path) -> None:
    """用模型声明建库（即本地内存库走的那条路径）。"""
    import app.models  # noqa: F401  必须导入，否则 Base.metadata 是空的
    from app.core.db.base import Base

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()


def _schema_of(db_path: Path) -> dict[str, dict[str, set]]:
    """取库结构：表 → {列, 索引, 唯一约束}。"""
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    schema: dict[str, dict[str, set]] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":  # 迁移自己的记账表，模型里没有
            continue
        schema[table] = {
            "columns": {c["name"] for c in inspector.get_columns(table)},
            # SQLite 会为唯一约束自动建同名索引，两类都要看：
            # 自定义索引能反映「索引是否漏建」，唯一约束能反映「约束是否漏建」。
            "indexes": {i["name"] for i in inspector.get_indexes(table)},
            "uniques": {
                tuple(u["column_names"] or []) for u in inspector.get_unique_constraints(table)
            },
        }
    engine.dispose()
    return schema


# ======================================================================
# 迁移链本身
# ======================================================================
def test_migration_has_single_head():
    """迁移链必须只有一个 head。

    多个 head 时 `alembic upgrade head` 会报错（或需要 merge 迁移），
    容器启动命令会直接失败。
    """
    result = _run_alembic(["heads"])
    assert result.returncode == 0, f"alembic heads 失败：{result.stderr}"

    heads = [line for line in result.stdout.splitlines() if line.strip() and "(head)" in line]
    assert len(heads) == 1, f"应恰好有一个 head，实际 {len(heads)} 个：{heads}"


def test_migration_upgrade_downgrade_roundtrip(tmp_path):
    """迁移能升上去，也能降回来——降级链没断才算完整。"""
    db_path = tmp_path / "roundtrip.db"
    url = f"sqlite+aiosqlite:///{db_path}"

    up = _run_alembic(["upgrade", "head"], database_url=url)
    assert up.returncode == 0, f"upgrade 失败：{up.stdout}\n{up.stderr}"
    assert _schema_of(db_path), "upgrade 后没有任何表"

    down = _run_alembic(["downgrade", "base"], database_url=url)
    assert down.returncode == 0, f"downgrade 失败：{down.stdout}\n{down.stderr}"

    remaining = _schema_of(db_path)
    assert remaining == {}, f"downgrade 到 base 后仍残留表：{sorted(remaining)}"


# ======================================================================
# ★ 迁移 vs 模型
# ======================================================================
def test_migration_matches_models(tmp_path):
    """★★ 迁移建出的库必须与模型声明完全一致（逐表、逐列、逐索引、逐唯一约束）。

    不一致通常意味着「加了模型但忘了生成迁移」。
    """
    migrated_db = tmp_path / "migrated.db"
    declared_db = tmp_path / "declared.db"

    result = _run_alembic(["upgrade", "head"], database_url=f"sqlite+aiosqlite:///{migrated_db}")
    assert result.returncode == 0, f"迁移失败：{result.stdout}\n{result.stderr}"

    _build_with_create_all(declared_db)

    migrated = _schema_of(migrated_db)
    declared = _schema_of(declared_db)

    assert migrated, "迁移没有建出任何表"

    missing = sorted(set(declared) - set(migrated))
    extra = sorted(set(migrated) - set(declared))
    assert not missing, (
        f"这些表在模型里有、迁移里没有：{missing}。"
        "很可能是加了模型但忘了 `alembic revision --autogenerate`。"
    )
    assert not extra, f"这些表在迁移里有、模型里没有：{extra}"

    for table in sorted(migrated):
        assert migrated[table]["columns"] == declared[table]["columns"], (
            f"表 {table} 的列不一致："
            f"迁移多 {sorted(migrated[table]['columns'] - declared[table]['columns'])}、"
            f"缺 {sorted(declared[table]['columns'] - migrated[table]['columns'])}"
        )
        assert migrated[table]["indexes"] == declared[table]["indexes"], (
            f"表 {table} 的索引不一致："
            f"迁移多 {sorted(migrated[table]['indexes'] - declared[table]['indexes'])}、"
            f"缺 {sorted(declared[table]['indexes'] - migrated[table]['indexes'])}"
        )
        assert migrated[table]["uniques"] == declared[table]["uniques"], (
            f"表 {table} 的唯一约束不一致"
        )


def test_migrated_schema_covers_all_tenant_tables(tmp_path):
    """★ 隔离底线：所有继承 TenantScopedMixin 的表都必须有 tenant_id 列。

    迁移是「上生产的那一份 DDL」，如果它在某张表上漏了 tenant_id，
    运行时钩子就无从过滤——所以要在迁移产物上直接验，而不是只看模型。
    """
    import app.models  # noqa: F401
    from app.core.db.base import Base
    from app.core.db.mixins import TenantScopedMixin

    # 用 mapper 注册表而不是 table.entity：后者在没有映射类时是 None，
    # 会让集合静默变空、测试假绿。
    tenant_tables = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantScopedMixin)
    }
    assert tenant_tables, "没找到任何租户级表，测试自身可能失效了"

    db_path = tmp_path / "tenant_cols.db"
    result = _run_alembic(["upgrade", "head"], database_url=f"sqlite+aiosqlite:///{db_path}")
    assert result.returncode == 0, result.stderr

    schema = _schema_of(db_path)
    for table in sorted(tenant_tables):
        assert table in schema, f"租户级表 {table} 不在迁移产物里"
        assert "tenant_id" in schema[table]["columns"], (
            f"租户级表 {table} 在迁移产物里缺少 tenant_id 列——隔离会直接失效"
        )
