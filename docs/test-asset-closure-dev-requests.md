# 测试资产闭环：Shakedown 开发需求清单

> 背景：自测项目 #11 的开发 Agent 已完成代码侧闭环（4 条 `verified_fixed`，
> `get_fix_plan(actionable) = 0`，最近执行全绿）。剩余缺口属于测试侧资产，
> 不应由被测系统开发 Agent 手工处理，也不应靠人工直接改库来“抹平”。
> 下面是 Shakedown 产品需要补齐的能力，交给 Shakedown 开发 Agent 实现。

## 事实基线（项目 #11）

- 13 条 finding：4 条 `confirmed + verified_fixed`，9 条历史 `false_positive`。
- `actionable` 修复清单为 0。
- OpenAPI 源重新解析 diff 为 0；三方一致性 96 个接口、0 个 inconsistent。
- 契约缺口页：247 条用例有缺口，320 条待确认断言，92 条缺输入，7 条无步骤。
- 缺输入分类：SLA/性能阈值 29、响应 schema 27、鉴权语义 14、接口/参数 8、其他 14。

## P0：批量缺口处置

**问题**：缺口页目前只能逐条处置，247 条用例无法作为测试资产批量治理。

**期望**：
- 支持按筛选条件（缺口类型 / 维度 / 功能点 / 证据来源 / 负责人）批量设置处置状态。
- 支持 `open / claimed / fixed / accepted / wont_fix`，可批量填写负责人和备注。
- 操作必须二次确认，并显示将影响的用例数量。
- 保留每条用例的处置审计（谁、何时、原因）。

**验收**：
- 能一次把“SLA 缺失”类缺口批量标为 `accepted` 或 `claimed`；
- 默认列表隐藏 `accepted / wont_fix`，可切换查看；
- 批量操作写入 `case_gap_states`，不修改用例 spec。

## P0：按项目 SLA 补齐性能用例

**问题**：项目 #11 已配置 `performance_sla_ms=100`，仍有 29 条用例的缺输入说明是“没有 SLA”。
这些是旧用例未吃到项目配置，属于 Shakedown 自身遗漏。

**期望**：
- 生成/重新生成性能维度时，明确使用项目设置里的 SLA 兜底。
- 对已有性能用例提供“按当前 SLA 重算 timing 断言”的能力。
- 重算前显示受影响用例数和旧断言，不静默覆盖。

**验收**：
- 项目配置 SLA 后，重新生成/重算的性能用例不再以“缺少 SLA”作为缺输入原因；
- 没有 SLA 的项目仍保持缺失输入，不硬编阈值。

## P1：Spec 变更后的用例重评估

**问题**：OpenAPI 重新导入会产生 spec diff 并把受影响用例置为 `needs_review`，
但目前不会根据新快照重新评估断言，也不会自动消解对应缺口。

**期望**：
- spec diff 后提供“重评估受影响用例”动作，可预览数量和范围。
- 重评估使用新 endpoint schema，只处理受影响用例，支持失败回滚/跳过。
- 重评估完成后，能确认的 pending 断言自动变为 confirmed；仍不确定的保留 pending。

**验收**：
- 新旧 spec 只改 422 响应结构时，只重评估引用受影响接口的用例；
- 不重新生成整个项目的用例，不产生重复用例。

## P1：待确认断言的分组复核

**问题**：320 条 pending 断言目前只能在用例详情里逐条看；缺少按接口/证据来源的聚合视图。

**期望**：
- 按 endpoint、断言类型、证据来源、功能点聚合；
- 支持批量确认（有 openapi/code/doc 依据）和批量接受风险；
- 确认/接受操作写入审计，不直接修改被测系统。

**验收**：
- 能按 `GET /api/...` 聚合出该接口下所有 pending 断言；
- 有依据的断言可批量确认，manual/无依据的必须人工逐条处理或接受风险。

## P1：测试债 vs 被测方契约缺口分流

**问题**：缺口页把“测试资产没补”和“被测系统契约没定义”混在一起。
开发 Agent 不应该接手测试资产；测试 Agent 也不应该替被测方定义契约。

**期望**：
- 缺口增加 `owner` 分类：`test-side` / `sut-contract` / `environment`。
- 只有 `sut-contract` 会进入给被测系统 Agent 的 MCP 报告；
- `test-side` 缺口留在 Shakedown 内部，由测试侧 Agent/人处理。

**验收**：
- SLA 缺失、无步骤、用例预期错误 → 默认 `test-side`；
- spec 未定义响应 schema / 鉴权语义 → 默认 `sut-contract`；
- MCP 的 `get_case_gap_report` 不把 test-side 缺口混进 SUT 修复清单。

## P2：误报复核工作流

**问题**：9 条历史误报已 `false_positive`，但没有“为什么误报”的审计，也没有 spec 修复后的批量复核入口。

**期望**：
- 误报必须能记录 `resolution.note`（旧数据可显示“未填写原因”）；
- 支持按 spec 快照变化筛选“可能需要重新打开的误报”；
- MCP 已支持 `verdict=false_positive` 拉取，前端提供对应说明和入口。

**验收**：
- 打开误报详情能看到误报原因/负责人；
- 开发 Agent 可通过 MCP 拉取误报清单，并用 `set_finding_status` 重新打开。

## P2：主动触发（而不是只能 pull）

**问题**：MCP 是 pull；被测系统 Agent 不主动调用时，新的 actionable finding / 契约缺口不会自动送达。

**期望**（任选其一或组合）：
- 项目级 webhook：新 finding / 新缺口 / 状态变更时 POST 事件；
- 定期 digest：按项目输出 actionable + 契约缺口摘要；
- 与 GitHub / Jira 工单联动，由工单系统触发开发 Agent。

**约束**：
- 事件只包含 Shakedown 自己的数据，不写回被测系统；
- 推送失败可重试，不阻塞执行主流程。

## 明确不做

- 不替 Shakedown 手工批量改测试数据（应由上述产品能力承接）。
- 不把测试资产缺口转成被测系统的代码修复任务。
- 不让 Shakedown 自动修改被测系统的 OpenAPI / 文档 / 代码。

---

## 验收记录（2026-11-06）

### 已验证可用

- 182 个后端测试通过，前端构建通过，迁移在 `a8d3f1c5b7e9` head。
- `GET/POST /projects/{id}/cases/sla-recompute` 可用：
  - 预览 22 条受影响性能用例；
  - 执行后 `applied=22`，再次预览为 0。
- `GET/POST /projects/{id}/cases/reevaluate` 可用：当前快照 diff 为 0，返回 0 条受影响用例。
- `GET /projects/{id}/case-gaps/assertions/groups` 可用：
  - 318 条 pending 断言中 271 条有 openapi/code/doc 依据；
  - 批量确认 271 条，全部成功。
- `GET /projects/{id}/case-gaps/batch`、审计、`owner_kind` 筛选、SUT 报告导出均可用。
- webhook / digest 有单元测试覆盖，投递失败不影响主流程。

### 收口结果（项目 #11）

```text
缺口用例：247 → 100
待确认断言：320 → 47（剩下全部是 manual，没有可引用依据）
缺输入：92 → 70
无步骤：7 → 2
全部归属：test-side 40 / sut-contract 60
```

### 验收发现：需要 Shakedown 开发 Agent 修的分类问题

**问题 1：`missing_input` 分类会把“顺带提到 SLA”误判成 SLA 缺口**

`classify_missing_input()` 先匹配 `_SLA_MARKERS`，导致下面这类文本被归为 `sla`，
但实际主因是响应 schema / 健康检查语义：

- #4082 `缺少 name/base_url/description 的 maxLength 定义……`
- #4035 `……无 SLA 故未生成性能基线用例`
- #4033 / #4029 / #3884 / #3885 / #3886 / #3887 / #4061

结果：9 条非 performance 用例在 `by_missing_input_kind` 里显示为 `sla`，
但它们不会进入 SLA 重算（重算只处理 `dimension=performance`），形成死分类。

建议：分类前先看结构化上下文（dimension、断言 kind、用例 steps），或者让模型输出
结构化的 `missing_input_kind`，不要从自由文本里先命中“SLA”关键词。

**问题 2：`no_steps` 优先级把“鉴权契约缺失”归成测试侧**

`_primary_owner_kind()` 在 `no_steps` 时直接返回 `test-side`，导致：

- #3850 `[安全] 任务详情获取 - 鉴权/越权访问覆盖缺失`
- #3792 `[安全] 未鉴权访问用例列表`

这两条没有步骤的原因是 OpenAPI 没有 securitySchemes / 鉴权语义，
应该出现在 `sut-contract` 报告里，而不是被当作测试侧无步骤占位。

建议：`no_steps` 与 `auth/response_schema/api_param` 同时存在时，允许一条用例有多个
owner_kind，并在导出时按“主归属”判断；主归属不应无条件偏向 test-side。

**复验标准**

- `by_missing_input_kind` 中不再有非 performance 用例被归为 `sla`；
- #3850 / #3792 出现在 `owner_kind=sut-contract` 的 gap report；
- 其余收口数据保持不变。

---

## 追加 P0：无依据时不应该生成断言

### 现象

当前生成链路允许“没有依据的断言”以 `status=pending` 的形式落库：

- `_CASE_SYSTEM_PROMPT` 第 5/6 条规定：没有依据的断言标 `pending`，不要判失败；
- `_normalize_assertion()` 在 evidence 为 `none` 时只把状态改成 `pending`，**不删除断言**；
- 运行时会记录 pending 断言但不判失败。

结果就是：OpenAPI/Doc/Code 都没有依据时，系统仍然生成了一个具体 expected 的断言
（例如 `text_contains` / `json_path`），再把它标成“待确认”。这在产品语义上是错的：
**待确认的猜测不是断言，是问题/契约缺口。**

项目 #11 目前剩下 47 条 manual pending 断言，典型例子：

- “跨项目读取任务详情应被拒绝；文档未定义”
- “未鉴权访问应被拒绝；文档未定义”
- “created_at 应为 ISO8601；文档未定义”
- “重复取消是否幂等返回 200；文档未约定”

这些不是可以从契约推导出的断言，而是需要被测方回答的问题。

### 期望

1. **无依据不生成断言**
   - 如果一条预期没有 openapi / doc / code / 明确 policy 依据，不要生成具体断言；
   - 改为生成一个 `contract_question` / `gap`：说明想验证什么、缺什么契约、需要谁确认；
   - 该问题不进入断言执行链，也不计入 pending assertion。

2. **pending 只保留“有明确假设来源”的断言**
   - 人工输入的假设可以 pending，但必须记录作者、理由、待确认点；
   - 通用安全策略（如“错误响应不得泄露堆栈”）应使用显式 `policy` 依据，
     而不是伪装成 `manual` 或 `none`。

3. **缺口页区分“待确认断言”和“待确认问题”**
   - 断言：有 expected、有依据/假设，执行时只记录不失败；
   - 问题：没有 expected，只有待确认事项，不进入断言统计。

4. **存量 47 条 manual pending 断言迁移**
   - 有 policy 依据的（不得 5xx、不得泄露堆栈等）→ 转成 policy 断言或安全策略检查；
   - 没有依据的（401/403、404、ISO8601、幂等语义等）→ 转成 contract_question / gap；
   - 不要继续以“断言”形式挂在用例里。

### 验收

- 新增生成的用例中，`evidence.source_type in {none, manual}` 且没有明确理由的断言数为 0；
- 缺口页断言总数不再包含纯问题项；
- 存量迁移后，项目 #11 的 `pending_assertions` 要么有可引用依据，要么被转成问题/接受风险；
- 运行时和导出报告不再把“猜测”展示成断言。

---

## 追加 P0：契约之外的通用测试智能层

### 问题

当前用例生成和断言主要依赖项目知识库（OpenAPI / Doc / Code）。知识库不完整、不可靠时：

- 只测“文档写了的”行为，会漏掉文档没写但真实存在的缺陷；
- 通用安全问题（越权、注入、信息泄露、5xx、幂等、边界）不一定有项目契约；
- 大模型的通用世界知识目前只用于失败归因和白盒走查，没有进入用例生成/探索式测试。

但也不能因此把“通用猜测”直接变成硬断言，否则会制造大量误报。需要分层。

### 建议模型：按 oracle 来源分层

| 层 | 来源 | 用途 | 失败处理 |
|---|---|---|---|
| `contract` | OpenAPI / Doc / Code | 确定性契约断言 | 可判失败 |
| `policy` | 显式安全/可靠性策略包 | 不得 5xx、不得泄露堆栈/SQL、空标识、负数分页、鉴权隔离等 | 可判失败（策略需版本化、可审计） |
| `heuristic` | 通用模型知识 + 风险启发式 | 生成探针/候选缺陷，不生成硬断言 | 只记录，进入 triage |
| `exploratory` | 探索式 / 变形 / 差分测试 | 发现未知行为差异 | 候选 finding，人工/Agent 确认 |

关键：**不是所有测试都需要项目契约，但不同 oracle 的结论强度必须不同。**

### 建议能力

1. **Probe / 探针用例**
   - 可以没有具体 expected；
   - 执行请求/流程后，由归因 Agent 结合 policy + common sense 判断是否产生候选缺陷；
   - 不进入 pending assertion 统计。

2. **Policy 包**
   - 显式策略：不得 5xx、不得泄露堆栈/SQL、错误响应不得包含敏感字段、空语义标识不得创建成功、负数分页不得进 DB、跨项目不得越权；
   - 每条 policy 有 ID、版本、适用范围、证据；
   - policy 违反可以直接产生 finding，但必须可追溯到 policy 定义。

3. **变形 / 差分测试**
   - 创建后列表能查到、更新后详情能读到、删除后详情 404；
   - 分页不重不漏、排序生效、过滤结果都属于全集；
   - A 角色/B 项目不能读到对方数据；
   - list 与 detail、API 与 DB、OpenAPI 与运行时一致性。

4. **属性 / 模糊测试**
   - 非法类型、缺字段、超长、空值、unicode、重复提交、并发；
   - 即使没有 schema，也可以用通用健壮性策略判断“不得 5xx / 不得泄露”。

5. **探索式 Agent**
   - 用通用模型知识提出高风险路径和输入；
   - 只产出 probe + candidate finding，不能直接判缺陷；
   - 结果需要 triage 或人工确认。

6. **覆盖率口径**
   - 除“文档断言覆盖率”外，增加“风险类别覆盖率”：鉴权、注入、错误处理、幂等、并发、分页、隔离、敏感信息等；
   - 未覆盖的风险类别应显示为测试债，而不是沉默。

### 验收

- 无项目契约时，系统仍能生成 probe / policy 检查，而不是只生成“无依据断言”或空白；
- heuristic 结果不会直接判失败/确认 finding；
- policy 违规可稳定复现并带 policy ID；
- 覆盖率页能区分 contract coverage 与 risk coverage；
- 存量 47 条 manual pending 断言中，属于 policy 的迁移为 policy 检查，属于问题的迁移为 contract_question。

---

## 第二轮验收（2026-11-06）：通用测试智能层

### 已验证可用

- 194 个后端测试通过，前端构建通过，迁移在 `a8d3f1c5b7e9` head。
- 两个上轮分类 bug 已修复：
  - 非 performance 用例不再被误归为 `sla`；
  - `#3850` / `#3792` 已进入 `sut-contract`。
- 无依据断言迁移可用：
  - preview：34 条用例 / 47 条断言；
  - apply：8 条转 `policy`，39 条转 `contract_question`；
  - 迁移后 `pending_assertions = 0`，`contract_questions = 39`。
- 通用 probe 生成可用：
  - `GET /projects/{id}/probes/generate` 能生成带 policy 依据的 probe；
  - probe 无业务 expected，只挂 policy 检查，符合设计。
- 覆盖率分层可用：
  - oracle mix 能区分 contract / policy / manual / none；
  - risk coverage 有独立视图。

### 本轮验收发现的问题

**问题 1：`_has_security` 把 `required=false` 当成了需要鉴权**

`GET /api/projects` 的 endpoint security 是：

```json
{"schemes": [], "required": false}
```

但 `probe_generator._has_security()` 只判断 `bool(security)`，于是生成了
“[Probe] GET /api/projects · 未携带鉴权头访问”，并挂上 POLICY-AUTH-001。
这会在公开接口上制造假缺陷。

期望：只有 `security.required == true` / `schemes` 非空时才生成 missing_auth probe。
已删除这条错误 probe（case 4533），但生成器需要修。

**问题 2：probe 默认会生成破坏性请求**

`limit=10` 的 preview 已经包含：

- `DELETE /api/projects/{project_id}` 的未鉴权 probe
- PATCH / DELETE 的注入 / 路径穿越 probe

如果直接批量启用执行，可能删除测试项目或修改配置。

期望：
- 默认只生成只读 / 安全方法（GET/HEAD/OPTIONS）的 probe；
- DELETE/PUT/PATCH 需要显式 opt-in，并使用专用测试数据；
- 对路径参数使用安全的占位 ID，不指向真实项目。

**问题 3：risk coverage 被关键词/维度高估**

当前项目 #11 显示 risk coverage = 100%，但：

- `probe_cases = 0`
- 9 类风险里多数只有 1–7 条 policy 检查
- 判定逻辑是 `dimension == security` 或标题/描述/问题文本里出现关键词就覆盖

这会把“提到过风险”误当成“有可判定覆盖”。

期望：
- risk coverage 只统计：
  - 有 policy 断言的用例；
  - 有 probe 实际执行的用例；
  - 有 confirmed contract 断言且直接涉及该风险的用例；
- 标题/描述/contract_question 只能算“已识别”，不能算“已覆盖”；
- 区分 `identified` / `covered` / `verified` 三档。

### 复验标准

- 公开接口不生成 missing_auth probe；
- 默认 probe 不包含破坏性方法；
- risk coverage 在 probe 未执行时不会显示 100%；
- 其余迁移/oracle 统计保持现状。
