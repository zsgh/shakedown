# Shakedown — 软件测试 Agent 开发任务清单

> 本文档交给开发 AI 执行。按 Phase 顺序推进，每个 Phase 完成并通过验收标准后再进入下一个。
> 任务前的 `[ ]` 完成后改为 `[x]`。

---

## 0. 项目背景与约束

**命名与组件对应**：
- **Shakedown**：本项目（平台）名称，取新船试航之意
- **skipper**：任务规划执行 Agent（已有命名），负责知识理解、规划、用例生成、调度执行和失败分析，对应本文的 Agent 模块
- **berth**：沙箱（已在用），负责隔离执行用例，对应本文的沙箱与执行环境

**产品定位**：个人自用的测试 Agent，目标是**真实测出被测项目的 bug**，不追求用例数量。

**核心流程**：
1. 新建 Project（测试工作空间）
2. 绑定知识源：本地 clone 的代码库、Markdown 文档、OpenAPI/Swagger 文档
3. Agent 自动规划测试点与用例，按维度分类：**功能、缺陷/边界、安全、性能**
4. 白盒代码走查（独立于黑盒用例，单独展示）
5. 调用执行器，在隔离环境中执行
6. 生成网页看板报告，每个结论可追溯到用例、执行记录和依据

**被测对象**：Web 应用 + 后端 HTTP 接口。被测系统的语言/框架无关，黑盒测试只依赖对外行为。

**硬性约束**：
- 数据库使用 **PostgreSQL**（用 JSONB 存半结构化数据，pgvector 做知识检索）
- 项目之间**完全隔离**：独立的被测数据库实例、测试账号、环境地址，互不干扰
- 正常流程的功能测试必须保留，不能只测边界
- 每条断言的预期结果必须带**依据来源**；没有依据的标"待确认"，不直接判失败
- Agent 生成的用例默认"待确认"，人工启用后才参与执行
- 暂不做：CI/CD 集成、公共前提抽取、压测、飞书/语雀/Confluence 直连

---

## 1. 技术选型

| 模块 | 选型 |
|---|---|
| 后端 | Python 3.11+、FastAPI、SQLAlchemy、Alembic |
| 数据库 | PostgreSQL 16 + pgvector |
| 前端 | React + TypeScript + Vite（看板、审核队列） |
| Agent（skipper） | 自写工具调用循环，LLM 调用抽象成 provider 接口（便于替换模型）；与 skipper 现有实现对接，接口待确认 |
| 接口执行器 | pytest + httpx |
| Web 执行器 | Playwright |
| 沙箱 | berth（自有项目 github.com/zsgh/berth，缺能力直接在 berth 仓库改造，见 `berth-todo.md`） |
| 任务调度 | 数据库驱动的任务状态机 + 后台 worker（第一版不引入 Celery） |
| OpenAPI 解析 | prance / openapi-spec-validator 或同类库，支持 Swagger 2.0 与 OpenAPI 3.x |
| 代码解析 | tree-sitter（通用路由抽取的辅助手段）+ Agent 读代码 |

---

## 2. 数据模型（Phase 0 建表）

所有表带 `id`、`created_at`、`updated_at`。半结构化字段用 JSONB。

- [x] `project`：名称、描述、被测环境地址（base_url）、测试账号配置（加密存储）、被测系统启动方式、隔离配置
- [x] `knowledge_source`：project_id、类型（`code` / `markdown` / `openapi`）、来源（本地路径或 URL）、版本标识（commit hash 或内容 hash）、解析状态、错误信息
- [x] `chunk`：source_id、内容、来源位置（文件路径+行号 / 文档标题层级）、向量（pgvector）、元数据 JSONB
- [x] `endpoint`：project_id、method、path、参数/请求体/响应 schema（JSONB）、鉴权要求、来源（`openapi` / `code` / `doc`，可多个）、一致性状态（三方对照结果）
- [x] `feature`：project_id、名称、所属模块、来源引用 JSONB（文档章节、接口、代码位置）
- [x] `test_case`：project_id、feature_id、维度（`functional` / `boundary` / `security` / `performance`）、类型（`api` / `web`）、前置条件、步骤 JSONB、预期结果 JSONB、**预期依据 JSONB**、生成方式（`agent` / `manual`）、状态（`pending` / `enabled` / `disabled` / `needs_review`）
- [x] `run`：project_id、触发方式、环境快照、状态机状态、开始/结束时间
- [x] `case_result`：run_id、case_id、结果（`pass` / `fail` / `flaky` / `error` / `skipped`）、请求响应 JSONB、日志、截图路径、失败归因 JSONB（类别：环境/用例/疑似缺陷，附理由）、是否重跑
- [x] `finding`：project_id、来源（`execution` / `whitebox`）、标题、描述、严重级别、依据 JSONB、代码位置、关联 result_id、复现命令、状态（`pending` / `confirmed` / `false_positive`）
- [x] `agent_trace`：记录 Agent 每次规划/生成/分析的输入输出与工具调用，便于排查与复盘
- [x] `human_feedback`：对用例、归因、finding 的人工修正记录，供后续优化

**验收**：Alembic 迁移可从空库一键建出全部表；pgvector 扩展可用。

---

## Phase 0：工程骨架

- [x] 初始化 monorepo：`backend/`、`frontend/`、`runner/`（执行器镜像）、`docker/`、`docs/`
- [x] `docker-compose.yml`：启动平台 PostgreSQL（含 pgvector）、后端、前端
- [x] 后端基础：配置管理（环境变量）、日志、统一错误处理、健康检查接口
- [x] Alembic 迁移 + 第 2 节全部表
- [x] LLM provider 抽象层：统一接口（对话、工具调用、结构化输出），支持配置模型与密钥；记录 token 用量
- [x] 后台任务框架：基于 `run`/任务表的状态机 worker，状态：排队中 → 准备环境 → 执行中 → 分析中 → 完成/失败；每步写库，支持断点恢复
- [x] 前端基础：路由、布局、API 客户端、项目列表页

**验收**：`docker compose up` 后可打开前端，创建/查看项目；后台 worker 能跑一个空任务并完整走完状态机。

---

## Phase 1：项目与知识源

### 1.1 项目管理
- [x] 项目 CRUD 接口与页面：名称、被测环境地址、测试账号、启动方式
- [x] 项目隔离配置：每个项目独立的被测库实例参数（容器名、端口、初始化脚本路径）

### 1.2 Markdown 文档导入
- [x] 支持上传 md 文件或指定本地目录
- [x] 按标题层级切块，保留章节路径作为来源位置
- [x] 生成向量并存入 pgvector；文档变更（内容 hash）后可增量重新解析

### 1.3 代码库导入
- [x] 指定本地已 clone 的目录，记录 commit hash
- [x] 代码切块（按文件/类/方法），保留文件路径与行号
- [x] 提供给 Agent 的代码工具：列目录、读文件、grep 搜索、按符号检索
- [x] 路由/接口清单抽取：Agent 结合 grep 与 tree-sitter 定位路由定义，产出 `endpoint` 候选（来源标记为 `code`）

### 1.4 OpenAPI / Swagger 导入（必须支持）
- [x] 支持输入形式：本地 JSON/YAML 文件、URL（如 `/v3/api-docs`、`/swagger.json`、`/openapi.json`）、Swagger UI 页面地址（自动探测其背后的 spec 地址）
- [x] 同时支持 **Swagger 2.0** 与 **OpenAPI 3.0 / 3.1**
- [x] 解析并展开 `$ref`（含循环引用保护），处理 `allOf` / `oneOf` / `anyOf`
- [x] 归一化为内部 `endpoint` 模型：method、path、参数（path/query/header/body）、请求体 schema、响应 schema、鉴权方式（securitySchemes）、示例值
- [x] 保存 spec 快照与内容 hash；重新拉取后做 diff，列出新增/删除/变更的接口，并把受影响的用例标记为 `needs_review`
- [x] 解析失败要给出可读错误（哪一行、哪个 `$ref`），不要静默丢弃

### 1.5 三方一致性对照
- [x] 对照 OpenAPI、代码抽取的路由、文档描述三者：
  - 文档有、代码/spec 没有
  - 代码有、文档/spec 没有
  - 参数或响应字段在三者间不一致
- [x] 结果写入 `endpoint.一致性状态`，并在看板中作为"可疑点"列表展示（这本身就是潜在缺陷线索）

### 1.6 前端
- [x] 知识源管理页：添加/删除/重新解析，显示解析状态与版本
- [x] 接口清单页：列出 endpoint，显示来源与一致性状态

**验收**：
- 能用一个真实项目导入：1 份 md 文档 + 本地代码库 + 一份 Swagger 2.0 与一份 OpenAPI 3.x spec
- 接口清单正确展示，三方不一致项能被列出
- 修改 spec 后重新导入，diff 结果准确

---

## Phase 2：规划与用例生成

### 2.1 功能点抽取
- [x] Agent 基于文档 + endpoint + 代码，抽取 `feature`，每个功能点带来源引用
- [x] 功能点与 endpoint 建立映射；无法映射的标记出来供人工检查

### 2.2 按维度生成用例
- [x] 固定模板：每个功能点至少 1 条**正常流**用例，断言需验证业务结果（状态变化、金额、数据落库），不能只看状态码
- [x] **缺陷/边界**：空值、超长、负数/零、非法状态跳转、重复提交、精度与时区等
- [x] **安全**：越权访问（A 访问 B 的数据）、未鉴权访问、注入类输入、敏感信息泄露；依据来自鉴权设计与接口定义
- [x] **性能**：第一版只做接口级响应时间基线，阈值来自文档中的 SLA；无 SLA 则标"缺少输入"，不硬编阈值
- [x] 任何维度缺少依据时，输出"缺少输入"提示，而不是硬凑用例
- [x] 每条用例必须带 `预期依据`（文档章节/spec 条目/代码位置）；无依据的断言标 `待确认`
- [x] 支持用例手写前置步骤（不做自动抽取公共前提）
- [x] 生成测试数据：带 run_id 前缀的唯一数据，避免冲突；敏感数据脱敏

### 2.3 用例格式（统一 JSON，执行器只认此格式）
- [x] 定义并文档化用例 schema：前置条件、步骤（请求/页面操作）、断言（类型、期望值、依据）、清理步骤
- [x] 提供 schema 校验，Agent 生成后先校验，不合法则让 Agent 重试

### 2.4 前端：用例管理
- [x] **功能覆盖树**：模块 → 功能点 → 用例，显示各维度用例数，无覆盖标红
- [x] **待审核队列**：逐条启用/修改/停用，支持批量操作
- [x] **用例详情**：步骤、预期结果、预期依据（可跳转到原文/代码位置）
- [x] 修改与停用记录写入 `human_feedback`；停用不删除

**验收**：
- 对一个真实项目生成用例，每个功能点都有正常流用例
- 抽查 20 条用例，预期依据均能定位到真实来源
- 覆盖树能暴露未覆盖的功能点

---

## Phase 3：执行器与沙箱

### 3.1 环境准备
- [x] 通过 berth `PUT /services/{项目}-db` 按项目起停独立的被测数据库（服务名即身份，天然按项目隔离）；执行初始化 SQL / 迁移脚本，保证每次起点一致（依赖 `berth-todo.md` 的 B5、B6、B8）
- [x] 支持"整库重置"和"带 run_id 前缀数据 + 事后清理"两种隔离方式（接口自行提交事务时事务回滚无效，需前缀方案）
- [x] 项目配置的被测服务地址可指向本地 Docker 版服务或已有测试环境

### 3.2 沙箱（berth）
- [x] berth 现状（已核对其 README 与 docs/api.md）：HTTP API；`POST /scripts/run` 跑一次性脚本（overlayfs 隔离、uid 1000、cgroup 限资源、`task_id` 持久 `/workspace`）；`PUT /services/{name}` 按模板起长驻服务（现有 redis、mysql/MariaDB，systemd 托管，名字即身份）；host 网络；SQLite 记录状态。缺口与改造任务见 `berth-todo.md`，**先完成其中 P0（B1 到 B7）再做执行器**
- [x] 在 Shakedown 后端封装 `BerthClient`（调用 berth HTTP API：起停服务、上传用例、执行、取产物、清理），执行器只依赖该客户端
- [x] 用例在沙箱内执行，用完即销毁；限制 CPU、内存、执行时长
- [ ] 网络白名单：容器仅能访问该项目配置的被测地址，无法访问生产域名和外网
      > 平台侧已预留 `project.isolation.allowed_hosts`；**实际拦截依赖 berth B2 网络策略**，berth P0 完成前不生效（Shakedown 侧无法单方面实现，见 `docs/berth.md`）
- [x] 凭据隔离：只注入该项目的测试账号，不注入任何生产凭据
- [ ] 文件系统：仅挂载用例代码与报告目录，其余只读
      > 依赖 berth B3 文件进出；当前实现里用例代码与产物都写在 berth 的 `/workspace` 内（overlay 层天然隔离），但「只挂载指定目录」要 berth 支持

### 3.2.1 对 berth 的依赖

> berth 的改造任务单独放在 `berth-todo.md`（在 `zsgh/berth` 仓库实施），与本文档并行推进。Shakedown 只通过 berth 的 HTTP API 调用它。

| Shakedown 任务 | 依赖的 berth 任务 |
|---|---|
| 起停并初始化被测库（3.1） | B5 服务参数覆盖与初始化、B6 PostgreSQL 模板、B8 服务重置 |
| 网络白名单、凭据隔离（3.2） | B1 API 鉴权、B2 网络策略 |
| 上传用例、取回报告截图（3.3、3.4） | B3 文件进出 |
| 长时间执行、Web 用例（3.3、3.4） | B4 异步执行与资源上限、B7 runner 镜像 |
| 失败归因取被测服务日志（4.1） | B9 服务日志接口 |
| 起被测系统并等待就绪（3.1） | B10 http 健康检查、B11 被测系统运行时 |
| 看板里从失败结果跳转到沙箱执行详情、按项目回溯（4.2） | B12 标签、U8 按项目过滤、U10 深链约定（berth 可视化界面） |

**联调节点**：berth 的 P0（B1 到 B7）完成后，再开始 3.3 / 3.4 的执行器开发；Phase 1、2 不依赖 berth，可同步进行。

### 3.3 接口执行器
- [x] 将统一 JSON 用例转为 pytest + httpx 可执行代码（数据库为准，代码由数据库生成，不手改代码）
- [x] 断言引擎：支持状态码、响应字段、数据库状态断言、跨接口一致性断言
- [x] 收集完整请求响应、耗时、日志

### 3.4 Web 执行器
- [x] Playwright 执行页面用例：操作步骤、断言、失败截图、trace 保存
- [x] 登录态复用，避免每条用例重复登录

### 3.5 结果处理
- [x] 失败用例自动重跑一次；两次不一致标 `flaky`，不报缺陷
- [x] 失败压缩为最小复现步骤，接口用例生成 curl 复现命令
- [x] 高风险操作（删除数据、修改配置）在白名单之外需人工确认

**验收**：
- 一次完整执行：起库 → 起沙箱 → 跑接口与 Web 用例 → 收集结果 → 销毁环境
- 两个项目同时执行互不影响
- 沙箱内访问外网/生产地址被拒绝

---

## Phase 4：分析、报告看板与有效性验证

### 4.1 失败归因
- [x] Agent 对每个失败结果判断类别：环境问题 / 用例问题 / 疑似真实缺陷，必须给出理由与依据
- [x] 疑似缺陷自动生成 `finding`，含复现步骤、请求响应、日志、依据
- [x] 人工修正归因后写入 `human_feedback`

### 4.2 网页看板
- [x] **项目总览**：功能覆盖率、各维度用例数与通过率、最近几次执行趋势
- [x] **执行结果页**：按次展示通过/失败/flaky 分布，失败项展开看归因、请求响应、日志、截图；失败项提供跳转到 berth 执行详情的链接（`/ui/#/runs/{task_id}`）
- [x] **缺陷列表**：疑似缺陷，支持标记确认/误报，显示依据与复现命令
- [x] **可追溯**：报告中任一结论可点回用例、执行记录、依据原文
- [x] **接口一致性页**：展示 Phase 1.5 的三方不一致项
- [x] 报告可导出为 HTML 文件

### 4.3 有效性验证（重要）
- [x] 提供回测脚本：给定一个项目的 N 个已修复的历史 bug（commit 对），回退到修复前代码，让 Agent 仅基于当时的文档生成并执行测试
- [x] 输出指标：抓到的 bug 比例、误报数、漏报原因归类
- [x] 此指标作为"好不好用"的核心判断依据，并展示在看板上

**验收**：
- 看板能完整展示一次执行的所有结论并可逐级追溯
- 回测脚本在至少一个真实项目上跑通并产出指标

---

## Phase 5：白盒代码走查

- [x] Agent 按模块读代码，查找逻辑风险：入参未校验、事务缺失、越权判断缺位、并发问题、异常吞没、敏感信息日志等
- [x] 输出 `finding`（来源 `whitebox`），**必须带代码位置（文件+行号）和依据**，无法定位的不输出
- [x] 看板中与黑盒结果分开展示，可按模块/风险类型筛选
- [x] 白盒发现可关联到黑盒用例：对高风险点建议补充对应的验证用例
- [x] 人工可标记确认/误报，记录用于后续优化

**验收**：对一个真实项目走查，抽查 20 条发现，代码位置均准确，并统计确认率。

---

## Phase 6：后续（本期不做，仅记录）

- [ ] 公共前提抽取与复用（显式依赖 + 前提校验）
- [ ] 压测与性能分析
- [ ] CI/CD 触发与按 git diff 的回归范围选择
- [ ] 飞书 / 语雀 / Confluence 文档直连
- [ ] UI 自愈选择器、探索式测试
- [ ] 属性测试、差分测试、变异测试

---

## 全局注意事项（开发 AI 必读）

1. **断言依据**是本项目的核心质量保证：所有预期结果必须能追溯到来源；无依据不判失败。
2. **项目隔离**不可妥协：任何共享状态（数据库、账号、缓存、端口）都要按项目隔离。
3. **Agent 与执行器解耦**：Agent 负责规划、生成、分析；执行器只按统一用例格式执行并收集结果。
4. **数据库为准**：用例的真身在数据库，可执行代码由其生成。
5. **不可信代码**：Agent 生成的任何代码必须在沙箱中执行。
6. **每个 Phase 完成后**补充 README 与使用说明，并附一个可复现的端到端演示。
7. 遇到需求不明确的地方，在 `docs/questions.md` 中记录问题并选择最保守的实现，不要自行扩大范围。

---

## 2026-11 UI / 功能重做（已完成）

> 详细方案与页面规格见 [`docs/redesign.md`](docs/redesign.md)。旧的 GitHub 风格表格页已被替换。

- [x] 设计系统：深色侧栏 + 浅色工作区、统一状态色、按钮 / 卡片 / 抽屉 / Toast / 图表
- [x] 应用壳：项目切换器、导航、面包屑、全局任务指示、⌘K 命令面板、响应式
- [x] 项目组合页：项目卡片、健康徽标、待审核 / 缺陷计数、新建三步向导、删除确认
- [x] 项目总览：行动中心、覆盖率 / 通过率 / 待审核 / 缺陷指标、执行趋势、维度与模块覆盖
- [x] 用例审核工作台：覆盖树 + 列表 + 详情三栏、筛选 / 分页 / 批量、编辑用例、跑选中、J/K/A/E 快捷键
- [x] 执行：实时轮询、状态机时间线、结果分布、失败抽屉、请求 / 响应 / 断言 / 日志、修正归因、重跑失败、取消
- [x] 缺陷：黑盒 / 白盒分诊、抽屉证据、代码位置、复现命令、关联执行、确认 / 误报
- [x] 理解：知识源紧凑列表、接口抽屉、三方一致性、导出修复清单
- [x] Agent 活动：任务中心、轨迹输入输出、token 用量
- [x] 设置：全局分组配置（密钥打码不覆盖）、项目配置、危险区
- [x] 后端补口：用例分页、失败重跑、单条结果、项目健康、全局搜索、任务列表、产物静态路由
- [x] 展示层全量中文化，统一运行 / 用例 / 结果 / 缺陷 / 任务状态文案
- [x] 修复：全局设置保存 `_overrides` 导致 422、密钥打码回写覆盖、布尔字符串误判
- [x] 修复 AI 接入：完整 `/chat/completions` 地址不再重复拼接；OpenCode Go 自动带 `x-opencode-session` / User-Agent；模型列表健康检查走正确路径；兼容 `{"type":"object","value":…}` 包装；归因证据为字符串时不再崩溃
- [x] 缺陷按「用例 + 未关闭」去重：重复失败更新证据与关联执行，不再刷屏
- [x] 白盒走查：finding 关联建议用例、建议用例最近执行回链、再次走查复用 finding / 用例
- [x] 白盒走查：模块级进度写入 task checkpoint，Agent 活动显示「白盒 2/5 · internal」
- [x] 白盒走查降速：工具轮次 8→5、单独使用 medium 推理；实测 7m17s → 5m52s
- [x] 白盒走查可停止 / 继续：任务取消 API、完成模块 checkpoint（done_modules）、按模块续跑
- [x] 白盒 evidence 统一为对象数组，修复字符串 evidence 导致 findings 接口 500
- [x] AI 缺陷分诊：按模块/主题聚类，默认「按模块 + 判定」把 79 条压成 13 组；AI 只写 `cluster_key/triage/triage_status`，不删原始 finding
- [x] 分诊结果支持按组批量确认、标记误报、恢复；单条抽屉显示 AI 判定、置信度、理由、建议动作
- [x] 分诊任务进度写入 task checkpoint，Activity 显示「分诊 4/5 · web」
- [x] generate_cases：批次硬超时（默认 360s）、失败批次跳过、任务心跳续租、协作式取消
- [x] generate_cases：按 `dimension + feature_ids` 记录已完成批次，任务重试时断点续跑
- [x] generate_cases：批次进度写入 checkpoint，Activity 显示「生成 12/47 · 失败 1」；trace 记录耗时
- [x] generate_cases：按 wave 隔离线程池，超时线程不会占满线程池导致后续批次永远排不到
- [x] generate_cases：超时/repair 改用墙钟，兼容 macOS 休眠；repair 放进工作线程并发执行
- [x] 生成接口支持 `only_missing=true`，只给没有任何用例的功能点补生成
- [x] `SHAKEDOWN_LLM_CONCURRENCY` 可配置；provider 不稳时默认示例用 1
- [x] 缺陷修复清单导出：按 AI 分诊根因分组，Markdown（AI 可执行）+ JSON（自动化），支持筛选与敏感值脱敏
- [x] Findings 页面「导出修复清单」按钮，跟随当前状态/来源/级别/关键词/结论筛选
- [x] GitHub Issue / Jira 集成：修复组预览 + 创建工单 + IntegrationIssue 幂等回链
- [x] MCP stdio server：list_finding_groups / get_finding / get_fix_plan / create_github_issue / create_jira_issue
- [x] 工单配置分层：全局只放凭据 / 服务地址，GitHub 仓库与 Jira 项目放在「项目设置 → 工单集成」，支持全局回退
- [x] 原生 Streamable HTTP MCP：挂载 /mcp/，Bearer Token 客户端，read / write scope
- [x] MCP 客户端 Token 管理 UI：一次性显示明文、删除、权限隔离
- [x] 缺陷修复生命周期：open → claimed_fixed → verified_fixed / reopened，记录 Actor、commit、PR、测试结果与历史
- [x] MCP 回写工具：report_finding_fix / verify_finding_fix；缺陷详情展示修复声明与人工验证按钮
- [x] 测试设计支持按用例 ID / 缺陷 ID / 标题搜索，并显示关联 finding ID
- [x] 白盒建议用例 AI 补全（enrich_case）：只补 steps / assertions / evidence，保持 pending
- [x] 自动验证任务 verify_fixes：有关联可执行用例则回归；没有则转 needs_human_verification
- [x] 缺陷列表修复状态筛选优化：服务端过滤、gzip、project+fix_status+created_at 复合索引
- [x] 用例验证方式：api / process / artifact / frontend / privileged / manual，自动分类 + 外部验证历史
- [x] MCP report_case_verification：开发 Agent 回写构建产物 / 进程 / 浏览器验证，并同步关联 finding
- [x] 自动验证按验证方式路由：API 用例回归；非 API 转 needs_external_verification，不硬转人工
- [x] 生成用例 preconditions 本地归一化，避免 schema 假失败触发 1～2 轮模型 repair
- [x] 生成入口改为维度选择 + 范围选择，默认“只补缺失维度”，避免一键全量 60 批
- [x] 新增 only_missing_dimensions：按「功能点 + 维度」跳过已有维度；自测项目实测 1 批 45 秒完成
- [x] 执行前按端点 schema 归一化请求体 / query：纠正字符串数字、对象写成字符串、非法 enum、缺省 required body
- [x] 生成上下文加入 request_schema / param_schema，减少模型把 priority 写成 P1、把 isolation 写成字符串
- [x] 运行时补齐 JSONPath `[*]`、错误体 `$.detail` 兼容、数字/字符串 ID 比较、空值 exists、`/api` 前缀回退和 typed placeholder 渲染
- [x] 自测项目回归：233 条用例通过率 51.07% → 98.71%，剩余 3 条为真实 API 校验问题
- [x] 失败归因注入端点 request/response schema、字段约束、相关代码/文档证据，避免只看到 `422` 响应就误判依据不足
- [x] 确定性归因规则：5xx / SQL 驱动异常泄露 / 空 name·title·username 被接受 / 负数 limit 直接判缺陷，并保留 common_sense 证据
- [x] 高置信度确定性缺陷自动确认 finding（`confirmed_by=auto:attribution`），最新归因为 case 时自动关闭旧 pending finding
- [x] 收紧边界校验：项目 / 功能点 name 非空，所有分页 `limit` 增加 `ge=0`；`#216/#217/#220/#222` 经 MCP developer-agent 提交并验证为 verified_fixed
- [x] 增加默认项目写操作保护：未创建独立测试项目的 findings 写操作会拒绝执行，避免自测用例污染项目 11 的 finding 状态
- [x] 隔离 9 条缺少独立 finding fixture 的危险旧用例（标记 disabled + missing_input）；全量回归 224/224 通过
- [x] 修复“理解”一致性噪声：还原 FastAPI router prefix、过滤注释/docstring/字符串误识别、归一 `{path:path}`、合并 OpenAPI/代码 endpoint、仅用 spec 文档做文档覆盖
- [x] 自测项目一致性从 201 强信号降至 0；95 个 API 接口全部一致
- [x] 失败结果顶层 request/response 指向真实失败步骤，不再被 cleanup DELETE 覆盖；新增 MCP 用例/运行读取与触发工具
- [x] OpenAPI 错误响应改为真实 ErrorEnvelope；500 不再回显 SQL/驱动异常；MCP 增加知识源、一致性、用例、运行读取与触发工具
- [x] actionable 修复清单纳入「已确认但未分诊」的黑盒 execution finding；已 verified_fixed 的不再重复出现
- [x] 项目设置增加性能 SLA（ms）；生成器支持项目 SLA 兜底；只有 missing_input 的占位性能用例不再阻止重新生成
- [x] Shakedown 自身 SLA 收紧为同步 API P95 ≤ 100ms、P99/硬上限 ≤ 200ms；项目 11 配置 100ms，已生成性能用例阈值同步改为 100ms
- [x] timing 断言支持 N 次顺序采样（默认 20），输出 P50/P95/P99/min/max；自测 case #4374 实测 P95=19.5ms，证明实际耗时未丢失
- [x] 每次模型调用（initial / repair-N）单独记录 trace，支持按 task_id 查看原始输出和耗时
- [x] 执行对比与通过率趋势对比（P1）
- [x] spec 版本 diff 页面（P1）
- [x] 深色模式（P1）
- [ ] 登录 / 多租户（P2，本期不做）

---

## 实现说明（交付记录）

**交付范围**：Phase 0 – Phase 5 全部实现；Phase 6 按约定本期不做。

**目录结构**

```
backend/    FastAPI + SQLAlchemy + Alembic（后端 + Agent + 执行器 + worker）
frontend/   React + TypeScript + Vite（看板、审核队列）
runner/     执行器镜像（pytest + httpx + playwright）
docker/     Dockerfile / nginx / postgres 初始化
docs/       架构、用例格式、使用手册、回测、berth 依赖、问题清单
scripts/    demo.py（端到端演示）、backtest.py（有效性验证）
examples/   演示项目（带两个 bug）、回测仓库、历史 bug 清单
```

**快速验证**

```bash
docker compose up -d --build                 # 平台（前端 5173 / 后端 8000）
python scripts/demo.py                       # 端到端演示：导入 → 生成 → 执行 → 缺陷 → 报告
python scripts/backtest.py \
  --repo examples/backtest-repo \
  --bugs examples/backtest-bugs.json \
  --target-base-url "" \
  --deploy-cmd "{python} {worktree}/server.py --port {port}"   # 回测指标
cd backend && pytest -q                      # 99 个测试
cd frontend && npm run typecheck && npm run build
```

**未闭环的外部依赖**（berth 侧，平台侧无法单方面实现）

- Phase 3 的「网络白名单」「文件系统只挂载指定目录」两项保持未勾选，依赖 `berth-todo.md` 的 B2 / B3；
  平台侧已预留配置位与接口，联调说明见 `docs/berth.md`。

**取舍与待确认**：需求不明确处的保守实现记录在 `docs/questions.md`（含 skipper 对接接口、边界依据判定、
flaky 判定、鉴权失败期望值等 10 条）。

**增量增强（实现过程中补上的）**

- [x] 仓库一键自动导入：给一个目录路径，自动发现代码库 / Markdown / OpenAPI，并建源解析
- [x] 文档式接口规范抽取：Markdown 里的 `METHOD + path + 说明` 端点表（代码块或表格）登记成来源为 `doc` 的接口
- [x] Markdown 支持远程 URL（GitHub blob 链接自动转 raw）
- [x] Go 路由抽取支持 ServeMux helper 批量注册（`api("/x", h)` 形式）
- [x] 网页文档抓取：HTML 页面 → Markdown（去脚本/样式/导航，保留标题/表格/列表/代码块）
- [x] 接口文档页自动探测 spec：Swagger UI / Redoc 惯例 + 自研页面（脚本里 fetch）+ 文档页同目录常见文件名
- [x] Go 路由表 map 字面量抽取（`"GET /api/x": handler`）—— 适配数据驱动端点表
- [x] Agent 读文档抽接口（非标准接口文档也认），保留原文引用作为依据；离线时用宽松规则兜底
- [x] 知识源重导入时清理「幽灵接口」（代码重构后消失的接口不再残留在清单里）
- [x] 一致性弱信号（文档缺口）默认折叠，只展示强信号；新增文档覆盖率汇总
- [x] 开发日志类文档（todo/changelog）不作为接口规范；方法名列表不误判为路径
- [x] Agent 工具调用循环改用 LangGraph 状态图（agent ⇄ tools），provider 适配为 LangChain ChatModel
- [x] 轮数用尽时强制收尾（不带工具再问一次拿最终答案）
- [x] 一致性判定区分强弱信号：强信号（文档提到但没实现 / 与 spec 冲突 / 参数不一致）与弱信号（文档缺口）

**质量红线落实位置**

| 红线 | 落实位置 |
|---|---|
| 断言必须有依据，无依据不判失败 | `app/executor/templates/runtime.py`（pending 断言只记录）、`app/executor/case_schema.py` |
| 项目完全隔离 | `app/executor/environment.py`（每项目独立 berth 服务 / DSN / 账号）、`{{run_id}}` 数据前缀 |
| 正常流优先 | `app/agent/providers/mock.py::_functional_cases`、真实模型侧 prompt 约束 |
| 数据库为准 | `app/executor/compiler.py`（代码由数据库生成） |
| 不可信代码沙箱执行 | `app/sandbox/*`（`BerthSandbox` / `LocalSandbox`） |

## Bug 发现率专项

目标：从“接口能跑”转向“能抓错”。每项先写分析，再实现并用真实项目验证。

### 1. 差分 / 越权矩阵
- 分析：同一资源换两个普通账号访问，比较页面、REST、git 三种通道；只测匿名/管理员会漏横向越权。
- [x] `idor-differential`：alice 建私有项目，viewer 读页面/API/git；反向再测
- [x] `api-authz`：从代码 `seg[0]` 抽 `/api/v1/<资源>`，测匿名 + 低权限
- [x] `delete-authz`：guest 成员尝试删除他人项目
- [x] 变异验证：删除项目/成员管理/项目设置三个管理员校验被用例杀死

### 2. 状态机
- 分析：单接口断言看不出“状态迁移后旧凭据是否仍有效”，需要登录/登出/吊销/重放链路。
- [x] `token-revocation`：建令牌 → 列表取 id → 吊销 → 旧令牌访问 401
- [x] `session-logout`：登录保存 Cookie → 登出 → 旧 Cookie 重放 401
- [x] 运行器补 `save_cookie`（只存变量，不落库）
- [ ] 归档 → push：需要 git wire 协议和可写工作区，当前 runner 没有 git 客户端；改为 CLI 用例后再做

### 3. 协议 / 输入 fuzz
- 分析：客户端格式错误应 4xx；5xx、堆栈、解析崩溃才是缺陷。
- [x] `fuzz-json`：API 畸形 JSON + npm 发布畸形 JSON；发现 NPM 发布 500（#230）
- [x] `fuzz-protocol`：git pkt-line 乱码、npm dist-tag 畸形 JSON、非法 URL 字节
- [x] 运行器支持原始字符串 body；二进制响应只存 size/sha256，避免 pgvector/JSONB 写入崩溃

### 4. 并发竞态
- 分析：并发同名创建、重复提交、最后管理员都可能绕过“先查后写”。
- [x] `race-condition`：5 个并发同名项目创建，断言状态码集合只有一个成功
- [x] `duplicate-submit`：同名项目提交两次，第二次不得再成功
- [x] 运行器补 `parallel` 步骤（独立 cookie 罐）
- [ ] 最后管理员并发禁用：需要两个临时管理员且可能把自己锁死，暂不自动化

### 5. 变异测试
- 分析：用“改坏代码 → 看测试是否变红”衡量断言杀伤力，而不是只看用例数。
- [x] `scripts/mutation_test_dmgit.py`：11 个鉴权/路径/输入/CSRF 变异体
- [x] 第一轮 10/11，幸存者是删除项目鉴权；补 `delete-authz` 后 13/13
- [x] 补成员管理、项目设置两个变异体并杀死
- 当前基线：**13/13 = 100%**（定向检查，不等于全量 377 条用例）

### 已发现的新缺陷（安全专项）
- [x] #231 `[越权] 同名用户组可读写该用户的个人项目`：用户组与个人命名空间同名时，`Access`/`canCreateIn` 不区分两种 namespace；隔离实例复现：bob 读取 alice 私有项目 200、在 alice 命名空间创建项目 201。
- [x] #232 `[竞态] 并发禁用可把最后一个管理员也禁掉`：两个管理员并发 toggle 对方，`AdminCount` 与 `UpdateUser` 存在 TOCTOU，最终 0 个启用管理员，管理面 401。
- [x] 自动回归：新增 `POST /projects/{id}/security-lab/run`，由平台自己构建一次性实例执行上述两项探针；Finding 标记 `auto:security-lab`。
