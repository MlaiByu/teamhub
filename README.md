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

## 第 4 周交付状态（评论 / 实时通知事件系统）

**6 个事件类型全部可用**：定义 → 触发 → 落库 → 推送，端到端跑通。

| 事件 | 触发点 |
|---|---|
| `task.assigned` | 创建任务时分配 / 改派 |
| `task.status_changed` | 任务状态流转 |
| `task.commented` | 发表评论 |
| `task.mentioned` | 评论中 `@提及` |
| `member.joined` | 被加入团队 |
| `role.assigned` | 被授予角色 |

| 接口 | 说明 |
|---|---|
| `GET /notifications?unread=true` | 通知列表（分页） |
| `GET /notifications/unread-count` | 未读数（角标，也是断线兜底） |
| `POST /notifications/{id}/read` | 标记单条已读（幂等） |
| `POST /notifications/read-all` | 全部标记已读 |
| `GET|POST /tasks/{id}/comments` | 评论列表 / 发表评论 |
| **`WS /api/v1/ws`** | 实时通知推送 |

**264 项测试全绿**（含 25 项安全测试），覆盖率 95%，ruff / mypy 干净，CI 绿。

### 关键设计决策

- **连接按 `(tenant_id, user_id)` 索引**（PROJECT-PLAN 风险 8）。本项目的 `User`
  是全局表——同一账号可加入多个租户。若只按 `user_id` 索引，`user_id=42` 在租户 A
  与 B 各有连接时会落进同一个桶，推给 A 的通知就送到 B 的 socket 上：
  **静默跨租户泄露**，不报错，只是某天有人在 B 公司界面看到 A 公司的任务标题。
  推送函数的 `tenant_id` 是**必填关键字参数**，让「忘了传租户」在签名层面无法发生。
- **握手校验在 `accept()` 之前**。未认证的连接不该先占住服务端资源，也不该留下
  「忘了断开就成永久通道」的风险。代价是客户端看到的是 HTTP 403 而非 WS 关闭码
  ——这是有意的取舍，已在代码里写明。
- **令牌类型必须是 access**。refresh 有效期长（14 天）且唯一用途是换 access，
  允许它建连等于把长有效期凭据暴露在 URL 里。
- **`emit` 是事件落库与投递的唯一入口**，时序固定为
  **校验 → 落库 → 推送**。通知失败一律降级返回 `None` 不抛异常——
  调用点在业务操作成功之后，此时抛错会让客户端以为业务失败并重复提交。
- **`emit` 内建「目标必须是本租户 ACTIVE 成员」守卫**。触发点有 6 个且还会增加，
  靠每处各自记得校验必然漏一处；漏掉的后果是给已离开团队 / 属于别的公司的人发通知。
- **每个事件类型一份独立的 payload 模型**（非裸 dict）。payload 会落库（JSONB）
  也会推给前端，裸 dict 的话字段差异只能靠注释，写入端与消费端各写一遍名字，
  改名必然漂移且只在运行期暴露。
- **提及解析是权限边界**：客户端不能指定「通知谁」，只能写 `@名字`，
  服务端只认本租户 ACTIVE 成员。正则刻意用宽松白名单——
  **漏发一条真实通知比多解析一个解析不到的名字严重得多**。

### 测试策略：每个事件都成对验证

事件系统最典型的故障不是「没发」，而是**不该发的也发了**（自己给自己派活、
自己推进自己的任务、重复点两次保存、同一角色绑两遍）。噪音会让人关掉通知，
等于系统失效。所以每条触发点都配了反向断言：

- **结构性自检**：遍历 `EventType` 断言全部已注册；断言标题模板的占位符都存在于
  对应 payload（防 `{task_titel}` 这类拼写错，它平时只表现为用户看到降级文案）；
  断言 payload 字段不与信封字段重名
- **真实连接验证**：起真 uvicorn + `websockets` 客户端，断言 REST 触发的事件
  真的推到了连接上，且另一租户的连接 3 秒内无任何消息。
  **握手正确 ≠ 推送能通**，两层都验

## 第 5 周交付状态（附件 / 操作日志）

| 接口 | 说明 |
|---|---|
| `POST /attachments?biz_type=&biz_id=` | 上传附件（multipart） |
| `GET /attachments?biz_type=&biz_id=` | 附件列表 |
| `GET /attachments/{id}/download` | 下载 |
| `GET /audit-logs?...` | 审计日志（**仅 TENANT_ADMIN**，支持四维过滤 + 分页） |

**289 项测试全绿**，覆盖率 95%，ruff / mypy 干净（4 个既有错误未增），CI 绿。

### 附件关键设计

- **存储抽象**：`StorageBackend` 协议 + `LocalStorage` 实现。方案明确
  「先本地存储，MinIO 作为可替换实现」——service 面向协议编程，换后端 = 换实现类。
- **文件按 `<tenant_id>/<uuid>.<ext>` 落盘**，文件系统层面就按租户分开。
- **路径穿越防线**：`path` 来自数据库，`Path.resolve()` 后校验仍在存储根内，
  否则 `../../etc/passwd` 这类脏数据会让下载接口读到根外文件。
- **归属校验**：附件挂靠的实体（任务/项目）必须走它自己的守卫查询，
  跨租户 / 超数据范围自然 404——附件可见性继承挂靠实体。
- **两道安全阀**（都在落盘前）：大小上限（10MB）+ 扩展名白名单
  （附件会被下载回浏览器，允许 .html/.svg 就是 XSS 载体）。
- **下载文件名服务端生成**：不用原始文件名（用户输入，防 Content-Disposition 头注入）。

### 审计关键设计

- **审计正确性不依赖异步**：`record` 同步落库，失败降级记日志但绝不抛异常。
  审计是「事后可查」的旁路，抛错会让客户端误以为业务失败并重复提交。
  （Celery 异步排在第 7 周，届时把 `record` 换成 `record.delay` 即可，调用点不变。）
- **只记写操作**（有明确操作者 + 实体），读操作不记——否则日志被刷爆。
- **`detail` 只存最小键集**：不存评论全文/文件字节，只存「谁在何时对什么做了什么」。
- **查询接口只对管理员**（`require_roles(TENANT_ADMIN)`），租户隔离由钩子天然保证。
- **客户端 IP 走 `current_client_ip` contextvar**：中间件写入，优先 `X-Forwarded-For`。
  该头可伪造，只用于审计留痕、不做安全决策。

## 第 6 周交付状态（数据权限越权矩阵）

**无新增接口**，本阶段的交付物是**测试矩阵 + 一个被它抓出来的真漏洞修复**。

`tests/security/test_data_scope_matrix.py`（48 项）把「**身份 × 资源 × 接口**」
的组合一次钉死，而不是逐个接口写用例。

**340 项测试全绿**（含 73 项安全测试），覆盖率 95%，mypy 全绿，CI 绿。

### 矩阵设计

四个身份覆盖三层数据范围：

| 身份 | 角色 | 范围 | 可见资源 |
|---|---|---|---|
| `alice` | TENANT_ADMIN | ALL | 全部 4 个 |
| `mgr` | DEPT_MANAGER | DEPT（研发部） | 本部门的 2 个 |
| `mem_a` | MEMBER | SELF（研发部） | 自己的 1 个 |
| `mem_b` | MEMBER | SELF（市场部） | 自己的 1 个 |

四个资源覆盖三种归属（管理员私有 / 本部门 / 别的部门），对 9 组接口做正向 + 反向断言：

- **列表**：返回集合**精确等于**期望集合（多一条是泄露，少一条是误杀）
- **详情 / 更新 / 状态流转 / 评论读写 / 附件列表与下载**：
  范围内 200、范围外 **404**（不是 403——避免存在性泄露）
- 新增受保护资源时只需在 `SEEDS` 加一行，所有身份 × 该资源的组合自动被覆盖

### ★ 矩阵首跑就抓出一个真越权漏洞

9 个失败**全部集中在附件**，且只失败受限身份（`mgr`/`mem_a`/`mem_b`）——
`alice`(ALL) 因本就该看全部而看不出问题。矩阵按身份精确指出了问题所在。

**根因**：`Attachment` 表不带 `DataScopedMixin`，而 `list_attachments` 只查附件表
（仅租户过滤），**没有任何挂靠实体可见性校验**。后果：

- 带 `biz_id=<别人的项目>` → 能列出同租户别人项目的附件
- **不带参数** → 列出全租户所有附件（最容易漏的一条：带参数时会想着校验，不带反而全表扫）
- 下载 → 知道 id 就能下载任何同租户附件

对比：评论**做对了**（`list_comments` 先 `TaskRepository.get_or_404(task_id)`），
所以评论的用例全过——同一套「**可见性继承挂靠实体**」原则，附件漏了这一道。

**修复**：
- 仓储拆两个方法，把「谁负责校验」写进名字：`list_for_biz`（调用方必须先校验）
  vs `list_for_scope`（传入可见实体 id 集合做 IN 过滤）
- 给了 `biz_type`+`biz_id` → 先 `_ensure_biz_visible`（跨范围 404）再查
- 没给全 → 先用实体自己的守卫查询算出可见 id 集合，再 `list_for_scope`
- `read_attachment`（下载）同样补上校验
- 算可见集合时用 `list_all()`（**实体查询**）而非 `select(Project.id)`：
  后者只选列不选实体，`with_loader_criteria` 的数据范围过滤**不一定会注入**——
  宁可多取几列，也不要一个「看着像过滤了、其实没过滤」的查询，
  那正是这个漏洞的成因

为什么**不**给 `Attachment` 加 `DataScopedMixin`：附件可见性语义是
「继承挂靠实体」而不是「继承上传者」。加了会把语义变成「上传者自己的范围」
（A 传附件到 B 负责的任务，B 就该能看到），还得补 `dept_id` 列与迁移。

> **本阶段的真正价值不在「写了多少用例」，而在「矩阵暴露了一个所有既有测试都没覆盖的
> 越权路径」。** 前 5 周每个功能都配了测试，但都是「这个接口对不对」；
> 矩阵问的是「同一个接口，换个身份还对不对」——后者才是越权的检测方式。

下一阶段目标（第 7 周）：**Celery 异步任务**（邮件异步发出、审计异步落库）。

> 方案十二第 7 周原定「WebSocket 通知 + Celery 异步任务」两项。
> **WebSocket 通知已在第 4 周随事件系统一并落地**（`WS /api/v1/ws` + 6 类事件推送，
> 含真实连接端到端验证），所以第 7 周实际只剩 Celery 这一半。
> 届时审计的 `record` 换成 `record.delay`、通知改用 outbox 投递，
> 调用点与签名均已留好位置（见对应模块 docstring）。

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
