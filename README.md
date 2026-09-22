# TeamHub

多租户团队协作平台。每个租户拥有独立的组织架构、成员、权限与数据，
在共享数据库的前提下实现**严格的租户级数据隔离**。

> 目标形态：可写进简历 / 可做技术博客 / 可完整讲透的求职作品集项目。
> 完整方案与决策记录见 [PROJECT-PLAN.md](./PROJECT-PLAN.md)。

---

## 5 分钟跑起来

**前置条件：Python ≥ 3.12。** 不需要装 PostgreSQL，也不需要装 Redis ——
本地与测试环境默认走降级模式（SQLite 内存库 + `StaticPool`），
`git clone` 之后一条命令就能跑通全部测试。

> **没装 Python 的话，venv 帮不上忙。** `venv` 是 Python 标准库里的一个**模块**
> （`python -m venv`），必须先有解释器才能调用；它建出来的目录只是**壳**——
> `pyvenv.cfg` 里的 `home` 指回基础解释器，标准库仍从基础解释器读取，
> 删掉基础解释器 venv 立刻失效。venv 解决的是「依赖隔离」，不是「获取解释器」。
>
> 无 Python 环境请直接走 `docker compose up -d`（见下方「跑生产形态」的 Docker 方式），
> 镜像内自带 Python 3.12，宿主机零运行时依赖。

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"

# 跑测试（含隔离测试）
pytest

# 起服务
uvicorn app.main:app --reload
# → http://127.0.0.1:8000/health
# → http://127.0.0.1:8000/docs      （Swagger UI）
# → http://127.0.0.1:8000/api/v1/system/context   （看当前租户上下文）
```

> **内存库的表是启动时自动建的。** 零依赖模式用的是 SQLite 内存库，
> 而 `alembic upgrade head` 是另一个进程、迁移的是另一个空的内存库——
> 对服务进程毫无作用。所以 `app/main.py` 在启动时会按当前模型建表，
> 并打一条 `local_schema_bootstrapped` 警告。
> 这条只对「`local`/`test` + SQLite 内存库」生效；
> **持久化数据库（PostgreSQL / 文件 SQLite）仍然必须用 Alembic 管理表结构**，
> 否则本地与线上会漂移。

跑生产形态（需要 Postgres + Redis）。两条路选一条：

**Docker —— 推荐，宿主机零 Python 依赖：**

```bash
docker compose up -d      # api + worker + beat + postgres + redis
# api 服务会先跑 alembic upgrade head 再起 uvicorn，迁移不会漏
```

**裸机 —— 自己管依赖与迁移：**

```bash
cp .env.example .env      # 改 DATABASE_URL / REDIS_URL / JWT_SECRET
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 多租户方案：共享库 + 行级隔离

三种方案的取舍：

| 方案 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| 独立数据库 | 隔离最强，可独立备份恢复 | 成本高，迁移跑 N 次，连接池爆炸 | 大客户、强合规 |
| 独立 Schema | 隔离较好，同库易管理 | 迁移麻烦，schema 数量膨胀 | 中大型 SaaS |
| **共享库 + 行级隔离**（选中） | 成本低、迁移一次、易维护 | **需严格保证不漏查** | 中小型 SaaS |

选第三种。它唯一的风险是「漏查导致跨租户泄露」，所以整个设计的目标是
**从结构上消除这个风险**，而不是靠开发者自觉。

### 实现：查询层统一注入

用 SQLAlchemy 的 `Session` 事件 `do_orm_execute` + `with_loader_criteria`，
在查询执行前自动追加过滤条件。业务代码**不写** `where tenant_id = ...`。

```
请求 → 中间件解析 JWT → 写入 contextvar(tenant_id / user_id / dept_id / data_scope)
     → 业务查询 → ORM 执行前拦截 → 自动注入两层过滤 → 执行
```

两个标记 Mixin 区分作用范围：

| Mixin | 作用对象 | 注入条件 |
|---|---|---|
| `TenantScopedMixin` | 所有租户级表 | `tenant_id == 当前租户` |
| `DataScopedMixin` | 需要数据权限的表 | `SELF → owner_id == 我`<br>`DEPT → dept_id == 我部门`<br>`ALL → 不加条件` |

两层在同一个钩子里合并成 `AND` 条件：

```sql
WHERE tenant_id = 1                                -- 租户管理员（ALL）
WHERE tenant_id = 1 AND dept_id  = 5               -- 部门主管（DEPT）
WHERE tenant_id = 1 AND owner_id = 42              -- 普通成员（SELF）
```

### 三条必须知道的边界

1. **未设上下文 = 全量泄露。** 钩子的逻辑是「有 tenant_id 才过滤」，所以
   后台任务、管理脚本、Celery worker 一旦漏设上下文就会查到所有租户的数据。
   防线是 `repositories/base.py` 的 `require_tenant_context()`——业务查询先过守卫，
   过不了直接 `raise`。跨租户场景走**显式 bypass**（`bypass_context` + 强制审计），
   而不是「忘了设上下文」。
2. **插入不经过钩子，`tenant_id` 要显式写。** `repository.create()` 统一从上下文取值，
   `NOT NULL` 兜底。
3. **`Session.get()` 命中 identity map 时会短路。** 隔离测试必须用**全新会话**，
   否则测到的是缓存，不是过滤逻辑。

---

## 落库契约（不遵守就会串数据）

| 契约 | 不做会怎样 |
|---|---|
| 所有租户表必带 `tenant_id`（含关联表、日志表、附件表） | 关联表成为泄露后门 |
| 唯一约束必须包含 `tenant_id` | 租户 A 和 B 无法使用相同业务编码 |
| 索引以 `tenant_id` 打头 | 查询全表扫描，租户越多越慢 |
| 外键**必须在应用层校验同租户** | 可把 A 租户的任务挂到 B 租户的项目上 |
| 软删除与租户过滤共存 | 删掉的数据仍能被查到 |

> 第四条最隐蔽：数据库的外键只保证 id 存在，**不保证同租户**。
> PostgreSQL 会老老实实插入成功——只有应用层能拦住。见
> `services/task_service.py::ensure_same_tenant`。

---

## 目录结构

顶层按**技术层**横向切分，每层内部**按领域分文件**。

```
app/
├── main.py                只做组装：路由 / 中间件 / 异常处理器 / 生命周期
├── core/                  横切基础设施，不依赖任何业务
│   ├── config.py          pydantic-settings
│   ├── security.py        密码哈希 / JWT
│   ├── logging.py         structlog + request_id
│   ├── constants.py       枚举（DataScope / TaskStatus / 业务码）
│   ├── exceptions.py      AppException + 全局处理器
│   └── db/                ★ Base / Mixin / 会话 / 钩子必须同处
│       ├── base.py        DeclarativeBase
│       ├── mixins.py      TenantScopedMixin / DataScopedMixin
│       ├── context.py     contextvar（租户/用户/部门/范围）
│       ├── session.py     engine / sessionmaker / get_db
│       └── tenant_hook.py do_orm_execute 双层过滤
├── middleware/            request_id → tenant_context → rate_limit
├── models/                按领域分文件，__init__ 统一再导出
├── schemas/  services/  repositories/  api/  realtime/  tasks/
alembic/                   数据库迁移
tests/
├── conftest.py
└── security/              ★ 本项目最重要的测试
    ├── test_tenant_isolation.py
    └── test_data_scope.py
```

### 为什么 `core/db/` 四件套必须同处

Mixin 要被所有领域 model 引用，钩子又要引用 Mixin。分开放会形成
`core → models → core` 的循环导入，**启动即报错**。这是硬约束。

### 为什么起点选横向而不是纵向

判断依据：**结构应该匹配你当前的信息量，而不是匹配理论上的优雅形态。**

项目刚开始时对领域边界的认知最少。纵向切分（按领域分目录）的收益拆开看：

| 收益来源 | 在「单人 + 10 周」下的实际值 |
|---|---|
| 减少多人文件冲突 | **0** —— 单人开发 |
| 支持并行开发 | **0** —— 单人开发 |
| 文件定位快 | 只在**熟悉领域之后**成立 |
| 领域边界结构强制 | **负值** —— 强制的是还没验证过的边界假设 |

具体例子：最初把 `tasks` 和 `projects` 当成两个领域，做到第 4 周才发现
「任务必须挂在项目下」让两者无法独立演进。纵向切分要付的迁移成本是
移目录 + 改几十处 import；横向布局下只是加了一个 `project_id` 外键。

**所以先选容易改的那个。** 横向随时可以机械地升级为纵向，反过来要拆目录。

**升级信号**（满足才动，不要预先全切）：

> 某个领域攒到 **5 个以上文件**，且与其它领域**低耦合**
> → 单独抽成 `domains/xxx/`

最终形态是「横向为主 + 局部纵向」，这很正常。

---

## 架构规则（硬性）

1. **依赖方向单向**：`api → services → repositories → models`。`core` 永不反向依赖业务。
   `tasks/` 与 `realtime/` 只调用 services，不直接碰 repository。
2. **service 之间单向依赖**：禁止 A→B 且 B→A。跨领域协作放到 **api 层编排**，
   或抽一个 orchestrator。这是横向布局下防循环导入的关键。
3. **层内按领域分文件**：单文件超过约 200 行，或出现两个以上领域概念，就拆。
4. **`Base` / Mixin / 过滤钩子必须同模块**（`core/db/`）。
5. **测试单列 `tests/security/`**：隔离与越权测试不混进业务测试。

---

## 测试策略

**优先级：安全测试 > 业务测试 > 覆盖度。**

```bash
pytest                          # 全部
pytest tests/security -v        # 隔离与越权
pytest --cov=app --cov-report=term-missing
```

### 隔离测试必须包含反向验证

每个隔离用例都成对出现：

- **[正向]** 设了租户上下文 → 只看到本租户
- **[反向]** 清空上下文 → 看到**全部**

**为什么反向验证最关键**：如果该租户下本来就只剩 A 自己的数据，正向断言会
**假通过**——你以为是钩子在起作用，其实只是碰巧没有别人的数据。
只有反向验证才能证明钩子真的在工作。

**没有反向验证的隔离断言，一律视为无效断言。**

### 覆盖率目标

核心 service 层 ≥ 80%，整体 ≥ 70%。
**不要为了覆盖率写无断言的测试**——那比没有测试更糟。

---

## 第 1 周交付状态

| 验收项 | 状态 |
|---|---|
| 项目能起来，`/health` 通 | ✅ |
| 统一响应信封 + 业务码 | ✅ |
| 结构化日志 + 请求 ID | ✅ |
| 多租户钩子 + 两个 Mixin | ✅ |
| Repository 上下文强制校验 + 显式 bypass | ✅ |
| Alembic 初始化 + 初始迁移（15 张表） | ✅ `upgrade → downgrade → upgrade` 往返一致 |
| `tests/security/` 骨架（含反向验证） | ✅ 18 项通过 |
| CI 雏形（lint → type → security test → test + coverage） | ✅ |
| 本地零依赖降级模式 | ✅ SQLite + StaticPool |

## 第 2 周交付状态（用户 / 租户 / 认证 / RBAC 基础）

| 能力 | 接口 |
|---|---|
| 认证：注册 / 登录 / 轮换 / 登出 | `POST /auth/{register,login,refresh,logout}` |
| 当前身份 | `GET /auth/me` |
| 租户：当前信息 / 切换 | `GET /tenants/current` · `POST /tenants/{id}/switch` |
| 部门：树 / 创建 | `GET /departments` · `POST /departments` |
| 成员：列表 / 加入 / 绑角色 | `GET /members` · `POST /members` · `POST /members/{id}/roles` |
| 角色：列表 / 创建 | `GET /roles` · `POST /roles` |

当前 **92 项测试全绿**（含 22 项安全测试），覆盖率 92%，ruff / mypy 干净。

**关键设计决策**：

- **注册 = 开一个团队**。`refresh_tokens` 是租户级表（`tenant_id` 非空），
  只建用户会卡在「无租户 → 签不出 refresh token」的死局。
  自助注册即开通个人团队 + 绑 `TENANT_ADMIN`；平台补开租户共用同一套角色装配。
- **仓储按「表是否带 tenant_id」分两个基类**：全局表用 `BaseRepository`，
  租户表用 `TenantAwareRepository`。登录发生在还没有租户上下文的时刻，
  全局表套守卫会让登录永远失败。
- **跨租户挂载拦截是「免费」的**：外键（部门上级、成员部门、角色）都走守卫查询，
  别的租户的 ID 自然返回 None → 404，不需要逐处手写 `ensure_same_tenant`。

## 第 3 周交付状态（项目 / 任务 / 状态流转）

| 能力 | 接口 |
|---|---|
| 项目：列表 / 创建 / 详情 / 更新 | `GET /projects` · `POST /projects` · `GET /projects/{id}` · `PATCH /projects/{id}` |
| 任务：列表 / 创建 / 详情 / 更新 | `GET /tasks` · `POST /tasks` · `GET /tasks/{id}` · `PATCH /tasks/{id}` |
| 任务状态流转 | `PATCH /tasks/{id}/status` |

**140 项测试全绿**（含 25 项安全测试），覆盖率 94%，ruff / mypy 干净，CI 绿。

**关键设计决策**：

- **跨租户引用分两类，做法不同**（判断标准：引用目标是不是租户级表）
  - 目标是**租户级表**（项目、部门、角色）→ 走守卫查询，别的租户的 ID
    自然返回 `None` → 404，**不需要额外写校验**
  - 目标是**全局表** → 钩子覆盖不到，**必须显式校验**。
    `task.assignee_id` 指向全局 `users` 表，是当前唯一一处，
    用 `ensure_assignee_is_member()` 兜底。失效后果不是报错而是**静默串数据**：
    A 公司的任务分配给 B 公司的人，对方会在自己的待办里看到它
- **任务 `owner_id` 取「被分配人」而非创建者**（未分配时退回创建者）。
  这样 SELF 数据范围的语义是「**分配给我的**任务」。若取创建者，
  被管理员分配任务的普通成员会在列表里看不到自己的任务。改派时同步重算归属。
- **`owner_id` / `dept_id` 绝不接受客户端传入**。它们决定该行落在谁的
  数据范围内，允许请求体指定等于让数据权限失效。
- **状态流转独立成接口**，不塞进通用 `PATCH`：规则要集中校验
  （否则容易被「顺手改 status」绕过），审计上也要能区分「改优先级」与「推进状态」。
  非法跳转返回 409（业务冲突）而非 422——请求格式没问题，是状态不允许。
- **数据范围来自 token 声明，不每请求重算**。有意取舍：鉴权路径不查库，
  代价是角色变更要等 access token 过期（默认 30 分钟）才生效。

> `tests/security/test_data_scope.py` 的越权矩阵**全程只走公开接口、不碰数据库**，
> 因此它同时验证了「注册 → 邀请 → 挂部门 → 绑角色 → 登录 → 查列表」
> 整条 RBAC 链路能串起来。每个断言**成对**出现（正向 + 反向），
> 没有反向验证的断言一律视为无效。

下一阶段目标（第 4 周）：评论 / @提及 / 附件 / 操作日志（PROJECT-PLAN 十二）。

---

## 环境变量

见 [.env.example](./.env.example)。几个关键项：

| 变量 | 本地默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///:memory:` | 生产改 `postgresql://...`，会自动改写为 asyncpg 驱动。SQLite 写成 `sqlite://` 或 `sqlite+pysqlite://` 也会被自动改写为 aiosqlite——`create_async_engine` 不接受同步驱动 |
| `APP_ENV` | `local` | `staging` / `prod` 下会强制校验 `JWT_SECRET`：既不能是默认占位值，也不能短于 32 字节（HS256 的密钥下界，RFC 7518 §3.2） |
| `USE_FAKEREDIS` | `true` | 本地无 Redis 时用 fakeredis |
| `CELERY_ALWAYS_EAGER` | `true` | 本地同步执行任务，不需要 broker |

> ⚠️ **`cp .env.example .env` 会把数据库指向 PostgreSQL**（见该文件里的 `DATABASE_URL`）。
> 想保持零依赖，就不要覆盖 `DATABASE_URL`，删掉这一行或改回上面的 SQLite 值。

---

## 已知环境坑（Windows / zh-CN）

记录在这里避免重复踩：

1. **`alembic.ini` 必须是纯 ASCII。** `configparser` 用系统区域编码（GBK）读它，
   含中文会让 alembic 直接抛 `UnicodeDecodeError`。
2. **`alembic/env.py` 必须保留 `# -*- coding: utf-8 -*-`。** 同理，Alembic 用系统
   编码读该文件。这行在 Ruff 看来是多余的（UP009），已就地 `noqa` 并注明原因。
3. **`JSONB` 不能直接当列类型用。** SQLite 编译器渲染不了 JSONB，建表即
   `CompileError`。必须 `JSON().with_variant(JSONB(), "postgresql")`。
4. **Alembic autogenerate 生成的 `with_variant` 列会漏 `Text` 导入。**
   已在 `script.py.mako` 里预置 `from sqlalchemy import Text`。

---

## License

私有项目，未授权。
