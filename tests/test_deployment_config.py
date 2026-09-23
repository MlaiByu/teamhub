"""部署配置一致性测试（第 9 周）。

★★ 本机没有 Docker，`docker compose up` 无法真跑。但**「一键起失败」的原因
   大多不是容器本身，而是配置里引用了一个不存在的东西**：

       command: celery -A app.tasks.celery_app ...   ← 模块可能不存在
       command: alembic upgrade head                 ← 迁移脚本可能缺失
       env_file: .env                                ← 文件可能不在仓库里
       volumes: ./docker/postgres/init.sql           ← 路径可能写错
       environment: DATABASE_URL / REDIS_URL ...     ← 变量名可能与 settings 不符

   这类问题**全都不需要 Docker 就能查出来**：把 compose 里出现的每个名字
   拿回代码/仓库里核对一遍即可。本文件把这件事做成可重复的检查，
   而不是上线前人工扫一眼。

★ 两个真实案例（都是这套检查能提前抓到的）：

   1. 第 7 周之前 `app/tasks/celery_app.py` **根本不存在**，
      而 compose 的 worker/beat 都引用它——这两个服务必然启动失败。
      该问题从第 1 周潜伏到第 7 周才被发现。

   2. `.env` 被 `.gitignore` 正确排除（不该进仓库），
      但 compose 声明的是 `env_file: .env`——clone 下来没有这个文件，
      `docker compose up` 会在解析阶段直接失败。修法是 `required: false`。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

# 应用自身的服务（需要核对它们的环境变量是不是 settings 的字段）
APP_SERVICES = ("api", "worker", "beat")


@pytest.fixture(scope="module")
def compose() -> dict:
    assert COMPOSE_FILE.is_file(), "docker-compose.yml 不存在"
    data = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and "services" in data, "compose 结构异常"
    return data


# ======================================================================
# 结构与依赖图
# ======================================================================
def test_compose_parses_and_has_expected_services(compose):
    """YAML 必须能解析，且五个服务齐全。

    一个字符的缩进错误就会让 compose 在解析阶段整体失败——
    这属于「一键起」最直接的失败模式。
    """
    assert set(compose["services"]) >= {"postgres", "redis", "api", "worker", "beat"}


def test_depends_on_references_existing_services(compose):
    """`depends_on` 引用的服务必须存在（拼错服务名 compose 会直接报错）。"""
    names = set(compose["services"])
    for service, spec in compose["services"].items():
        for dep in spec.get("depends_on") or {}:
            assert dep in names, f"{service}.depends_on 引用了不存在的服务 {dep}"


def test_healthcheck_gated_dependencies_target_services_with_healthcheck(compose):
    """要求 `condition: service_healthy` 时，被依赖的服务必须**真的定义了健康检查**。

    否则 compose 会一直等一个永远不会出现的健康状态——表现为「卡住不动」，
    没有任何报错，非常难查。
    """
    for service, spec in compose["services"].items():
        for dep, options in (spec.get("depends_on") or {}).items():
            if isinstance(options, dict) and options.get("condition") == "service_healthy":
                assert "healthcheck" in compose["services"][dep], (
                    f"{service} 等待 {dep} 健康，但 {dep} 没有定义 healthcheck"
                )


# ======================================================================
# build / Dockerfile
# ======================================================================
def test_build_context_has_dockerfile(compose):
    assert (PROJECT_ROOT / "Dockerfile").is_file(), "有服务声明 build 但仓库里没有 Dockerfile"


def test_dockerfile_copy_sources_exist():
    """Dockerfile 里每个 `COPY <src>` 的源路径都必须存在。

    ★ 这条最容易漏：`COPY alembic ./alembic` 写错成别的目录名时，
      本地毫无感觉（docker build 在别人机器上才报错）。
    """
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    missing: list[str] = []
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=" in stripped:  # 多阶段间拷贝，源是上一阶段的路径
            continue
        parts = stripped.split()[1:]
        # 去掉选项（如 --chown=...）；最后一个是目标，其余是源
        sources = [p for p in parts[:-1] if not p.startswith("--")]
        for src in sources:
            if not (PROJECT_ROOT / src).exists():
                missing.append(src)
    assert not missing, f"Dockerfile 的 COPY 源不存在：{missing}"


# ======================================================================
# command 里引用的模块 / 脚本
# ======================================================================
def _python_targets(command: str) -> list[str]:
    """从命令里抽出需要能导入的 Python 模块路径。"""
    targets: list[str] = []
    # uvicorn app.main:app
    targets += re.findall(r"uvicorn\s+([a-zA-Z_][\w.]*)(?::\w+)?", command)
    # celery -A app.tasks.celery_app
    targets += re.findall(r"-A\s+([a-zA-Z_][\w.]*)", command)
    return targets


def test_command_python_targets_are_importable(compose):
    """★ 命令里引用的 Python 模块必须真的能导入。

    这是本文件最有价值的一条——`celery -A app.tasks.celery_app` 指向一个
    不存在的模块时，容器会以「退出码 1 + 一行 ModuleNotFoundError」结束，
    而这些在本地（不用 Docker）完全看不出来。
    """
    checked: list[str] = []
    for service in APP_SERVICES:
        command = str(compose["services"][service].get("command", ""))
        for module in _python_targets(command):
            checked.append(f"{service}->{module}")
            importlib.import_module(module)  # 导入失败即测试失败
    assert checked, "没有从命令里解析出任何 Python 目标，检查逻辑可能失效了"


def test_api_command_runs_migrations_before_serving(compose):
    """api 必须先迁移再起服务，否则会对着空库启动。"""
    command = str(compose["services"]["api"]["command"])
    assert "alembic upgrade head" in command, "api 启动命令里没有跑迁移"
    assert command.index("alembic upgrade head") < command.index("uvicorn"), (
        "迁移必须在 uvicorn 之前执行"
    )


def test_alembic_assets_exist():
    """`alembic upgrade head` 依赖 ini 与脚本目录都进镜像。"""
    assert (PROJECT_ROOT / "alembic.ini").is_file(), "缺少 alembic.ini"
    assert (PROJECT_ROOT / "alembic" / "env.py").is_file(), "缺少 alembic/env.py"
    versions = PROJECT_ROOT / "alembic" / "versions"
    migrations = [p for p in versions.glob("*.py") if not p.name.startswith("__")]
    assert migrations, "alembic/versions 下没有任何迁移脚本"


# ======================================================================
# volumes / env_file
# ======================================================================
def test_bind_mount_sources_exist(compose):
    """bind mount（`./x:/y`）的宿主路径必须存在——路径写错容器起不来。"""
    for service, spec in compose["services"].items():
        for mount in spec.get("volumes") or []:
            if not isinstance(mount, str) or ":" not in mount:
                continue
            host = mount.split(":")[0]
            if host.startswith("./") or host.startswith("../"):
                assert (PROJECT_ROOT / host).exists(), f"{service} 挂载的宿主路径不存在：{host}"


def test_env_file_is_optional_or_exists(compose):
    """★★ `env_file` 引用的文件要么存在，要么显式标 `required: false`。

    `.env` 被 `.gitignore` 排除是**正确**的，但 compose 若直接写
    `env_file: .env`，clone 下来 `docker compose up` 会在解析阶段就失败，
    报一句「env file not found」——对刚拿到仓库的人是纯粹的劝退。
    """
    for service in APP_SERVICES:
        env_file = compose["services"][service].get("env_file")
        if env_file is None:
            continue
        entries = env_file if isinstance(env_file, list) else [env_file]
        for entry in entries:
            if isinstance(entry, str):
                path, required = entry, True
            else:
                path, required = entry.get("path"), entry.get("required", True)
            exists = (PROJECT_ROOT / str(path)).is_file()
            assert exists or required is False, (
                f"{service} 的 env_file `{path}` 既不存在、也没有标 required: false；"
                "clone 下来会直接起不来"
            )


def test_env_var_names_match_settings_fields(compose):
    """★ compose 里给应用服务设的环境变量，必须是 settings 认识的字段。

    变量名拼错（如 `RATE_LIMIT_PER_MIN` 少个 UTE）不会报错，
    只会**静默使用默认值**——配置看着改了、实际没生效。
    """
    from app.core.config import Settings

    valid = {name.upper() for name in Settings.model_fields}
    unknown: dict[str, list[str]] = {}
    for service in APP_SERVICES:
        for key in compose["services"][service].get("environment") or {}:
            # Compose 的插值语法 `${JWT_SECRET:?...}` 不是 settings 字段名，
            # 但它映射到的键名本身仍是 settings 字段，取冒号前的部分即可。
            if key not in valid:
                unknown.setdefault(service, []).append(key)
    assert not unknown, f"compose 里出现 settings 不认识的变量：{unknown}"


# ======================================================================
# 零依赖降级（compose 之外，但同属「能不能一键起来」）
# ======================================================================
def test_settings_load_with_zero_env(monkeypatch):
    """**不设任何环境变量**时 settings 必须能加载——零依赖降级的前提。

    这条保证「clone 下来直接 pytest / 直接 uvicorn」不会因为缺环境变量而炸。
    """
    for key in (
        "APP_ENV",
        "DATABASE_URL",
        "REDIS_URL",
        "USE_FAKEREDIS",
        "CELERY_ALWAYS_EAGER",
        "CELERY_BROKER_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    from app.core.config import Settings

    fresh = Settings(_env_file=None)  # type: ignore[call-arg]
    assert fresh.database_url.startswith("sqlite"), "零依赖默认库应为 SQLite"
    assert fresh.use_fakeredis is True, "零依赖默认应使用内存 Redis"
    assert fresh.celery_always_eager is True, "零依赖默认应 eager 执行任务"
