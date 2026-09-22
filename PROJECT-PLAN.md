# TeamHub 多租户团队协作平台 · 项目方案

> 版本 v1.0 ｜ 目标形态：可写进简历 / 可做技术博客 / 可完整讲透的求职作品集项目
> 周期：10 周（全职）／14 周（边求职边做）

---

## 一、项目概述

### 1.1 一句话定位

支持多企业入驻的团队协作平台。每个租户拥有独立的组织架构、成员、权限与数据，
在共享数据库的前提下实现**严格的租户级数据隔离**，并提供项目/任务管理、
实时通知、操作审计等协作能力。

### 1.2 目标用户与场景

| 角色 | 场景 |
|---|---|
| 平台运营方 | 开通租户、查看平台级统计、处理租户异常 |
| 租户管理员 | 维护本企业部门与成员、分配角色、查看操作审计 |
| 部门主管 | 管理本部门项目与任务、查看本部门数据 |
| 普通成员 | 查看/操作自己参与的项目与任务、接收实时通知 |

### 1.3 为什么这个项目适合作为求职作品集

1. **纯后端工程，不依赖算法或 AI 积累**——考察的是工程能力本身
2. **多租户 + 双层权限是企业级系统的核心难点**，面试官非常爱问，且能明显区分"做过 CRUD"和"想过隔离"的人
3. **FastAPI 的依赖注入、Pydantic v2、异步特性都能真实用上**，不是硬套
4. **可自然引入 Redis、Celery、WebSocket、PostgreSQL**，技术栈有纵深但每一层都有存在理由
5. **有明确的"取舍故事"可讲**——三种隔离方案的权衡、查询层自动过滤的代价、分层结构的演进

### 1.4 明确不做（边界）

写清边界比写清功能更显专业。以下**明确排除**，避免范围失控：

- ❌ 不做多公司/多法人结算、不做发票与财务
- ❌ 不做字段级权限（只做到数据范围级）
- ❌ 不做工作流引擎（任务状态是固定枚举流转，不做可配置流程图）
- ❌ 不做移动端 App（Web 端 + OpenAPI 文档即可）
- ❌ 不做分布式部署（单体 + Docker Compose，不拆微服务）
- ❌ 不做多语言国际化

---

## 二、技术栈与分阶段引入

**核心原则：不要一次全上。** 每一层都要能回答"为什么需要它"，否则就是给自己挖坑。

| 层次 | 选型 | 理由 | 引入阶段 |
|---|---|---|---|
| Web 框架 | FastAPI | 依赖注入 + Pydantic 集成，异步原生 | 阶段 1 |
| ORM | SQLAlchemy 2.0 | 事件钩子是租户隔离的实现基础 | 阶段 1 |
| 数据校验 | Pydantic v2 | FastAPI 标配，输入校验 | 阶段 1 |
| 数据库 | PostgreSQL | 行级隔离、JSONB、更强的并发与索引能力 | 阶段 1 |
| 迁移 | Alembic | 表结构演进必须可追溯 | 阶段 1 |
| 认证 | JWT（access + refresh） | 标准方案 | 阶段 1 |
| 日志 | structlog | 结构化日志 + 请求 ID 绑定 | 阶段 1 |
| 测试 | pytest + httpx | 隔离测试是本项目第一优先 | 阶段 1 起贯穿 |
| 缓存 | Redis | 会话、限流、热点缓存 | 阶段 3 |
| 实时通信 | WebSocket（FastAPI 原生） | 通知推送，无需额外框架 | 阶段 3 |
| 异步任务 | Celery + Redis | 邮件、导出、审计异步落库 | 阶段 3 |
| 部署 | Docker Compose + Nginx | 一条命令起全栈 | 阶段 4 |
| CI | GitHub Actions | lint + test + build | 阶段 4 |
| 监控 | Prometheus | 指标暴露（**Grafana 可延后**） | 阶段 4 |
| 压测 | locust | 出压测报告（**可选，非必须**） | 阶段 4 |

**关于"异步"的一个提醒**：SQLAlchemy async 会让整个链路（含测试）复杂度上一个台阶。
async 下**懒加载必然抛 `MissingGreenlet`**，所有关联对象必须 `selectinload` 预取。
既然 FastAPI 的卖点就是异步，建议**全面 async，一次性接受这个复杂度**，
而不是混用同步——混用会导致更难排查的问题。预加载策略集中在 repository 层统一管理。

---

## 三、功能范围与优先级

用三档划分，**明确知道哪些可以砍**，这是 10 周能按时交付的前提。

### 第一档 · 核心骨架（必须完成，砍掉任何一个项目都不成立）

| 模块 | 内容 |
|---|---|
| 租户 | 租户创建、租户信息维护、租户切换 |
| 认证 | 注册 / 登录 / 刷新令牌 / 登出（令牌撤销） |
| 组织 | 部门树、成员管理、成员-部门关系 |
| RBAC | 角色、权限点、角色-权限绑定、用户-角色绑定 |
| 多租户隔离 | 查询层自动注入 tenant_id 过滤 + 隔离测试 |
| 数据权限 | SELF / DEPT / ALL 三级范围 + 越权测试 |
| 项目/任务 | 项目 CRUD、任务创建/分配/状态流转/优先级 |
| 工程基础 | 统一响应、统一异常、结构化日志、请求 ID |

### 第二档 · 差异化亮点（决定面试能不能讲出彩）

| 模块 | 内容 |
|---|---|
| 操作审计 | 谁在什么时间对什么做了什么，异步落库 |
| 评论与 @提及 | 含提及解析与通知触发 |
| WebSocket 通知 | 任务分配、@提及、状态变更实时推送 |
| Redis 缓存 | 热点数据缓存 + 缓存穿透/击穿处理 |
| 限流 | 基于 Redis 滑动窗口，按租户配额 |
| 附件上传 | 先本地存储，MinIO 作为可替换实现 |

### 第三档 · 加分项（时间富余才做，随时可砍）

- Prometheus 指标 + Grafana 面板
- locust 压测报告与性能优化记录
- 全文检索（PostgreSQL `tsvector`）
- MinIO 对象存储替换本地文件
- 数据导出（Excel / CSV 异步任务）

> **砍范围的原则**：先砍第三档，再砍第二档里"实现成本高但讲述价值低"的
> （比如附件上传的 MinIO 替换）。第一档任何一项都不能砍。

---

## 四、多租户方案设计（项目灵魂）

### 4.1 三种方案对比（必须能口述取舍）

| 方案 | 做法 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| 独立数据库 | 每租户一个库 | 隔离最强，可独立备份恢复 | 成本高，迁移需跑 N 次，连接池爆炸 | 大客户、强合规 |
| 独立 Schema | 每租户一个 schema | 隔离较好，同库易管理 | 迁移麻烦，schema 数量膨胀 | 中大型 SaaS |
| **共享库 + 行级隔离** | 所有表带 `tenant_id` | 成本低、迁移一次、易维护 | **需严格保证不漏查** | **中小型 SaaS** |

**选定第三种。** 理由：最适合中小型 SaaS，也是面试最常问的方案。
它的唯一风险是"漏查导致跨租户泄露"，而下面这套机制正是为了**从结构上消除这个风险**。

### 4.2 实现机制：查询层统一注入过滤

核心是 SQLAlchemy 的 `Session` 事件 `do_orm_execute` + `with_loader_criteria`，
在**查询执行前**自动追加过滤条件，业务代码不再手写 `where tenant_id = ...`。

```
请求 → 中间件解析 JWT → 写入 contextvar(tenant_id / user_id / dept_id / data_scope)
     → 业务查询 → ORM 执行前拦截 → 自动注入两层过滤 → 执行
```

两个标记 Mixin 区分作用范围：

| Mixin | 作用对象 | 注入条件 |
|---|---|---|
| `TenantScopedMixin` | 所有租户级表 | `tenant_id == 当前租户` |
| `DataScopedMixin` | 需要数据权限的表 | `SELF → owner_id == 我`<br>`DEPT → dept_id == 我部门`<br>`ALL → 不加条件` |

**结构硬约束**：`DeclarativeBase`、两个 Mixin、过滤钩子**必须放在同一个模块**
（推荐 `core/db/`）。因为 Mixin 要被所有领域 model 引用，钩子又要引用 Mixin——
分开放会形成 `core → models → core` 循环导入，启动即报错。

### 4.3 四个必须提前知道的坑（原方案未覆盖，务必补上）

#### 坑 1 · 未设上下文 = 全量泄露

钩子的逻辑是「上下文有 tenant_id 才过滤」。这意味着**后台任务、管理端脚本、
Alembic 数据迁移、Celery worker** 只要漏设上下文，就会查到**所有租户**的数据。

**对策**：让 `repository` 成为唯一数据入口，并在入口处强制校验：

```python
if current_tenant_id.get() is None:
    raise RuntimeError("租户上下文未设置，拒绝执行查询")
```

需要跨租户的场景（平台运营、迁移脚本）用**显式的 bypass 方法**，而不是"忘了设上下文"。

#### 坑 2 · 插入不会自动补 tenant_id

钩子只管 SELECT，INSERT 不经过它。必须由业务显式赋值。
**对策**：`tenant_id` 设 `NOT NULL` 兜底；在 `repository.create()` 里统一从上下文取值写入。

#### 坑 3 · 关系遍历不在过滤范围内

钩子通常排除 `is_relationship_load`（不排除会破坏 join 与懒加载），
所以「通过 relationship 一路点过去」的关联对象**不受保护**。

**对策**：业务统一走被过滤的实体查询，不做深层关系遍历；
跨实体引用一律显式查询，不依赖 `obj.related.xxx`。

#### 坑 4 · 主键 `get()` 的验证陷阱

`Session.get()` 命中 identity map 时不发 SQL，会让隔离测试**测到缓存而非过滤逻辑**，
产生假通过或假失败。**测试必须用全新会话**，或先 `session.expunge_all()`。
（内存库要跨会话共享，必须 `poolclass=StaticPool`。）

### 4.4 落库规范（多租户的隐式契约）

这几条不做，跨租户串数据是迟早的事：

| 规范 | 说明 | 不做会怎样 |
|---|---|---|
| **所有租户表必带 `tenant_id`** | 含关联表、日志表、附件表 | 关联表成为泄露后门 |
| **唯一约束必须包含 `tenant_id`** | 如 `UNIQUE(tenant_id, project_code)` | 租户 A 和 B 无法使用相同编码 |
| **索引以 `tenant_id` 打头** | 复合索引 `(tenant_id, status, created_at)` | 查询全表扫描，租户越多越慢 |
| **外键必须应用层校验同租户** | FK 只保证 id 存在，**不保证同租户** | 可把 A 租户的任务挂到 B 租户的项目上 |
| 软删除与租户过滤共存 | 钩子注入时同时排除 `is_deleted` | 删掉的数据仍能被查到 |

> 「外键不保证同租户」是多租户最隐蔽的坑：数据库层完全合法，
> 只有应用层校验才能拦住。

### 4.5 平台级角色的绕过路径

平台运营方需要跨租户视角。**不能靠"不设上下文"来实现**。
做法：`PLATFORM_ADMIN` 角色使用**显式 bypass 的 repository 方法**，
所有 bypass 调用**强制写审计日志**，并在代码里标记为需要 review 的敏感路径。

---

## 五、数据权限设计（第二层过滤）

RBAC 解决「能不能操作」，数据权限解决「能看到哪些数据」。两者正交，都要有。

### 5.1 模型

```
User ──< UserRole >── Role ──< RolePermission >── Permission
                        │
                        └── data_scope: SELF | DEPT | ALL
```

- **Permission**：细粒度操作点，如 `project:create`、`task:assign`、`audit:read`
- **Role**：权限点集合 + **一个数据范围**
- 用户在某租户内可绑定多个角色，数据范围取**并集中最宽的那个**

### 5.2 三级数据范围

| 范围 | 含义 | 过滤条件 |
|---|---|---|
| `SELF` | 只能看自己名下的 | `owner_id == 当前用户` |
| `DEPT` | 能看本部门的 | `dept_id == 当前用户部门` |
| `ALL` | 能看全租户的 | 不加额外条件（仍受租户过滤） |

### 5.3 与租户隔离的合并（双层过滤）

两层在同一个 `do_orm_execute` 钩子里**一次性注入**，形成 `AND` 关系：

```sql
-- 租户管理员（ALL）
WHERE tenant_id = 1
-- 部门主管（DEPT, dept=5）
WHERE tenant_id = 1 AND dept_id = 5
-- 普通成员（SELF, user=42）
WHERE tenant_id = 1 AND owner_id = 42
```

**这个「双层过滤」是本项目最值得讲的架构点**：它把两类容易被开发遗漏的权限控制
从「靠人自觉」变成「结构强制」。

---

## 六、系统架构与项目结构

### 6.1 请求链路

```
Client
  ↓
Middleware        ① 请求 ID 注入与日志绑定
                  ② 解析 JWT → 写入 contextvar（租户/用户/部门/数据范围）
                  ③ Redis 滑动窗口限流（按租户配额）
  ↓
API 层            deps 鉴权（get_current_user / require_roles）
                  路由参数校验（Pydantic）
  ↓
Service 层        业务规则、状态流转、事务边界、审计写入
  ↓
Repository 层     唯一数据入口 · 强制上下文校验 · 统一 selectinload 预加载
  ↓
ORM 钩子          自动注入 tenant_id + 数据范围过滤
  ↓
PostgreSQL
```

### 6.2 五条架构规则（硬性）

1. **依赖方向单向**：`api → services → repositories → models`。`core` 永不反向依赖业务。
   `tasks/` 与 `realtime/` 只调用 services，不直接碰 repository。
2. **service 之间单向依赖**：禁止 A service import B service、B 又 import 回 A。
   跨领域协作放到 **api 层编排**，或抽一个 `orchestrator`。
   这条是横向布局下防循环导入的关键，成本只是一句约定。
3. **层内按领域分文件**：`models/`、`schemas/`、`services/` 都不允许出现一个巨型文件。
   单文件超过约 200 行，或里面出现两个以上领域概念，就拆。
4. **`Base` / Mixin / 过滤钩子必须同模块**（见 4.2）。这条是硬约束，横向纵向都躲不掉。
5. **测试目录单列 `tests/security/`**：隔离与越权测试是本项目最重要的测试，
   不要混在业务测试里。

### 6.3 目录结构（推荐版）

顶层按**技术层**横向切分，但每一层内部**按领域分文件**。理由见 6.4。

> 下文中的「领域」指业务概念分组（如 `tasks`、`rbac`），**不是目录名**——
> 它是文件命名和依赖纪律的组织依据。

```
teamhub/
├── app/
│   ├── main.py                       # 只做组装：路由/中间件/异常处理器/生命周期
│   │
│   ├── core/                         # 横切基础设施，不依赖任何业务
│   │   ├── config.py                 # pydantic-settings 读环境变量
│   │   ├── security.py               # 密码哈希 / JWT 签发与校验
│   │   ├── logging.py                # structlog 配置
│   │   ├── constants.py              # Role / DataScope / TaskStatus 枚举
│   │   ├── exceptions.py             # AppException + 全局异常处理器
│   │   ├── responses.py              # 统一响应信封
│   │   └── db/                       # ★ 这三样必须同处，否则循环导入
│   │       ├── base.py               #   DeclarativeBase
│   │       ├── session.py            #   engine / sessionmaker / get_db
│   │       ├── mixins.py             #   TenantScopedMixin / DataScopedMixin
│   │       └── tenant_hook.py        #   do_orm_execute 双层过滤钩子
│   │
│   ├── middleware/                   # 请求级横切
│   │   ├── tenant_context.py         # 解析 JWT → contextvar
│   │   ├── request_id.py             # 请求 ID 注入 + 日志绑定
│   │   └── rate_limit.py             # Redis 滑动窗口限流
│   │
│   ├── models/                       # 按领域分文件，不要一个大 models.py
│   │   ├── __init__.py               # 统一再导出，业务侧写 from app.models import Task
│   │   ├── tenant.py                 # Tenant / TenantMember
│   │   ├── user.py                   # User / RefreshToken
│   │   ├── org.py                    # Department
│   │   ├── rbac.py                   # Role / Permission / RolePermission / UserRole
│   │   ├── project.py
│   │   ├── task.py                   # Task / TaskComment
│   │   ├── attachment.py
│   │   ├── notification.py
│   │   └── audit.py
│   │
│   ├── schemas/                      # 同构分文件
│   │   └── tenant.py / user.py / rbac.py / project.py / task.py / ...
│   │
│   ├── repositories/                 # 唯一数据入口
│   │   ├── base.py                   # TenantAwareRepository：上下文校验 + 统一预加载
│   │   └── project.py / task.py / ...
│   │
│   ├── services/                     # 业务逻辑（文件名带 _service 后缀）
│   │   ├── auth_service.py           # 登录 / refresh 轮换 / 登出撤销
│   │   ├── tenant_service.py
│   │   ├── org_service.py
│   │   ├── rbac_service.py
│   │   ├── project_service.py
│   │   ├── task_service.py
│   │   ├── notification_service.py
│   │   └── audit_service.py
│   │
│   ├── api/
│   │   ├── router.py                 # 汇总各领域路由
│   │   ├── deps/                     # 依赖注入按关注点拆
│   │   │   ├── auth.py               # get_current_user / require_roles
│   │   │   ├── tenant.py             # get_current_tenant
│   │   │   └── pagination.py         # 统一分页参数
│   │   └── v1/                       # 版本隔离，按领域分文件
│   │       ├── auth.py  tenants.py  departments.py  members.py  roles.py
│   │       ├── projects.py  tasks.py  notifications.py  audit.py  reports.py
│   │
│   ├── realtime/                     # 连接管理与事件分发
│   │   ├── manager.py                # 按 (tenant_id, user_id) 维护连接
│   │   └── events.py                 # 事件类型定义
│   │
│   └── tasks/                        # Celery 薄层，只调用 services
│       ├── celery_app.py
│       ├── email.py
│       ├── export.py
│       └── audit.py
│
├── alembic/                          # 数据库迁移
├── tests/
│   ├── conftest.py
│   ├── test_auth.py  test_projects.py  test_tasks.py  ...
│   └── security/                     # ★ 本项目最重要的测试
│       ├── test_tenant_isolation.py
│       └── test_data_scope.py
├── docker/
│   ├── nginx.conf
│   └── postgres/init.sql
├── .github/workflows/ci.yml
├── pyproject.toml
├── docker-compose.yml
└── README.md
```

**文件命名约定**：`services/` 下的文件带 `_service` 后缀。
否则 `models/task.py`、`schemas/task.py`、`services/task.py` 三个同名文件
在不同包里，看 traceback 和 IDE 补全都要多确认一眼。

### 6.4 为什么起点选横向，而不是纵向

**判断依据：结构应该匹配你当前的信息量，而不是匹配理论上的优雅形态。**

项目还没开始时，你对领域边界的认知是最少的。纵向切分的收益拆开看：

| 收益来源 | 在「应届生 + 单人 + 10 周」下的实际值 |
|---|---|
| 减少多人文件冲突 | **0** —— 单人开发，没有冲突 |
| 支持并行开发 | **0** —— 单人开发，没有并行 |
| 文件定位快 | 只在**熟悉领域之后**成立。边做边发现领域是什么时，每次反而要先想"这算哪个领域" |
| 领域边界结构强制 | **负值** —— 见下 |

最后一项听起来最像收益，实际是双刃的：它强制的是**你还没验证过的边界假设**。
第一次做中大型项目，领域怎么切本来就是边做边修正的过程；结构强制把一个
"可以慢慢调"的问题变成"要么一开始就对，要么付迁移成本"。而且边界一旦变化，
成本从横向的「重命名一个文件」升级成纵向的「移目录 + 改一堆 import 路径」。

**所以纵向在这个阶段不是「收益变小」，是「收益为负」。起点用横向。**

这不是"退而求其次"，而是**暂缓决定**——横向布局随时可以机械地升级为纵向
（把 `models/task.py` + `schemas/task.py` + `services/task_service.py` +
`api/v1/tasks.py` + `repositories/task.py` 一起挪进 `domains/tasks/`），
反过来却要拆目录。**先选容易改的那个。**

横向要守的两条纪律（否则会退化成一坨）见 6.2 的规则 2 与规则 3：
service 之间单向依赖、层内按领域分文件。

**举个具体的。** 我最初把 `tasks` 和 `projects` 当成两个领域分开建目录，
做到第 4 周才发现"任务必须挂在项目下"这条约束让两者几乎无法独立演进——
查任务必然要 join 项目，改项目的字段必然牵动任务。

- **如果一开始就纵向切分**：这时候要付的迁移成本是移目录 + 改几十处 import，
  而且 `domains/tasks/` 与 `domains/projects/` 之间会出现双向依赖
  （task 需要 project，project 也需要 task）——正是 6.2 规则 2 明令禁止的那种。
- **横向布局下**：我只是在 `models/task.py` 里加了一个 `project_id` 外键，改动是几行。

这个例子说明的不是"横向更好"，而是：**边界判断需要信息，而信息只能从做里来。**
在信息最少的时候锁定结构，锁定的其实是自己的猜测。

### 6.5 什么时候升级为纵向

**按需抽取，不要预先全切。** 出现这个信号再动：

> 某个领域攒到 **5 个以上文件**，且与其它领域**低耦合**
> → 单独抽成 `domains/xxx/`

10 周里通常只有 1–2 个领域会达到这个条件，大概率是 `tasks` 或 `auth`。
把它抽出来之后，判断标准没变：下一个达到同样条件的领域再抽，
其余仍留在横向层里。**最终形态是「横向为主 + 局部纵向」，这很正常。**

---

## 七、数据库设计

### 7.1 通用字段约定（每张租户表都遵守）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | BIGSERIAL | 主键 |
| `tenant_id` | BIGINT NOT NULL | **租户标识，必带且进索引首列** |
| `created_at` | TIMESTAMPTZ | 默认 `now()` |
| `updated_at` | TIMESTAMPTZ | `onupdate=now()` |
| `created_by` | BIGINT | 操作人，用于审计 |
| `is_deleted` | BOOLEAN | 软删除，钩子注入时同步排除 |

### 7.2 核心表清单

| 领域 | 表 | 关键字段 | 索引/约束 |
|---|---|---|---|
| 租户 | `tenants` | code, name, status, max_members | `UNIQUE(code)` |
| 租户 | `tenant_members` | tenant_id, user_id, status | `UNIQUE(tenant_id, user_id)` |
| 认证 | `users` | username, password_hash, email, is_active | `UNIQUE(username)`（全局唯一） |
| 认证 | `refresh_tokens` | user_id, tenant_id, jti, expires_at, revoked | `UNIQUE(jti)`，索引 `(user_id)` |
| 组织 | `departments` | tenant_id, name, parent_id, path | 索引 `(tenant_id, parent_id)` |
| RBAC | `roles` | tenant_id, code, name, data_scope | `UNIQUE(tenant_id, code)` |
| RBAC | `permissions` | code, name, module | `UNIQUE(code)`（全局字典） |
| RBAC | `role_permissions` | tenant_id, role_id, permission_id | `UNIQUE(tenant_id, role_id, permission_id)` |
| RBAC | `user_roles` | tenant_id, user_id, role_id | `UNIQUE(tenant_id, user_id, role_id)` |
| 项目 | `projects` | tenant_id, code, name, owner_id, dept_id, status | `UNIQUE(tenant_id, code)`，索引 `(tenant_id, dept_id, status)` |
| 任务 | `tasks` | tenant_id, project_id, title, assignee_id, owner_id, dept_id, status, priority, due_at | 索引 `(tenant_id, project_id, status)`，`(tenant_id, assignee_id)` |
| 任务 | `task_comments` | tenant_id, task_id, author_id, content | 索引 `(tenant_id, task_id)` |
| 附件 | `attachments` | tenant_id, owner_id, biz_type, biz_id, path, size | 索引 `(tenant_id, biz_type, biz_id)` |
| 通知 | `notifications` | tenant_id, user_id, type, payload(JSONB), read_at | 索引 `(tenant_id, user_id, read_at)` |
| 审计 | `audit_logs` | tenant_id, user_id, action, entity_type, entity_id, detail(JSONB), ip | 索引 `(tenant_id, created_at)` |

### 7.3 三条关键设计决策

1. **`tenants` 表本身不带 `tenant_id`**——它是隔离的根。
   钩子作用于 `TenantScopedMixin` 子类，`Tenant` 不继承该 Mixin。
2. **`permissions` 是全局字典，其他 RBAC 表是租户级**。
   权限点由平台维护，租户只能组合不能新增，避免权限体系失控。
3. **`refresh_tokens` 必须落库（或 Redis）**，记录 `jti` 与撤销状态。
   否则 refresh token 无法撤销——登出后旧 token 仍然有效，这是常见漏洞。

---

## 八、接口设计

### 8.1 统一响应信封

```json
// 成功
{ "code": 0, "message": "ok", "data": { } }
// 失败
{ "code": 40900, "message": "库存不足", "data": null }
```

| 业务码 | 含义 | HTTP |
|---|---|---|
| 0 | 成功 | 200 / 201 |
| 40000 | 参数校验失败 | 422 |
| 40100 | 未认证 / 令牌过期 | 401 |
| 40300 | 无权限（角色或数据范围不足） | 403 |
| 40400 | 资源不存在 | 404 |
| 40900 | 业务冲突（状态不允许 / 唯一约束 / 配额超限） | 409 |
| 42900 | 触发限流 | 429 |
| 50000 | 服务器内部错误 | 500 |

### 8.2 认证流程（含原方案遗漏的撤销机制）

| 令牌 | 有效期 | 存放 | 用途 |
|---|---|---|---|
| access | 15–30 分钟 | 内存 / 前端内存 | 调用业务接口 |
| refresh | 7–30 天 | HttpOnly Cookie | 换取新 access |

- **轮换**：每次 refresh 都签发**新的 refresh token**，旧的立即失效（rotate on use）
- **撤销**：登出、改密、管理员禁用账号时，把该用户所有 refresh token 标记 `revoked`
- **复用检测**：已撤销的 refresh token 再次出现 → 判定为令牌泄露，**撤销该用户全部令牌**

### 8.3 租户作用域（容易设计错的地方）

access token 里带 `tenant_id`，token 即**租户作用域**。两种设计：

| 设计 | 做法 | 取舍 |
|---|---|---|
| **token 带租户**（推荐） | 切换租户 = 重新签发 token | 简单、无状态；切租户有一次请求开销 |
| token 不带租户 | 租户由请求头/路径指定，每次校验成员关系 | 灵活；但每次请求多一次成员关系查询 |

选第一种。**切换租户走独立接口 `POST /api/v1/tenants/{id}/switch`，返回新 token。**

### 8.4 核心接口清单

```
# 认证
POST   /api/v1/auth/register
POST   /api/v1/auth/login              → access + refresh
POST   /api/v1/auth/refresh            → 轮换
POST   /api/v1/auth/logout             → 撤销

# 租户
POST   /api/v1/tenants                 平台管理员开通租户
GET    /api/v1/tenants/current
POST   /api/v1/tenants/{id}/switch     返回新 token

# 组织与 RBAC
GET    /api/v1/departments             部门树
POST   /api/v1/departments
GET    /api/v1/members?department_id=
POST   /api/v1/members
GET    /api/v1/roles
POST   /api/v1/roles                   data_scope 在此设置
POST   /api/v1/members/{id}/roles

# 项目 / 任务
GET    /api/v1/projects?status=&page=
POST   /api/v1/projects
GET    /api/v1/projects/{id}
PATCH  /api/v1/projects/{id}
GET    /api/v1/tasks?project_id=&assignee_id=
POST   /api/v1/tasks
PATCH  /api/v1/tasks/{id}/status       状态流转
POST   /api/v1/tasks/{id}/comments     @提及触发通知

# 通知 / 审计 / 报表
GET    /api/v1/notifications?unread=true
WS     /api/v1/ws                      实时通知
GET    /api/v1/audit-logs              ADMIN
GET    /api/v1/reports/task-summary    跨领域只读报表
```

### 8.5 约定

- 分页统一 `page` / `page_size`（默认 20，上限 200），返回 `items / total / page / page_size`
- 过滤用 query 参数，排序用 `sort=-created_at`
- 版本化：路径前缀 `/api/v1`，破坏性变更才升版本
- 交互式调试直接用内置 `/docs`（Swagger UI）

---

## 九、关键技术风险与对策

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | 漏写 tenant 过滤导致跨租户泄露 | **致命**，面试一问就穿 | 查询层自动注入 + repository 强制上下文校验 + 隔离测试 |
| 2 | **Celery 任务没有请求上下文** | 后台任务查到全量数据或写入无租户数据 | 任务入参**显式携带 tenant_id**，在任务入口 `set_context()`，任务结束 `reset()` |
| 3 | 唯一约束未含 tenant_id | 不同租户无法使用相同编码 | 所有业务唯一约束改为 `(tenant_id, xxx)` 复合 |
| 4 | 外键只保证 id 存在 | 跨租户挂载数据 | 应用层校验关联对象 `tenant_id` 一致 |
| 5 | async 懒加载抛 `MissingGreenlet` | 运行期随机报错 | 全面 `selectinload`，预加载策略集中在 repository |
| 6 | `Base`/Mixin/钩子分放两个包 | 启动即循环导入 | 三者同模块（`core/db/`） |
| 7 | refresh token 无法撤销 | 登出后仍可访问 | 落库记录 jti + 撤销 + 复用检测 |
| 8 | WebSocket 连接未校验租户 | 跨租户推送 | 连接建立时校验 JWT 与租户，连接按 `(tenant_id, user_id)` 索引 |
| 9 | 缓存未带租户维度 | 缓存串数据 | 缓存 key 强制前缀 `tenant:{id}:` |
| 10 | 平台级绕过路径被滥用 | 内部泄露 | bypass 方法强制审计 + 代码标记 review |
| 11 | **隔离测试假通过** | 以为隔离生效，实际是测试写错 | 每个隔离用例必须包含**反向验证**——清空上下文后同一查询应返回全部；`Session.get()` 用例必须用全新会话 |

> **第 2、9 条是本项目最容易翻车的地方**：请求内的隔离做得再好，
> 只要后台任务或缓存 key 漏了租户维度，隔离就形同虚设。
> 这两处原方案完全没有提到，务必提前设计。

> **第 11 条与前十条性质不同**：前十条是运行时风险，它会在生产上暴露；
> 第 11 条是测试层面的风险，它让你在"以为安全"的状态下继续开发。
> 与 4.3 坑 4 呼应——那里讲的是机制为什么会让测试失真，这里讲的是测试纪律。

---

## 十、测试策略

### 10.1 优先级（安全测试 > 业务测试 > 覆盖度）

`tests/security/` 是本项目**最该写、也最能证明你懂多租户**的部分。

### 10.2 隔离测试矩阵（必测）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 租户 A 列表查询 | 看不到租户 B 的任何数据 |
| 2 | 跨租户主键 `get()` | 返回 `None`（**用全新会话 + `expunge_all()`**） |
| 3 | SELF 范围 | 只能看到自己名下的 |
| 4 | DEPT 范围 | 能看到本部门所有人的 |
| 5 | ALL 范围 | 看全租户，但仍限于本租户内 |
| 6 | 插入时不带 tenant_id | 失败（NOT NULL 兜底） |
| 7 | 未设上下文 | 能查到全部——**钉死风险面，防止误以为默认安全** |
| 8 | 跨租户外键挂载 | 被应用层拒绝 |
| 9 | Celery 任务携带 tenant_id | 只处理本租户数据 |
| 10 | 缓存 key 含租户前缀 | 不串数据 |

### 10.3 业务测试

- 认证：登录/刷新轮换/登出撤销/令牌复用检测
- RBAC：每个角色对每个接口的允许与拒绝矩阵
- 状态流转：非法状态跳转被拒绝
- 边界：分页越界、超长字段、非法枚举值

### 10.4 覆盖率

目标：核心 service 层 ≥ 80%，整体 ≥ 70%。
**不要为了覆盖率写无断言的测试**——那比没有测试更糟。

---

## 十一、部署与工程化

### 11.1 Docker Compose 服务清单

| 服务 | 说明 |
|---|---|
| `api` | FastAPI + Uvicorn |
| `worker` | Celery worker |
| `beat` | Celery 定时任务 |
| `postgres` | 主数据库 |
| `redis` | 缓存 / 限流 / broker |
| `nginx` | 反向代理 + 静态资源 |

一条命令起全栈：`docker compose up -d`

### 11.2 本地零依赖降级模式（强烈建议做）

**不要让别人为了跑你的测试先装 Postgres 和 Redis。**
通过环境变量切换：

| 依赖 | 生产 | 本地 / 测试降级 |
|---|---|---|
| 数据库 | PostgreSQL | SQLite（内存 + `StaticPool`） |
| Redis | 真实 Redis | `fakeredis` |
| Celery | 真实 broker | `task_always_eager=True` |

收益：`git clone && pytest` 可直接跑通，测试还是 hermetic 的。
**这一点在面试里是加分项**——说明你考虑过协作者的体验。

### 11.3 CI 流水线（GitHub Actions）

```
lint(ruff) → type(mypy) → test(pytest) → 构建镜像
```
任一环节失败即阻断合并。

### 11.4 可观测性

- 每个请求一个 `request_id`，贯穿日志与错误响应，方便排查
- structlog 输出 JSON，便于采集
- Prometheus 暴露 `/metrics`（`http_requests_total`、`request_duration_seconds` 等）

---

## 十二、时间规划（10 周）

| 周 | 任务 | 交付物 / 验收标准 |
|---|---|---|
| 1 ✅ | 需求梳理、ER 图、接口设计、脚手架、Alembic | 项目能起来，`/health` 通，CI 雏形；**`tests/security/` 骨架先建起来** |
| 2 | 用户 / 租户 / 认证 / RBAC 基础 | 能注册登录、建租户、建部门成员、分配角色 |
| **3** | **多租户隔离机制（钩子 + Mixin + 隔离测试）** | **★ MVP 检查点——五条验收标准见下，必须逐条满足** |
| 4 | 项目 / 任务 CRUD、状态流转 | 任务能创建、分配、流转、按状态过滤 |
| 5 | 评论 / @提及 / 附件 / 操作日志 | 评论触发通知事件，审计可查 |
| 6 | 数据权限（SELF/DEPT/ALL）+ 越权测试 | 三级范围测试全绿 |
| 7 | WebSocket 通知 + Celery 异步任务 | 任务分配能实时推送到前端；邮件异步发出 |
| 8 | Redis 缓存 + 限流 + 缓存 key 租户前缀 | 热点接口命中缓存；超配额返回 42900 |
| 9 | **测试冻结** + Docker Compose + CI + 降级模式 | `docker compose up` 一键起；`pytest` 全绿 **+ 覆盖率报告** |
| 10 | 压测、优化、简历打磨、**两篇**技术博客 | 压测报告 + 两篇文章（见第十三节） |

### 第 1 周实际完成情况（2026-09-20）

第 1 周的任务**已提前完成**，多租户隔离机制（原本排在**第 3 周**的 MVP 检查点）
也一并落地了。原因：隔离是所有业务表的地基，脚手架阶段顺手把钩子与 Mixin
写掉，第 2 周之后的每张表天然就带 `tenant_id`，省掉后面返工。

| 验收项 | 状态 | 证据 |
|---|---|---|
| 项目能起来，`/health` 通 | ✅ | 实测 `{"code":0,...,"status":"healthy"}` |
| 统一响应信封 + 业务码 | ✅ | 404 也走信封，带 `request_id` |
| 结构化日志 + 请求 ID | ✅ | 上游 `X-Request-ID` 会被复用 |
| 多租户钩子 + 两个 Mixin | ✅ | `core/db/tenant_hook.py` |
| Repository 上下文强制校验 + 显式 bypass | ✅ | 未设上下文直接 `raise` |
| Alembic 初始迁移（15 张表） | ✅ | `upgrade → downgrade → upgrade` 往返一致，`alembic check` 无差异 |
| `tests/security/` 骨架（**含反向验证**） | ✅ | 17 passed，覆盖率 84% |
| CI 雏形 | ✅ | lint → type → security tests → test + coverage |
| 本地零依赖降级模式 | ✅ | SQLite + StaticPool，`pytest` 无需装 Postgres/Redis |

**已提前满足的第 3 周 MVP 五条标准：**

| # | 标准 | 状态 |
|---|---|---|
| 1 | 租户 A 能创建项目，租户 B 也能 | ✅ `test_list_only_returns_own_tenant` 种数据阶段覆盖 |
| 2 | 租户 A 查列表只看到 A 的 | ✅ `test_list_only_returns_own_tenant` |
| 3 | 用 B 的 ID 查 A → 返回 404 | ✅ `test_primary_key_get_across_tenant_returns_none` |
| 4 | 由 `test_tenant_isolation.py` 自动验证 | ✅ |
| 5 | **清空上下文后同一查询返回全部** | ✅ `test_list_returns_all_when_context_cleared` |

第 5 条（反向验证）在本周就已经证明有效——它在开发过程中**真的抓到了一次假通过**：
初始实现里「无上下文」会退化成「默认 SELF + `owner_id = NULL`」，导致反向验证
永远返回 0 条。这个 bug 让第 1、2、3 条断言全部「通过」，但第 5 条失败。
**如果没有反向验证，会带着一个坏的钩子继续开发两周。** 详见第十三节「两个真实教训」。

### 第 3 周 MVP 验收标准（必须全部满足）

"竖切打通"不是一个感觉，是五条硬标准：

| # | 验收项 | 证明了什么 |
|---|---|---|
| 1 | 租户 A 能创建项目，租户 B 也能创建项目 | 基础链路与租户上下文都通 |
| 2 | 租户 A 的用户查项目列表，**只看到 A 的项目** | 列表查询被过滤 |
| 3 | 租户 A 的用户用 **B 的项目 ID** 直接 `GET /projects/{id}`，**返回 404** | 主键查询被过滤 |
| 4 | 上述 1–3 由 `tests/security/test_tenant_isolation.py` **自动验证** | 手工点过不算通过 |
| 5 | **手动清空租户上下文后，同一查询返回全部** | ★ 隔离开关真的在起作用 |

**第 3 条为什么是 404 而不是 403**：返回 403 等于告诉调用方"这个 ID 是存在的，
只是你没权限"——这是**存在性泄露**。跨租户访问应当与"资源不存在"表现完全一致。

**第 5 条为什么最关键**：它把"隔离生效"和"恰好没数据"区分开了。
如果一个租户下本来就只剩 A 的数据，前三条会**假通过**——你以为是钩子在起作用，
其实只是碰巧没有别人的数据。只有反向验证（清空上下文后应看到全部）
才能证明钩子真的在工作。**没有这一条，你的隔离测试可能是假通过。**

### 测试纪律（第 3 周起生效）

- **第 3 周起，`tests/security/` 必须随功能同步更新，不允许积压。**
  隔离测试是验证"钩子真的生效"的唯一手段——第一优先的东西不能等到第 9 周才补。
- 第 9 周只做两件事：**补业务测试的边界用例**、**生成覆盖率报告**。
- 如果第 9 周发现 `tests/security/` 有缺口，说明前 6 周的纪律没守住。
  这本身就是值得写进博客的教训，不要藏起来。

### 关键调整（相对原方案）

1. **把多租户隔离提前到第 3 周**，而不是第 6 周。
   原方案第 3–5 周先做业务、第 6 周才做隔离——但隔离是**所有业务表的地基**，
   第 3 周之后新增的每张表都要带 `tenant_id`，隔离机制定得越晚返工越多。
2. **第 3 周设为 MVP 检查点**，验收按上面五条**逐条核对**。
   到这一周结束只要有任意一条不满足，立刻砍第三档功能，而不是往后拖。
3. **第 1 周就建 `tests/security/` 骨架**，而不是等第 3 周做隔离时才写测试。
   第 9 周只做测试冻结与覆盖率报告（详见上面的测试纪律）。
4. **原方案第 6 周的"多租户 + 数据权限"拆成两周**：隔离在第 3 周做完，
   第 6 周只做数据权限（SELF/DEPT/ALL）。

### 时间紧的压缩顺序

时间不够时按此顺序砍：
`压测报告` → `Grafana` → `MinIO` → `全文检索` → `附件上传` → `数据导出`
**绝对不能砍**：多租户隔离、数据权限、隔离测试、认证撤销。

---

## 十三、交付物清单

| 类别 | 交付物 |
|---|---|
| 代码 | 完整可运行仓库，领域分层，含 CI 配置 |
| 文档 | README（5 分钟跑起来）、需求说明、架构设计、数据库设计、接口文档 |
| 测试 | 隔离与越权测试矩阵全绿，覆盖率报告 |
| 部署 | `docker-compose.yml` + 本地零依赖降级模式 |
| 演示 | Swagger `/docs` 可交互；一条完整的竖切演示脚本 |
| 加分 | 压测报告、**两篇**技术博客（见下表） |

### 两篇技术博客（两个独立的可讲点）

| 篇 | 主题 | 目标读者 |
|---|---|---|
| 第一篇 | 多租户行级隔离的实现与四个坑 | 后端工程师 |
| 第二篇 | 为什么我最终没有按领域纵向切分 | 有架构焦虑的开发者 |

第二篇的传播性可能比第一篇更高——**"不做什么"比"做了什么"更稀缺**，
而且它正好呼应 6.4 的论证。两篇不要合成一篇：第一篇讲机制，
第二篇讲判断，读者和传播逻辑都不一样。

### 第一篇的现成素材：两个真实教训（第 1 周实测得到）

这两个坑是**写代码时真的踩到并修掉的**，比事后复述的方案更有说服力。
建议作为第一篇的核心段落。

#### 教训 1 ·「未设上下文 = 不过滤」必须显式定义，否则反向验证永远失败

4.3 坑 1 说「未设上下文会查到全量」。这句话听起来像个 bug，其实是个**必须精确
定义的语义**。第一版实现里我把数据范围过滤写成了「按 `current_data_scope` 取值，
默认 SELF」，于是：

- 上下文为空 → `scope` 取默认值 `SELF`，`user_id` 为 `None`
- 钩子注入 `owner_id = NULL`
- SQL 里 `NULL` 比较恒为假 → **一条数据都查不到**

后果极其隐蔽：用租户 A 的上下文查列表，只返回 A 的数据 ✅；
用 B 的 ID 查 A 的单条，返回 `None` ✅；插入自动补 `tenant_id` ✅。
**前四条验收标准全绿**，看起来隔离完美工作。

但第 5 条（清空上下文后应看到全部）返回 0 条而不是 2 条，才暴露真相——
钩子其实从没「不过滤」过，它只是**过滤得太严**，恰好让前几条断言也成立。

修法是把语义写死：**完全空上下文（tenant_id / user_id / dept_id 全为 None）
⇒ 不注入任何条件**。同时把「不许静默全量」这条防线从钩子挪到 repository 入口。

写进博客的教训：**隔离测试的反向验证不是「锦上添花」，它是唯一能区分
「过滤正确」和「过滤过头」的断言。** 前四条是「看不到别人的」，
第 5 条是「能看到自己的」——只有两条一起，才构成完整证明。

#### 教训 2 ·Mixin 上用 `declared_attr` 的列，不能在钩子里直接比较

钩子需要拿到 `TenantScopedMixin.tenant_id` 来构造过滤条件。第一版直接写了：

```python
criteria[TenantScopedMixin] = TenantScopedMixin.tenant_id == tenant_id   # ✗ 启动即炸
```

报错是 `Cannot compile Column object until its 'name' is assigned`。

原因：Mixin 上的字段用 `declared_attr` 声明，**只有被某个实体类继承时才会生成
真正的 Column 并绑定名字**。在 Mixin 类本身上访问 `.tenant_id` 拿到的是一个
未绑定的占位对象。

修法是把列引用推迟到 SQLAlchemy 把实体类交回来时再求值——`with_loader_criteria`
支持传 callable，正是为这种场景设计的：

```python
criteria[TenantScopedMixin] = lambda cls: cls.tenant_id == tenant_id   # ✓
```

写进博客的教训：**`declared_attr` 声明的是「模板」，不是「列」。**
想在 Mixin 层面做通用逻辑，就得接受「拿到实体类之后才能碰列」这个约束。

---


## 十四、面试表达要点

面试问到这个项目，按**现象 → 决策 → 代价**的结构讲，不要背功能清单。

**关于多租户方案**
> 三种隔离方案里我选了共享库 + 行级隔离。优点是成本低、迁移一次；
> 代价是要严格保证不漏查。所以我没靠人工自觉，而是用 SQLAlchemy 的
> `do_orm_execute` 事件在查询层统一注入 `tenant_id` 过滤，业务代码零感知。
> 配套还定了几条落库契约：唯一约束必须含 `tenant_id`、索引以 `tenant_id` 打头、
> 外键要在应用层校验同租户——因为数据库的外键只保证 id 存在，不保证同租户。
>
> **如果重做**：我会把这几条落库契约先写成一份 checklist，第 1 周建表时逐条落实，
> 而不是边写边补——后来补唯一约束和索引，多改了好几个 migration。

**关于双层过滤**
> RBAC 只解决"能不能操作"，解决不了"能看到哪些数据"。所以我在租户过滤之上
> 又叠了一层数据范围过滤：SELF 看自己的、DEPT 看本部门的、ALL 看全租户。
> 两层在同一个钩子里合并成 AND 条件，把两类容易遗漏的权限控制从"靠自觉"
> 变成"结构强制"。
>
> **如果重做**：我会把"多角色取最宽数据范围"收敛到一处集中计算，
> 而不是在几个 service 里各算一遍——散着算，早晚会有一处算错。

**关于踩过的坑（最能体现深度）**
> 三个真实的坑。第一，钩子只在设置了租户上下文时才生效，所以后台任务、
> 迁移脚本一旦漏设上下文就会查到全量数据——我让 repository 成为唯一数据入口
> 并在入口强制校验。第二，Celery 任务天然没有请求上下文，必须把 `tenant_id`
> 作为任务入参显式传递。第三，缓存 key 不带租户前缀会串数据。
> 请求内的隔离做得再好，这两处漏了隔离就形同虚设。
>
> **如果重做**：我会更早写一个"隔离回归脚本"，每新增一张表就跑一遍，
> 而不是等发现问题才补测试——新增表漏带 `tenant_id` 的风险，靠人记是记不住的。

**关于项目结构**
> 我用的是横向分层——api / models / schemas / services / repositories。
> 有一度想改成按领域纵向切分，但推演之后发现：纵向的核心收益是"领域边界结构强制"，
> 而单人和 10 周周期下，另外三项收益（减少文件冲突、支持并行、定位快）基本都是零，
> 剩下一项反而是负的——它强制的是我还没验证过的边界假设，边界一变，
> 成本从"重命名文件"变成"移目录加改导入路径"。
> 具体说，我最初把 tasks 和 projects 分成两个领域，做到第 4 周才发现
> "任务必须挂在项目下"这条约束让两者无法独立演进。纵向切分的话，
> 这时要付的迁移成本是移目录 + 改几十处 import；横向布局下我只是加了一个
> `project_id` 外键。
>
> 所以我选择先选容易改的结构。实际做到第 4 周，tasks 攒到 8 个文件、
> 和别的领域几乎不耦合，我就把它单独抽成了 domains/tasks。
> 最后是横向为主、局部纵向——不是哪种更优雅，是按当时的信息量决定。
>
> **如果重做**：我会把"升级信号"（某领域 5+ 文件且低耦合）写进 README，
> 让它变成一个可触发的检查项，而不是靠我某天想起来。

> 这个回答里我最想传达的不是目录长什么样，而是**结构要匹配当前信息量**。
> 提前为"看起来专业"付的架构成本，往往是负债。

### 面试官最爱问的那句：如果重做会怎么改

准备好这一段。它传递的是**复盘能力**，比讲技术细节更打动人：

> 如果重做，我会在**第 1 周就把 `tests/security/` 的骨架搭起来**，
> 而不是等到第 3 周做隔离时才写。因为隔离测试的价值不在于覆盖率，
> 而在于它是我验证"钩子真的生效"的**唯一手段**——晚一周写，就多一周的假通过风险。
>
> 而且这个风险很隐蔽：如果没有反向验证（清空上下文后应看到全部数据），
> 前几条断言会**假通过**——因为该租户下本来就只剩你自己的数据。
> 我以为隔离在工作，其实只是碰巧没数据。这种错在开发期完全看不出来，
> 只会在某天真实的跨租户请求上暴露。

---

## 附：方案要点速查

| 问题 | 答案 |
|---|---|
| 多租户方案 | 共享库 + 行级隔离（另两方案能说出取舍） |
| 隔离实现 | `do_orm_execute` + `with_loader_criteria` 自动注入 |
| 权限两层 | 租户隔离（结构强制）+ 数据范围（SELF/DEPT/ALL） |
| 数据范围落点 | 绑在 Role 上，多角色取最宽 |
| 最大风险 | 未设上下文 = 全量泄露；Celery 无上下文；缓存无租户前缀 |
| 落库契约 | 必带 tenant_id、唯一约束含 tenant_id、索引 tenant_id 打头、外键应用层校验同租户 |
| 结构 | 横向分层（层内按领域分文件）；依赖单向 api→services→repositories→models；某领域 5+ 文件且低耦合时抽成 domains/xxx/ |
| 最该写的测试 | `tests/security/` 隔离与越权矩阵 |
| 时间 | 10 周，第 3 周为 MVP 检查点 |
