# Shakedown

> 软件测试 Agent 平台 —— **真实测出被测项目的 bug**，不追求用例数量。
>
> 取名自「新船试航」：先在沙箱里试水，再下水远航。

Shakedown 把「写测试」这件事拆成三段：

| 角色 | 对应组件 | 职责 |
|---|---|---|
| 规划者 | **skipper**（Agent 模块） | 读知识源 → 抽功能点 → 按维度生成用例 → 失败归因 → 白盒走查 |
| 船长 | Shakedown 平台（后端 + 看板） | 项目 / 知识源 / 用例 / 执行 / 缺陷 的编排与留痕 |
| 水手舱 | **berth**（沙箱） | 隔离执行用例（overlayfs / cgroup / 网络白名单） |

核心约束（贯穿全项目的设计红线）：

1. **断言必须有依据** —— 每条预期都能追溯到文档章节 / spec 条目 / 代码位置；没依据的标「待确认」，**不判失败**。
2. **项目完全隔离** —— 独立被测库实例、测试账号、环境地址、端口，互不干扰。
3. **正常流优先** —— 每个功能点至少一条正常流用例，断言要验证业务结果（状态变化、金额、数据落库），不能只看状态码。
4. **数据库为准** —— 用例真身在数据库；可执行代码由数据库生成，不手改代码。
5. **不可信代码** —— Agent 生成的任何代码都在沙箱里跑。

---

## 目录

- [快速开始](#快速开始)
- [端到端演示](#端到端演示)
- [功能一览](#功能一览)
- [架构](#架构)
- [目录结构](#目录结构)
- [配置](#配置)
- [测试](#测试)
- [文档](#文档)
- [许可](#许可)

---

## 快速开始

### 方式一：docker compose（推荐）

```bash
cp .env.example .env          # 按需修改 LLM 配置
docker compose up -d --build
# 前端 http://localhost:5173   后端 http://localhost:8000/docs
```

compose 会启动：PostgreSQL 16 + pgvector、后端（自动跑 alembic 迁移）、前端（nginx 反代 `/api`）。

### 方式二：本地开发

```bash
# 1. 依赖
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
brew install postgresql@18 pgvector          # 或用 docker 起一个 pgvector/pgvector:pg16
createdb shakedown && psql shakedown -c 'CREATE EXTENSION vector;'

# 2. 配置
cp .env.example .env                          # 默认 LLM_PROVIDER=mock，离线可跑

# 3. 迁移 + 启动
cd backend && alembic upgrade head && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev     # http://localhost:5173
```

> `SHAKEDOWN_LLM_PROVIDER=mock` 时不需要任何外部模型：内置的离线 provider 会用确定性规则完成
> 功能点抽取、用例生成、失败归因、白盒走查，用来打通链路与做回归基线。
> 接真实模型只需把 `LLM_PROVIDER` 改成 `openai` 并填 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`。

---

## 端到端演示

仓库自带一个**故意留了两个 bug** 的演示项目（订单负数金额、管理接口缺鉴权），
一条命令跑完「建项目 → 导知识源 → 抽功能点 → 生成用例 → 人工审核 → 执行 → 归因 → 报告」：

```bash
.venv/bin/python scripts/demo.py
```

输出（节选）：

```
▶ 导入知识源
  ✓ 导入 接口文档（Markdown）: 8 块
  ✓ 导入 Swagger 2.0: 5 接口
  ✓ 导入 OpenAPI 3.0: 6 接口
▶ Agent 抽取功能点
  ✓ 功能点 8 个（未映射 1 个）
▶ Agent 生成用例（功能 / 边界 / 安全 / 性能）
  ✓ 用例 30 条
▶ 执行
  ✓ 执行完成：{'total': 30, 'passed': 24, 'failed': 6, ...}
▶ 疑似缺陷 6 条：
  - [high] [疑似缺陷] [安全] api GET /api/admin/stats - 未鉴权访问
  - [low]  [疑似缺陷] [边界] api POST /api/orders - amount 负数
  ...
▶ 报告已导出：var/demo-report.html
```

配套材料：

- `examples/demo-project/`：被测代码 + Markdown 文档 + Swagger 2.0 + OpenAPI 3.0
- `examples/demo-target/server.py`：真实可跑的「被测系统」（带两个 bug）
- `examples/backtest-repo/`：回测用的历史 bug 仓库（含一次真实修复提交）

---

## 功能一览

| Phase | 能力 | 落地位置 |
|---|---|---|
| 0 | 工程骨架、配置、日志、alembic 迁移、LLM provider 抽象、DB 驱动的 worker 状态机 | `backend/app/{config,logging,db,models,agent/providers,worker}` |
| 1 | 项目 CRUD、知识源导入（代码 / Markdown / OpenAPI 2.0 & 3.x）、chunk + pgvector、接口清单、spec diff、三方一致性对照 | `backend/app/ingest/*` |
| 2 | 功能点抽取、四维度用例生成（功能 / 边界 / 安全 / 性能）、统一用例 schema + 校验、待审核队列、覆盖树、人工反馈 | `backend/app/agent/agents.py`、`backend/app/executor/case_schema.py`、`backend/app/api/cases.py` |
| 3 | berth 沙箱封装、统一 JSON → pytest 编译、接口 / Web 执行器、失败重跑与 flaky 判定、curl 复现命令、隔离与凭据注入 | `backend/app/sandbox/*`、`backend/app/executor/*` |
| 4 | 失败归因、缺陷生成、看板、HTML 报告导出、缺陷修复清单导出（Markdown / JSON）、可追溯、回测脚本 | `backend/app/worker/handlers.py`、`backend/app/reporting/*`、`scripts/backtest.py` |
| 5 | 白盒代码走查（带代码位置）、与黑盒分开展示、风险 → 建议用例 | `backend/app/agent/agents.py::whitebox_review` |
| UI | 深色侧栏 + 工作台式看板：行动中心、三栏用例审核、执行分诊、缺陷分诊、任务中心、命令面板 | `frontend/src/app/*`、`frontend/src/ui/*`、`frontend/src/pages/*` |

几个关键设计：

- **统一用例 JSON** 是执行器唯一输入，格式见 [`docs/case-schema.md`](docs/case-schema.md)。
- **待确认断言不判失败**：`status != confirmed` 或没有 `evidence` 的断言只记录、不报错。
- **spec 变更自动标记**：重新导入 spec 后做 diff，受影响用例自动置为 `needs_review`。
- **失败重跑**：失败用例自动重跑一次，两次结果不一致 → `flaky`，不报缺陷。
- **审核工作台**：覆盖树 → 用例列表 → 详情/断言/依据，支持 J/K 切换、A 启用、E 编辑、批量执行选中。
- **执行分诊**：状态机时间线、结果分布、失败抽屉（请求 / 响应 / 断言 diff / 日志 / 归因）、一键重跑失败；失败归因会带入端点 schema、代码/文档证据，并对 5xx、异常泄露、空名称、负数分页参数等确定性信号自动判缺陷和确认 finding；默认项目的 findings 写操作有独立项目保护，避免自测数据污染；失败结果顶层请求/响应绑定真实失败步骤，cleanup 不再覆盖复现信息。
- **理解一致性**：代码路由会还原 FastAPI router prefix，过滤注释/docstring/字符串误识别，合并 OpenAPI 与代码同路径 endpoint；notes 文档不计入接口契约，避免把 README 当 API 文档制造大量假缺口。项目设置可配置性能 SLA，性能维度生成器在文档没有 SLA 时会读取该配置，避免产出只有 missing_input 的空壳用例；Shakedown 自身同步 API 基线为 P95 ≤ 100ms、P99/硬上限 ≤ 200ms。用例侧的待确认断言 / 无步骤 / 待补全说明统一汇总在「理解 → 契约缺口」，可导出 Markdown 报告交给被测系统的维护者或其授权的编码 Agent；也可逐条触发 AI 补全用例（只改用例，不修改被测系统）。缺口支持认领 / 已修复待复检 / 接受风险 / 不修复，并记录负责人与备注。
- **实时感**：后台任务全局指示器、页面自动轮询、完成 toast；截图 / trace 通过 `/api/artifacts/*` 提供。
- **缺陷修复清单**：缺陷页可按当前筛选导出 AI 可执行的 Markdown（`GET /api/projects/{id}/findings/export.md`），也提供 JSON（`.../export.json`）给自动化工具；按 AI 分诊根因分组，带证据、代码位置、复现命令和验收标准。
- **修复回写**：MCP / API 支持 Agent 提交 `claimed_fixed` 修复声明（commit / PR / 文件 / 测试结果），再由回归验证进入 `verified_fixed` 或 `reopened`；缺陷详情展示修复历史与验证按钮。缺陷支持最终处置：误报 / 接受风险 / 不修复，并可指派负责人、记录备注；外部 GitHub / Jira 工单状态可同步展示。
- **测试设计增强**：用例搜索支持标题 / 用例 ID / 缺陷 ID；生成入口支持维度与范围选择，默认只补缺失维度；生成上下文带请求 schema，执行前按端点 schema 自愈常见类型漂移；运行时支持 `[*]`、错误体兼容和数字/字符串 ID 比较；白盒建议用例可一键 AI 补全步骤与断言；用例带 api / process / artifact / frontend / privileged 验证方式，API 用例自动回归，构建产物和进程类由开发 Agent 通过 MCP 回写外部验证。
- **工单 / MCP 接入**：修复组可直接创建 GitHub Issue / Jira 工单；凭据放系统设置，仓库 / Jira 项目放项目设置。原生 Streamable HTTP MCP 挂载在 `/mcp/`，支持按客户端发放 `read` / `write` Token；编码 Agent 可通过 MCP 读取契约缺口报告、重新解析知识源、重算一致性、触发用例重评估与回归，全程不修改被测系统。同时保留 `scripts/shakedown_mcp.py` 供本机 stdio 使用。配置见 [`docs/integrations.md`](docs/integrations.md)。


---

## 界面

前端按「测试工作流」组织，而不是按数据库表：

| 导航 | 作用 |
|---|---|
| 总览 | 行动中心：待审核 / 失败 / 待分诊缺陷 / 三方不一致，点一下直达处理位置；附执行趋势、维度、模块覆盖 |
| 测试设计 | 三栏审核工作台：覆盖树 → 用例列表 → 步骤 / 断言 / 依据；支持编辑、批量启用、跑选中用例 |
| 执行 | 执行列表与详情：状态机时间线、结果分布、失败抽屉、归因修正、重跑失败、HTML 报告、两次运行对比 |
| 缺陷 | 黑盒 / 白盒分诊：AI 聚类与建议、证据、代码位置、复现命令、关联执行，确认 / 误报；按当前筛选导出 AI 可执行的修复清单 |
| 理解 | 知识源、接口清单、三方一致性（带导出修复清单）、Spec 版本对比 |
| Agent 活动 | 后台任务状态与 Agent 轨迹 / token 用量 |
| ⌘K | 全局命令面板：跳转、搜索用例 / 缺陷 / 接口 |

执行页支持「对比最近两次」：新失败、持续失败、已修复、新增/移除用例一目了然；运行详情里也能直接「对比上次」。
右下角可切换深色 / 浅色主题（跟随系统偏好，选择会记住）。

设计系统在 `frontend/src/ui/`，所有枚举文案集中在 `frontend/src/lib/labels.ts`。

---

## 架构

```
┌────────────┐      ┌──────────────┐      ┌──────────────┐
│  前端看板   │ HTTP │  后端 (FastAPI) │      │  PostgreSQL   │
│ React + TS  │◀────▶│  项目/用例/执行 │◀────▶│  JSONB+pgvector│
└────────────┘      └──────┬───────┘      └──────────────┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
        ┌──────────┐  ┌───────────┐  ┌────────────┐
        │ skipper  │  │  worker   │  │   berth    │
        │ (Agent)  │  │ 状态机     │  │  (沙箱)     │
        │ provider │  │ 队列/租约  │  │ 隔离执行    │
        └──────────┘  └───────────┘  └────────────┘
```

一次执行的完整链路：

```
项目 + 知识源
   └─ ingest → chunk(+向量) / endpoint(+来源) / consistency
        └─ agent → feature → test_case（默认待确认）
             └─ 人工审核启用
                  └─ worker: preparing → running → analyzing → completed
                       ├─ 准备环境（按项目起独立被测库）
                       ├─ 编译：JSON → pytest（写进沙箱 /workspace）
                       ├─ 执行：pytest + httpx / Playwright
                       ├─ 失败重跑 → flaky 判定
                       └─ 归因 → finding（带复现命令）
```

---

## 目录结构

```
shakedown/
├── backend/                 FastAPI 后端
│   ├── app/
│   │   ├── api/             REST 路由（项目 / 知识源 / 用例 / 执行 / 缺陷 / 报告）
│   │   ├── agent/           skipper：provider 抽象、工具调用循环、各业务 Agent
│   │   ├── ingest/          知识源解析：markdown / code / openapi / consistency
│   │   ├── executor/        统一用例 schema、编译器（JSON→pytest）、运行时
│   │   ├── sandbox/         berth 客户端 + 本地降级沙箱
│   │   ├── worker/          DB 驱动的任务队列与状态机
│   │   ├── reporting/       HTML 报告导出
│   │   ├── models.py        全部数据表
│   │   └── main.py          应用入口
│   ├── alembic/             迁移
│   └── tests/               pytest 测试
├── frontend/                React + TS + Vite 看板
│   └── src/
│       ├── app/             应用壳、侧栏、顶栏、命令面板
│       ├── ui/              设计系统（按钮 / 卡片 / 抽屉 / 图表 / toast）
│       ├── pages/           总览 / 测试设计 / 执行 / 缺陷 / 理解 / 活动 / 设置
│       └── lib/             API、hooks、文案映射、格式化
├── runner/                  执行器镜像（pytest / httpx / playwright）
├── docker/                  Dockerfile / nginx / postgres 初始化
├── examples/                演示与回测样例
├── scripts/                 demo.py / backtest.py
└── docs/                    架构、用例格式、重做方案、berth 依赖…
```

---

## 配置

所有配置项见 [`.env.example`](.env.example)，前缀 `SHAKEDOWN_`。常用的几个：

| 变量 | 说明 | 默认 |
|---|---|---|
| `SHAKEDOWN_DATABASE_URL` | 平台库连接串 | `postgresql+psycopg://shakedown:shakedown@localhost:5432/shakedown` |
| `SHAKEDOWN_LLM_PROVIDER` | `mock` / `openai` / `skipper` | `mock` |
| `SHAKEDOWN_LLM_BASE_URL` | OpenAI 兼容接口地址；填到 `/v1` 即可，也兼容完整 `/chat/completions` 地址 | `http://localhost:11434/v1` |
| `SHAKEDOWN_LLM_JOB_TIMEOUT` | 单个生成批次的硬超时（秒），超时会跳过该批，重试时重新生成 | `360` |
| `SHAKEDOWN_LLM_CONCURRENCY` | 生成用例时同时发起的模型调用数；上游不稳定时设为 `1` 更可靠 | `4` |
| `SHAKEDOWN_EMBEDDING_PROVIDER` | `hash`（离线）/ `openai` | `hash` |
| `SHAKEDOWN_SANDBOX_MODE` | `berth` / `local` | `local` |
| `SHAKEDOWN_BERTH_BASE_URL` | berth 服务地址 | `http://localhost:9000` |
| `SHAKEDOWN_WORKER_ENABLED` | 是否启动后台 worker | `true` |
| `SHAKEDOWN_MCP_REQUIRE_AUTH` | 原生 HTTP MCP 是否要求 Bearer Token | `true` |
| `SHAKEDOWN_MCP_ALLOWED_HOSTS` | MCP HTTP 允许的 Host（逗号分隔） | `127.0.0.1,...` |
| `SHAKEDOWN_ENCRYPTION_KEY` | 测试账号 / DSN 的加密密钥（留空自动生成） | 空 |

> **OpenCode Go / 第三方兼容接口**：`LLM_BASE_URL` 填到 `/v1` 即可；即使直接粘贴完整的
> `/v1/chat/completions` 地址也能工作。调用 OpenCode Go 时会自动带上 `x-opencode-session`
> 和自定义 User-Agent，健康检查走 `/inference/go/v1/models`。
>
> GitHub Issue / Jira / MCP 的配置与使用见 [`docs/integrations.md`](docs/integrations.md)。

---

## 测试

```bash
cd backend && pytest -q          # 后端测试（需要本地 PostgreSQL + pgvector）
cd frontend && npm run typecheck && npm run build
```

后端测试覆盖：OpenAPI 解析（2.0 / 3.x、`$ref` 循环保护、`allOf`）、Markdown 与代码切块、
路由抽取、三方一致性、用例 schema 校验、编译器产物、运行时断言引擎，以及一条
「建项目 → 导入 → 生成 → 审核 → 执行 → 归因 → 报告」的端到端用例。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 组件、数据流、状态机、扩展点 |
| [docs/case-schema.md](docs/case-schema.md) | 统一用例 JSON 格式（执行器唯一契约） |
| [docs/usage.md](docs/usage.md) | 上手操作手册（从建项目到看报告） |
| [docs/redesign.md](docs/redesign.md) | UI / 功能重做方案与落地记录 |
| [docs/backtest.md](docs/backtest.md) | 有效性验证（回测）怎么跑、指标怎么算 |
| [docs/berth.md](docs/berth.md) | 与 berth 的接口约定与依赖清单（B1–B12） |
| [docs/integrations.md](docs/integrations.md) | 缺陷修复清单导出、GitHub Issue / Jira、MCP 接入 |
| [docs/questions.md](docs/questions.md) | 需求不明确处的记录与当前保守实现 |

---

## 许可

[MIT](LICENSE) © 2026 Shakedown contributors
