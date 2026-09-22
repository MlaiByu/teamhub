# TeamHub · 项目长期约定

> 详细方案见仓库根目录 `PROJECT-PLAN.md`，此处只记跨会话必须遵守的约定。

## 项目定位

多租户团队协作平台。求职作品集项目，目标是「可写进简历 / 可做技术博客 /
可完整讲透」。10 周周期，第 3 周为 MVP 检查点。

- 技术栈：FastAPI + SQLAlchemy 2.0(**async**) + Pydantic v2 + PostgreSQL
  + Alembic + JWT + structlog；阶段 3 引入 Redis / Celery / WebSocket
- 路径：`C:\Users\12607\Desktop\teamhub`
- Python 运行时：项目内 `.venv`。**版本锚定 3.12**（2026-09-21 对齐）——
  改 `requires-python` 时必须同步 `[tool.ruff] target-version` 与
  `[tool.mypy] python_version` 两处，否则又会出现「本地绿、容器炸」。
  本机 venv 实为 3.13.14（WorkBuddy 托管运行时，非项目自装），满足 ≥3.12。

## 硬性规则

1. **隔离是地基，不是后期功能。** 新增任何表都必须先确认是否继承
   `TenantScopedMixin`；关联表也要带 `tenant_id`。
2. **`core/db/` 四件套不许拆开**：`base.py` / `mixins.py` / `context.py` /
   `tenant_hook.py`。拆开 = `core → models → core` 循环导入。
3. **`repositories/` 是唯一数据入口。** 业务代码不写 `session.execute(select(...))`，
   也不手写 `where tenant_id = ...`（钩子会注入，手写会重复且在 bypass 时冲突）。
   **基类按「表是否带 `tenant_id`」二分**（第 2 周确立）：
   全局表（`User`/`Tenant`/`Permission`）→ `BaseRepository`（无守卫）；
   租户表 → `TenantAwareRepository`（有守卫）。
   选错的后果：全局表套守卫 → 登录等无上下文场景永远失败；
   租户表漏守卫 → 跨租户静默泄露。判断依据是模型有没有继承 `TenantScopedMixin`。
4. **跨租户必须走显式 bypass**（`bypass_context` / `bypass_*` 方法），
   绝不用「忘了设上下文」来实现。bypass 强制写审计。
   `bypass_context` 已改为 snapshot + restore（不再清空外层上下文）；
   bypass 下写租户表必须显式传 `tenant_id`（守卫返回 0，不拦会写坏数据）。
5. **依赖方向单向**：`api → services → repositories → models`；`core` 不反向依赖业务；
   `tasks/` 和 `realtime/` 只调 services。
   → 因此**不能**在 `core/db/session.py` 里 `import app.models`；
   需要「按模型建表」这类操作时放在组装点（`app/main.py`）。
6. **service 之间单向依赖**，禁止 A→B 且 B→A；跨领域协作放 api 层编排。
7. **`tests/security/` 随功能同步更新**，禁止积压（第 3 周起生效，实际已提前）。
8. **隔离断言必须成对**：正向（只看到自己的）+ 反向（清空上下文看到全部）。
   **没有反向验证的隔离断言一律视为无效断言。**
9. **动 `repositories/base.py` / `tenant_hook.py` 这类隔离地基时必须配回归测试。**
   守则：拆基类、改守卫、改上下文，都要在 `tests/security/` 加断言钉住
   「全局表可访问 + 租户表仍被拒」两侧。

## 落库契约（违反必出跨租户串数据）

| 契约 | 说明 |
|---|---|
| 必带 `tenant_id` | 含关联表、日志表、附件表 |
| 唯一约束含 `tenant_id` | 如 `UNIQUE(tenant_id, project_code)` |
| 索引以 `tenant_id` 打头 | 如 `(tenant_id, status, created_at)` |
| 外键应用层校验同租户 | FK 只保证 id 存在，不保证同租户 |
| 软删除与租户过滤共存 | 钩子注入时同步排除 `is_deleted` |

## 环境坑（Windows + zh-CN，改 Alembic 相关文件前必读）

1. **`alembic.ini` 保持纯 ASCII**——`configparser` 用系统编码（GBK）读它。
2. **`alembic/env.py` 的 `# -*- coding: utf-8 -*-` 不能删**（Ruff UP009 已 `noqa`）
   ——Alembic 用系统编码读该文件。
3. **JSON 列用 `JSON().with_variant(JSONB(), "postgresql")`**，不能直接用 JSONB
   ——SQLite 渲染不了。
4. **`alembic/script.py.mako` 已预置 `from sqlalchemy import Text`**
   ——autogenerate 渲染 `with_variant` 时会用到但不自动导入。
5. **`alembic.ini` 不放 ruff post-write hook**——venv 内 `python -m alembic`
   找不到 `console_scripts` 入口点，会让每次生成迁移都以 FAILED 结尾（文件其实已生成）。

## 启动相关坑

- **`DATABASE_URL` 必须带 `+aiosqlite`。** `create_async_engine` 不接受同步驱动，
  写成 `sqlite+pysqlite://` 会让 `import app.main` 直接抛
  `InvalidRequestError`（"loaded 'pysqlite' is not async"）。
  默认值已修正；归一化器现在同时处理 `sqlite://` 与 `sqlite+pysqlite://`。
  改写用**前缀切片拼接**，不能用 `str.replace`——后者会在
  `sqlite+aiosqlite://` 里再次命中内层 `sqlite://`，拼出畸形 URL。
- **内存库的表由启动时 `create_all` 建**（`app/main.py::_bootstrap_local_schema`）。
  内存库随进程生灭，`alembic upgrade head` 是另一个进程、迁移的是另一个空库，
  对服务进程毫无作用。仅在「`local`/`test` + SQLite 内存库」生效；
  持久化数据库仍必须走 Alembic。启动会打 `local_schema_bootstrapped` 警告。
- **`cp .env.example .env` 会把库指向 PostgreSQL**，破坏零依赖模式。
  想保持零依赖就不要覆盖 `DATABASE_URL`。
- **测试绿 ≠ 能启动。** 已踩过两次：①`DATABASE_URL` 写成同步驱动；
  ②内存库没有表。两次都是因为 **`conftest.py` / CI 自己额外做了准备**
  （设 `DATABASE_URL`、`create_all`），把坏默认值掩护住了。
  **规律：凡是「测试与 CI 自己多走了一步」的路径，必须专门用真实启动验证。**
  改动启动路径后要真的起一次 `uvicorn`，不能只跑 pytest。

## 测试与验证方式

```bash
.venv/Scripts/python.exe -m pytest tests -q                    # 全量
.venv/Scripts/python.exe -m pytest tests/security -v           # 安全测试
.venv/Scripts/python.exe -m ruff check app tests alembic       # lint
.venv/Scripts/python.exe -m mypy app                           # 类型（见下方遗留）
.venv/Scripts/python.exe -m pytest --cov=app --cov-report=term-missing
```

- **`[tool.coverage.run]` 必须带 `concurrency = ["greenlet", "thread"]`。**
  SQLAlchemy async 引擎内部用 greenlet 桥接同步驱动，`await session.execute()`
  会把执行流切进 greenlet，coverage 默认只按 thread 跟踪上下文 → 切出去后
  同一协程函数里的后续行**不再被记录**。
  症状：整个 `async def` 只统计到签名那一行，函数体全算未覆盖。
  2026-09-22 实测影响：`auth_service` 47%→85%，整体 82%→90%。
  **缺这一行时所有 async 业务代码的覆盖率都不可信。**
- 若 `mypy` 报 `No module named mypy`，说明 venv 没按 `pip install -e ".[dev]"`
  完整安装——先补跑该步骤。
- **`mypy` 有 4 个既有错误未修**（`logging.py:41`、`tenant_hook.py:78/87/93`），
  都是动态 ORM 用法的类型精度问题，非运行时 bug。
  CI 用 `continue-on-error: true` 掩盖。修的话要动隔离核心文件，需慎重。
  （原为 6 个，第 2 周重构 `repositories/base.py` 时顺手修掉 2 个。）

- 本地零依赖：SQLite 内存库 + `StaticPool`，**不需要** Postgres / Redis。
- 隔离测试必须用**全新会话**（`db_session_factory()`），因为 `Session.get()`
  命中 identity map 时不发 SQL，会测到缓存而非过滤逻辑。
- 覆盖率目标：核心 service ≥ 80%，整体 ≥ 70%（当前 90%）。

## 领域边界判断（架构）

起点用**横向分层**（api / models / schemas / services / repositories），
层内按领域分文件。理由：结构应匹配当前信息量，纵向切分的四项收益在
「单人 + 10 周」下三项为零、一项为负。

**升级信号**：某领域攒到 **5+ 文件**且与其它领域**低耦合** → 抽成 `domains/xxx/`。
按需抽取，不要预先全切；最终形态是「横向为主 + 局部纵向」。

## 明确不做（防范围失控）

多公司结算/发票、字段级权限、工作流引擎、移动端 App、微服务拆分、
多语言国际化。时间不够时的砍伐顺序见 PROJECT-PLAN 十二。

## 沟通偏好（承接用户全局约定）

- 改完给「修改文件 + 行为变化 + 验证方式」表格清单，不要只说"搞定了"。
- 先自检（跑构建/测试）再回报，不带着猜测反复问。
- 先分清「咨询」还是「实现」：问"这个方案怎么样"时只给判断与取舍，别顺手写代码。

## 工程状态备注

- **teamhub 目录尚未 `git init`**（截至 2026-09-20）。需要提交历史或做周报盘点时，
  先确认这一点，或先初始化仓库再谈"按提交记录归类"。
- 姊妹项目 `C:\Users\12607\Desktop\bluemoon` 是 git 仓库，同一台机器上并行推进；
  做跨项目盘点/周报时可一并纳入。
- bluemoon 的 `origin/main` 已 `gone`（远端被删），本地有未推送的领先提交。
