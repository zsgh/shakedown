# Shakedown 测试环节 oracle / 覆盖审计

> 目的：检查每个测试环节是否也存在“只依赖项目知识库、缺少通用测试智能”的问题，
> 以及由此带来的漏测、误报和闭环缺口。
>
> 结论先说：**问题不只存在于断言生成。功能点提取、边界/安全/性能用例生成、
> 测试数据、一致性、回归影响面、覆盖率统计都不同程度地依赖“文档写了什么”。**
> 修复方向不是降低断言标准，而是按 oracle 来源分层，并补 policy / heuristic /
> exploratory 能力。

## 0. Oracle 分层原则

| 层 | 来源 | 可判定性 | 典型用途 |
|---|---|---|---|
| `contract` | OpenAPI / Doc / Code | 强，可判失败 | 明确的状态码、字段、约束、业务规则 |
| `policy` | 显式安全/可靠性策略包 | 强，可判失败，但必须可追溯 | 不得 5xx、不得泄露堆栈/SQL、空标识、负数分页、鉴权隔离 |
| `heuristic` | 通用模型知识 / 风险经验 | 弱，只产生候选 | 可疑业务语义、异常模式、潜在越权 |
| `exploratory` | 变形 / 差分 / 属性 / 探索 | 中，需 triage | 创建→查询、删除→404、分页不重不漏、角色差异 |
| `observed` | 运行时观测/历史基线 | 中，用于变化检测 | 响应 schema 漂移、异常率变化、性能退化 |

关键原则：

- 没有 oracle 的检查只能算 probe，不能算断言；
- heuristic 结论不能直接判缺陷；
- observed 只能说明“变了”，不能单独说明“错了”；
- 知识库冲突时优先产生 contract_question，而不是硬选一个。

## 1. 环节审计

### 1.1 项目 / 环境建模

**依赖**：项目 `base_url`、隔离配置、测试账号、DB DSN、SLA、webhook。

**问题**：
- 平台不负责启动被测系统，`target_start` 只是说明；
- 账号 / DSN / 环境缺失会变成 `missing_input`，但这些不是产品缺陷；
- 环境不可达时仍可能生成用例，执行阶段才暴露。

**漏测风险**：
- 环境问题被误判为产品缺陷；
- 没有真实账号时，安全/权限类用例大面积缺失；
- 没有生产类数据时，分页、性能、隔离类缺陷测不出来。

**建议**：
- 环境 readiness gate：环境未就绪时不允许生成“可执行断言”，只生成计划；
- 账号/数据分级：匿名、普通用户、管理员、跨租户；
- 环境问题单独归类，不进入产品缺陷统计。

### 1.2 知识源导入 / 端点提取

**依赖**：代码路由、OpenAPI、Markdown 文档。

**问题**：
- 只能发现“代码/Spec/文档里存在”的端点；
- 运行时才存在、灰度开关、动态路由、网关层端点可能漏掉；
- 文档里的端点表可能过期，代码解析也可能漏掉动态注册。

**漏测风险**：
- 未登记接口完全没有覆盖；
- 过期端点产生假缺口；
- 文档没写但线上存在的接口不会被测试。

**建议**：
- 增加运行时发现：抓取流量/日志/网关路由/OpenAPI 探测；
- observed 端点与已知端点做差分，产生“未登记接口”风险项；
- 端点覆盖率区分 documented / runtime-discovered / unknown。

### 1.3 三方一致性

**依赖**：OpenAPI × 代码 × 文档。

**问题**：
- 项目只有一类来源时无法对照（unknown）；
- 文档角色不是 spec 时不计入契约，覆盖率为空是正常；
- 一致性只能发现“已知来源之间”的差异，不能发现运行时行为差异；
- 参数/响应 schema 的“缺失”经常被当成弱信号，长期挂着。

**漏测风险**：
- 运行时返回结构与文档不一致，但文档本身没写 schema，无法对照；
- 只有代码时，接口语义没有被验证；
- 一致性通过不代表行为正确。

**建议**：
- 增加 observed schema 基线：从真实响应推断结构，做漂移检测；
- contract gap 区分“文档缺失”与“实现偏离”；
- 对缺失 schema 的接口建立显式 policy/probe，而不是空白。

### 1.4 功能点提取

**依赖**：文档、OpenAPI、代码、LLM 结构化输出。

**问题**：
- 只能从已有素材抽功能点；
- 未文档化的用户旅程、业务规则、状态机可能漏掉；
- 代码里的分支/异常路径不一定会被抽象成功能点；
- 没有“业务领域常识”补全，例如电商的库存/优惠、SaaS 的租户隔离。

**漏测风险**：
- 功能覆盖率高，但真实用户路径没覆盖；
- 只有 CRUD 功能点，没有跨步骤业务流；
- 复杂状态机 / 权限矩阵缺失。

**建议**：
- 功能点来源分层：documented / code-derived / inferred / exploratory；
- 用通用领域知识生成“候选功能点”，标 heuristic，人工确认后入覆盖树；
- 从 UI/流量/issue tracker 补充用户旅程。

### 1.5 用例生成：功能

**依赖**：功能点、OpenAPI、文档、代码。

**问题**：
- 正常流优先，容易只覆盖 happy path；
- 业务结果断言依赖响应 schema/DB schema，缺 schema 时退化成状态码；
- 跨步骤业务流（创建→审批→支付→退款）不一定生成；
- 没有通用业务规则库。

**漏测风险**：
- 状态机跳转、权限变化、数据一致性、事务边界；
- 业务规则（金额、库存、额度、配额）没有 oracle；
- 多角色协作流程。

**建议**：
- 增加状态机/角色矩阵/业务规则模板；
- 从通用领域知识生成 heuristic 场景，triage 后升级；
- 用 metamorphic 关系补业务不变量（创建后必可查、更新后必反映）。

### 1.6 用例生成：边界

**依赖**：OpenAPI 的 minimum/maxLength/enum/required 等显式约束。

**问题**：
- 没有显式约束时直接输出 missing_input；
- 不生成通用边界：空字符串、null、unicode、超长、深层嵌套、类型混淆、重复提交、并发；
- 不覆盖协议层边界：错误 Content-Type、畸形 JSON、超大 body、慢请求。

**漏测风险**：
- 文档没写约束但代码实际会 500/崩溃的输入；
- 数值溢出、精度、时区、编码；
- 重复提交/并发导致的数据重复。

**建议**：
- 边界生成拆成 contract boundary + policy/fuzz boundary；
- 通用健壮性 policy：不得 5xx、不得泄露、不得接受空语义标识；
- 属性/模糊测试作为独立执行层，结果进 triage。

### 1.7 用例生成：安全

**依赖**：OpenAPI securitySchemes、文档、代码。

**问题**：
- 没有鉴权声明时，多数安全用例只能是 pending；
- 通用 OWASP 风险覆盖不足：IDOR、mass assignment、注入、SSRF、路径穿越、速率限制、敏感信息；
- 没有角色/租户矩阵，越权只能弱校验；
- 安全用例生成依赖文档，但安全往往最不该依赖文档。

**漏测风险**：
- 文档没写鉴权，但实际存在越权；
- 普通用户能访问管理员接口；
- 错误响应泄露 SQL/堆栈/DSN；
- 注入 payload 只测了少量固定样例。

**建议**：
- 引入安全 policy 包（OWASP ASVS 子集）；
- 角色/租户矩阵作为一等配置；
- 通用安全 probe（IDOR、注入、错误泄露）不依赖项目文档；
- policy 违反可判缺陷，heuristic 结果需 triage。

### 1.8 用例生成：性能

**依赖**：文档/项目 SLA；只做接口级响应时间基线。

**问题**：
- 没有 SLA 时只记录耗时或 missing_input；
- 没有并发、吞吐、压测、稳定性、资源泄漏、慢查询；
- 单次 P95 采样不能代表容量；
- 不覆盖数据库/外部依赖故障下的降级。

**漏测风险**：
- 高并发下的竞态、连接池耗尽、内存泄漏；
- N+1 查询、慢 SQL、锁竞争；
- 限流/熔断/重试行为。

**建议**：
- 性能分层：baseline（policy 默认预算）、load、stress、soak；
- 通用性能 policy：P95/P99 上限、错误率、资源增长；
- 没有 SLA 时使用“观测基线 + 退化检测”，而不是空白。

### 1.9 用例生成：Web / UI

**依赖**：文档/代码中的选择器、页面结构。

**问题**：
- 选择器由模型生成，UI 变更后易碎；
- 没有视觉回归、可访问性、响应式、浏览器兼容；
- 没有探索式点击/表单组合；
- 前端状态与后端契约的交叉验证不足。

**漏测风险**：
- 关键用户路径、错误提示、空状态、加载状态；
- 可访问性/键盘操作；
- 视觉回归和布局破坏。

**建议**：
- 选择器候选 + 自愈；
- 视觉/可访问性 policy；
- 探索式 UI Agent 产生 probe；
- 前端行为与 API 契约交叉验证。

### 1.10 断言 / Oracle 生成

**依赖**：OpenAPI/Doc/Code evidence；pending 机制。

**问题**：
- 无依据仍生成 pending 断言（已单独立项）；
- 业务结果断言依赖响应/DB schema；
- 通用 policy 没有正式 evidence 类型；
- observed 行为没有作为 oracle 基线。

**漏测风险**：
- 断言只验证状态码，不验证业务结果；
- 猜测断言被当成覆盖；
- 真实缺陷因为没有断言而漏掉。

**建议**：
- 见 `docs/test-asset-closure-dev-requests.md` 的 P0；
- 增加 policy/observed/probe；
- 覆盖率按断言强度加权。

### 1.11 测试数据 / 账号

**依赖**：schema 示例、`{{run_id}}` 前缀、项目账号配置。

**问题**：
- 数据模板主要来自 schema；缺 schema 时靠猜；
- 没有按业务语义生成数据（金额、库存、状态、时间）；
- 没有数据量/分布/并发/unicode/时区/locale；
- 账号缺失直接导致安全/权限用例缺失。

**漏测风险**：
- 真实数据规模下的分页/性能/精度问题；
- 特殊字符、emoji、RTL、超长文本；
- 多租户/多角色数据隔离。

**建议**：
- schema + 业务语义 + 通用数据策略三层；
- 数据量/分布可配置；
- 账号矩阵和数据隔离夹具。

### 1.12 执行 / 沙箱 / 清理 / flaky

**依赖**：本地/berth 沙箱、normalizer、cleanup、单次重跑。

**问题**：
- 本地沙箱无隔离，和生产差异大；
- normalizer 会自愈类型漂移，可能掩盖契约问题；
- flaky 只重跑一次；
- 并发/竞态/资源泄漏没有专门执行模式；
- cleanup 失败会污染后续运行。

**漏测风险**：
- 环境差异导致的假阴性/假阳性；
- 间歇性缺陷被 flaky 吞掉；
- 测试数据累积影响后续用例。

**建议**：
- 执行环境指纹和就绪检查；
- 重跑策略可配置（N 次/概率）；
- normalizer 自愈要记录并进入 contract gap；
- 并发/稳定性执行模式。

### 1.13 失败归因

**依赖**：LLM + 确定性 common-sense 信号 + 端点 schema + 代码/文档证据。

**问题**：
- 归因发生在“已经失败”之后；没有断言就不会有失败；
- common-sense 规则目前只覆盖少量（5xx、泄露、空标识、负数分页）；
- LLM 可能把用例问题误判为缺陷或反之；
- 归因依赖响应内容，UI/进程/性能失败可能信息不足。

**漏测风险**：
- 没有失败 = 没有 finding，即使行为可疑；
- 归因错误导致误报/漏报；
- 环境问题被当成产品缺陷。

**建议**：
- 失败归因 + probe 结果归因统一；
- 扩充 policy 规则库；
- 归因输出 confidence + evidence class，auto-confirm 仅限强信号；
- 对“无断言但可疑”的 observed 行为也进入 triage。

### 1.14 缺陷分诊 / 去重

**依赖**：AI 分组 + 人工确认 + 修复状态。

**问题**：
- 分诊基于已有 finding；没有 finding 的疑似行为不会进入；
- 分组可能把不同根因合并，或同一根因拆散；
- 人工误报/接受风险后，旧 triage verdict 可能残留（已做人工状态优先，但历史数据仍需清理）；
- 外部工单关闭不同步为回归验证（现在只同步展示）。

**漏测风险**：
- 真实缺陷被误标 false_positive；
- 重复缺陷干扰；
- 分诊结论与实际修复状态不一致。

**建议**：
- 分诊与 policy/heuristic 结果统一入口；
- 定期复核 false_positive（尤其 spec 变化后）；
- 工单状态同步但不替代回归验证。

### 1.15 白盒走查

**依赖**：代码切片 + LLM + 静态规则。

**问题**：
- 只能发现代码里能看出的风险；
- 对运行时配置、依赖、数据、并发、业务规则覆盖有限；
- 生成的建议用例可能缺少接口/输入，长期 pending；
- 没有 SAST/依赖/secret/config 扫描的体系化规则。

**漏测风险**：
- 配置错误、依赖漏洞、密钥泄露；
- 业务逻辑错误、状态机漏洞；
- 只在特定数据/并发下出现的缺陷。

**建议**：
- 接入 SAST/secret/dependency/config 扫描；
- 白盒 finding 自动生成 probe/policy 检查；
- 代码风险与运行时验证闭环。

### 1.16 外部验证（process / artifact / frontend / privileged / manual）

**依赖**：开发 Agent / 人工回写结果。

**问题**：
- 结果由被测方 Agent 自报，缺少独立复现证据；
- 命令/产物/哈希可伪造或过期；
- 失败时如果没有关联 finding，会生成新 finding（已补）；
- 成功时可能只凭自报把 finding 验证为 verified_fixed。

**漏测风险**：
- 假阳性关闭（自报通过但实际未修）；
- 外部验证结果与代码版本不一致；
- 人工验证缺乏审计。

**建议**：
- 要求 commit/构建产物 hash/命令/日志；
- Shakedown 侧尽可能独立重放；
- 外部验证结果带有效期和环境指纹；
- 回归验证优先，自报只作辅助。

### 1.17 覆盖率 / 报告 / 回测

**依赖**：功能点覆盖率、端点覆盖率、通过率、回测脚本。

**问题**：
- 覆盖率按“有正常流用例”统计，不按断言强度/风险类别；
- pending 断言可能让通过率虚高；
- 没有 mutation testing / oracle 质量评估；
- 回测只对已知 bug，不能证明未知缺陷覆盖；
- 没有“未覆盖风险类别”指标。

**漏测风险**：
- 100% 覆盖率但断言很弱；
- 高风险区域没有测试；
- 通过率好看但漏测严重。

**建议**：
- 覆盖率分层：功能 / 端点 / 断言强度 / 风险类别 / oracle 类型；
- 统计 pending 比例、policy 覆盖、heuristic 转化率；
- 引入 mutation testing 或断言强度评估；
- 回测持续跑，统计漏报/误报。

### 1.18 Spec / 代码 / 文档变更影响面

**依赖**：OpenAPI spec snapshot diff；代码/文档变更不触发用例复核。

**问题**：
- 只有 OpenAPI 重新导入会标记受影响用例；
- 代码/文档变更影响的用例不会自动 `needs_review`；
- 变更后是否重新评估取决于人工/API 调用；
- 旧快照/旧断言可能继续执行。

**漏测风险**：
- 行为变了但用例仍按旧契约执行；
- 新接口没有用例；
- 旧误报继续存在。

**建议**：
- 代码/文档变更也做影响面分析（端点/功能点/关键词/代码路径）；
- 变更后自动 preview reevaluate；
- 把“未复核变更”作为测试债。

### 1.19 MCP / 通知 / 交接

**依赖**：MCP pull + webhook/digest。

**问题**：
- MCP 是 pull，开发 Agent 不主动拉就无动作；
- webhook 需要项目配置，默认没有；
- 缺口/缺陷没有负责人 SLA（已有 owner/assignee，但无超时提醒）；
- 测试侧和执行侧的责任边界需要持续约束。

**漏测风险**：
- 新缺陷无人处理；
- 误报长期不复核；
- 测试债越积越多。

**建议**：
- 新事件 webhook + 定期 digest；
- 负责人/超时/SLA；
- 缺口 owner_kind 分流（已实现，需修分类 bug）；
- MCP 工具支持 claim / status / audit（已实现）。

## 2. 优先级建议

### P0
1. 无依据不生成断言，改生成 contract_question / probe；
2. policy 包（安全/可靠性基础规则）；
3. 边界/安全的通用 probe，不依赖项目文档；
4. 覆盖率按风险类别和 oracle 类型统计。

### P1
1. 变形/差分/属性测试；
2. 测试数据按业务语义和分布生成；
3. 运行时 observed schema / 流量发现；
4. 代码/文档变更影响面；
5. 外部验证独立证据。

### P2
1. 探索式 UI/业务 Agent；
2. 视觉/可访问性；
3. 性能 load/stress/soak；
4. mutation testing / 断言强度评估。

## 3. 结论

Shakedown 当前的主问题不是“某个环节忘了做”，而是**整个测试设计过度依赖项目知识库作为唯一 oracle**。
知识库是契约真相的来源，但不是全部测试判断的来源。正确方向是：

- contract 层保持严格；
- policy 层覆盖通用安全/可靠性；
- heuristic / exploratory 层扩大发现面；
- observed 层做漂移和回归；
- 所有结论按证据强度和可复现性分级，不把猜测当断言。

这样既能补覆盖，又不会用大量无依据断言制造误报。

## 4. 落地进度（2026-11-11）

| 优先级 | 项 | 状态 | 实现位置 |
|---|---|---|---|
| P0-1 | 无依据不生成断言，改生成 contract_question | 已完成 | `app/executor/case_schema.py`、`app/assertion_migration.py` |
| P0-2 | policy 包（5xx / 堆栈 / SQL / 未鉴权成功） | 已完成 | `app/policies.py` |
| P0-3 | 边界 / 安全通用 probe（不依赖文档） | 已完成（基础 body 边界） | `app/probe_generator.py` |
| P0-4 | 覆盖率按风险类别 + oracle 类型统计 | 已完成 | `app/risk_coverage.py`、`app/oracle_coverage.py` |
| P1-3 | 运行时 observed schema 漂移 | 已完成（结构漂移） | `app/observed_schema.py`、`GET /projects/{id}/observed/schema-drift` |
| P1-1 | 变形 / 差分 / 属性测试 | 已完成（HTTP 差分 / 变形 / fuzz / 属性执行器） | `app/exploratory.py`、`POST /projects/{id}/exploratory/run`、`tests/test_exploratory.py` |
| P1-2 | 测试数据按业务语义与分布生成 | 已完成（字段语义 + 分布 + 生成器） | `app/test_data.py`、`POST /projects/{id}/test-data/generate` |
| P1-4 | 代码 / 文档变更影响面 | 已完成（第一版：结构快照 + 三层影响） | `app/change_impact.py`、`GET /projects/{id}/change-impact` |
| P1-5 | 外部验证独立证据 | 已完成（Shakedown 独立验证 + 权限收口） | `app/case_verification.py`、`app/verification_evidence.py`、`app/worker/handlers.py`、`alembic/versions/b7c2e9f1a4d8_verification_evidence.py` |
| P2 | 探索式 Agent / 视觉可访问性 / 性能分层 / mutation | 部分完成（探索式 HTTP、视觉基线、a11y、性能分层、mutation 已落地；自主 UI 探索 Agent 未做） | `app/exploratory.py`、`app/visual.py`、`app/a11y.py`、`app/performance.py`、`app/assertion_quality.py` |

通用 probe 当前覆盖五类输入：`missing_auth`（匿名访问已声明鉴权的接口）、
`injection`、`path_traversal`、`boundary`（空值 / 类型混淆）、`fuzz`（Unicode /
超长），每接口最多 5 条（保证「鉴权 + 路径参数 + 请求体」的写接口不会挤掉 fuzz），
只挂 policy 断言，不带业务期望值。查询参数、路径参数的边界，畸形 JSON、错误
Content-Type、重复提交与并发仍未覆盖，属于后续增量。

observed 口径：
- 先按项目 `Endpoint` 的 method + path 模板匹配真实 URL（`/api/tasks/81` →
  `GET /api/tasks/{task_id}`），匹配不到落到 `unregistered` 桶；
- 只取最近 2000 条 `case_results`（倒序取再反转），不再截掉最新数据；
- 按「接口模板 + 状态码」分组，用最新一条对比上一条同类响应；空数组样本跳过，
  避免把空列表误报成结构缺失；
- `compared=0`、无漂移、无样本三种情况返回不同提示，不再统一说“需要两条样本”。

`POLICY-AUTH-001` 标记 `probe_only=True`，只用于 `missing_auth` probe，不进入
`policy_catalog()`，避免生成阶段把它挂到带鉴权头的用例上；其 `expected` 已补齐
全部 2xx 状态码（含 `204`）。

### 4.1 复验快照（项目 #11，只读）

口径固定为「项目 id + `case_results` id 区间 + 采样时间」，便于复现：

| 项 | 值 |
|---|---|
| 项目 | `11` Shakedown Self Test · Phase 1 |
| `case_results` | 3636 条，id `402 → 4038` |
| 采样时间 | `2026-11-06T10:24:38Z` |
| probe 计划（limit=40 接口） | 132 条：missing_auth 37 / injection 36 / path_traversal 35 / boundary 12 / fuzz 12 |
| observed | samples 1177 / endpoints 45 / compared 44 / drift 0 |
| oracle 覆盖 | cases 320、assertions 1233、contract 1222 / policy 8 / manual 3 / none 0、pending 0、probe_cases 0 |

覆盖率数字随执行数据变化，引用时需带上表的 id 区间与采样时间，否则不可复现。

### 4.2 P1-5 当前落地

**权限收口**：开发 Agent 只能报告，不能改验证状态。

- `report_case_verification` 只写 `case_verifications`，状态为
  `reported_passed / reported_failed`；不修改 `case.verification_status`、
  `case.verified_at`，也不修改 `finding.fix_status`。
- MCP 不再提供 `verify_finding_fix` 工具；开发 Agent 只能通过
  `report_finding_fix` 进入 `claimed_fixed`，通过 `report_case_verification`
  提交证据，不能直接写 `verified_fixed`。
- 本地人工接口仍可做人工复核，并在 `finding.fix_claim.verification` 记录
  `manual_override`。

**Shakedown 独立验证**：`handle_verify_fixes` 只按 Shakedown 自己的执行结果
判定，不再读取外部自报状态：

```text
Shakedown 执行 pass   → verified_fixed（写 replayed 证据）
Shakedown 执行 fail   → reopened
没有可执行用例        → needs_external_verification / needs_human_verification
```

`case_verifications` 保留 `evidence_level`、`commit_sha`、`artifact_hashes`、
`env_fingerprint`、`expires_at`，用于人工复核和审计；自报证据默认 7 天有效。

**历史结论**：验证是某个时间点、某个版本的事实。代码/环境变化后，不自动
回退旧 finding 的 `verified_fixed`；由 P1-4 的变更影响面标记/更新用例，新用例
产生新验证结论。旧验证仍可按 commit/环境指纹追溯。

**不做的事**：不执行开发 Agent 提交的任意命令；不因为目标环境变化自动重开
历史验证。前者是安全边界，后者会把历史结论和当前代码状态混在一起。

### 4.3 P1-4 变更影响面

代码 / 文档每次重新导入时，在 `KnowledgeSource.meta_json` 的私有字段
`_structure_snapshot` 保存结构快照，下一次导入时做结构 diff：

- 代码：路由 `METHOD path`、处理函数符号、函数/类内容 hash；格式化或行号漂移
  不产生变更。
- 文档：`file_path + section_path`、章节 hash、截断正文；用于章节级 diff。

影响面分三层：

1. **直接层**：路由新增/删除/处理函数变化，或文档章节变化直接匹配到
   `TestCase.steps` 的 `METHOD + path`；用例标记 `needs_review`。
2. **关联层**：通过 `Endpoint → Feature.endpoint_ids → TestCase.feature_id`
   或 `Feature.source_refs` 找到关联用例，标记 `needs_review` 并记录原因链。
3. **语义候选层**：说明类文档的业务语义变化不硬改状态，由 LLM（mock 环境用
   确定性关键词回退）生成 `change_candidates`，只作复核线索。

变更结果写入 `TestCase.spec.change_impact` / `change_impact_history`；
`GET /projects/{id}/change-impact` 可查看最近一次变更来源和受影响用例。
用例复核时可在详情页看到“受变更影响，需要复核”的原因；人工把用例启用
视为复核完成，会清掉待复核标记。

第一版不依赖 Git：结构快照按内容 hash 比对，Git diff 只作为后续可选的
“变化文件预筛选”优化，不作为 correctness 前提。当前代码符号到接口的关联
以同文件 ref 为主；跨文件 model/schema 依赖链留作后续增强。没有语法解析时
路由会退回 chunk hash 兜底，可能偏保守（宁可多标，不漏标）。

### 4.4 执行状态与批量重新评估

用例列表、缺口报告和 MCP 统一支持执行状态筛选：

- `never_executed`：从未产生 `case_results`
- `enabled_no_result`：enabled 但从未执行
- `pending_not_enabled`：pending，尚未审核启用
- `stale_after_source`：最后更新时间早于最近一次知识源解析
- `executed`：至少执行过一次

批量重新评估入口：

- `POST /projects/{id}/cases/enrich`（默认 `dry_run=true` 预览）
- MCP `request_case_enrich_batch`
- 可按缺口类型、owner、执行状态、case_ids 筛选；执行时入队 `enrich_case`
- 已有 queued/running 的同用例 enrich 任务会跳过，避免重复入队
- `reparse_source` 完成后返回 `reevaluate_candidates`（缺说明 / 缺步骤的用例数），
  只提示，不自动修改用例
