# 缺陷修复接入：GitHub Issue / Jira / MCP

缺陷修复清单支持三种交付方式：

1. **导出 Markdown / JSON**：不需要配置，适合直接把文件交给编码 AI。
2. **GitHub Issue / Jira**：在「系统设置 → 缺陷工单集成」配置后，从缺陷组直接建单。
3. **MCP**：让 OpenCode / Claude Code 等 Agent 直接查询缺陷、拿修复清单、创建工单。

---

## 1. GitHub Issue

**系统设置 → 缺陷工单凭据**（账号级）：

| 字段 | 说明 |
|---|---|
| `github_token` | Personal Access Token，至少需要目标仓库的 Issues 写权限 |
| `github_api_url` | 默认 `https://api.github.com`；GitHub Enterprise 改成自己的 API 地址 |

**项目设置 → 工单集成**（项目级）：

| 字段 | 说明 |
|---|---|
| `github_repo` | `owner/repo`，例如 `zsgh/mymac`；留空回退全局默认值 |
| `github_labels` | 可选，逗号分隔；留空表示不附加标签 |

项目级集成状态可查询：

```http
GET /api/projects/{project_id}/findings/integrations/status
```

配置后，在「缺陷」页展开任一 AI 修复组，点击「建工单」：

- 先选择 `GitHub Issue`
- 弹窗会预览工单标题和正文
- 确认创建后会打开新 Issue
- 同一个修复组重复点击会返回已有 Issue，不会重复建单

工单正文包含：根因判断、涉及文件、原始 finding ID、证据、复现命令、验收标准。

---

## 2. Jira

**系统设置 → 缺陷工单凭据**（账号级）：

| 字段 | 说明 |
|---|---|
| `jira_base_url` | 例如 `https://your-team.atlassian.net` |
| `jira_email` | Jira 登录邮箱 |
| `jira_api_token` | Jira API Token |

**项目设置 → 工单集成**（项目级）：

| 字段 | 说明 |
|---|---|
| `jira_project_key` | 项目 Key，例如 `SEC`；留空回退全局默认值 |
| `jira_issue_type` | 工单类型，默认 `Bug`，需要与项目工作流匹配 |

创建流程与 GitHub 相同，在修复组的「建工单」里切换到 `Jira 工单`。

---

## 3. 导出接口

```http
GET /api/projects/{project_id}/findings/export.md
GET /api/projects/{project_id}/findings/export.json
```

常用参数：

```http
?verdict=real_bug
&severity=high
&status=confirmed
&source=whitebox
&q=进程
```

`verdict`：

- `actionable`：默认，建议修复 + 需验证
- `real_bug`
- `needs_verification`
- `false_positive`
- `duplicate`
- `untriaged`
- `all`

敏感值（Authorization / Cookie / token / apiKey）会脱敏。

---

## 4. MCP

Shakedown 提供两种 MCP 接入方式：

### 4.1 原生 Streamable HTTP（推荐给远程 Agent）

```text
http://<shakedown-host>:8000/mcp/
Authorization: Bearer mcp_...
```

1. 打开「系统设置 → MCP HTTP 客户端」；
2. 填写客户端名称；
3. 选择权限：
   - `read`：列项目、读缺陷、拿修复清单和契约缺口报告；
   - `write`：额外允许触发执行、重新解析知识源、重算一致性、创建 GitHub Issue / Jira 工单；
4. 创建后复制一次性的 `mcp_...` Token。

OpenCode 远程配置示例：

```json
{
  "mcp": {
    "shakedown": {
      "type": "remote",
      "url": "http://your-shakedown-host:8000/mcp/",
      "headers": {
        "Authorization": "Bearer mcp_你的Token"
      }
    }
  }
}
```

服务端配置：

```env
SHAKEDOWN_MCP_REQUIRE_AUTH=true
SHAKEDOWN_MCP_ALLOWED_HOSTS=127.0.0.1,localhost,your-shakedown-host
```

如果走反向代理，把代理域名加入 `SHAKEDOWN_MCP_ALLOWED_HOSTS`，并在代理层再做一次认证/限流。不要直接把 8000 暴露公网。

### 4.2 本地 stdio（适合单机 OpenCode / Claude Code）

```bash
SHAKEDOWN_API_URL=http://127.0.0.1:8000 \
  .venv/bin/python scripts/shakedown_mcp.py
```

OpenCode：

```json
{
  "mcp": {
    "shakedown": {
      "type": "local",
      "command": [
        "/path/to/shakedown/.venv/bin/python",
        "/path/to/shakedown/scripts/shakedown_mcp.py"
      ],
      "environment": {
        "SHAKEDOWN_API_URL": "http://127.0.0.1:8000"
      }
    }
  }
}
```

### 工具

| 工具 | 权限 | 用途 |
|---|---|---|
| `list_projects` | read | 列出可访问项目 |
| `list_members` | read | 查看项目角色矩阵（owner / maintainer / viewer） |
| `list_accounts` | read | 查看测试账号矩阵（账号名 / 字段名，不含密码） |
| `list_sources` | read | 查看知识源和解析状态 |
| `get_consistency_report` | read | 查看 OpenAPI / 代码 / 文档一致性 |
| `list_case_gaps` | read | 汇总待确认断言 / 无步骤 / 待补全说明；可按 `execution_state` 过滤 |
| `get_case_gap_report` | read | 获取契约缺口 Markdown 报告，交给被测系统维护者 |
| `list_cases` | read | 列出项目用例，可按状态 / 维度 / 关键词 / 执行状态过滤 |
| `get_case` | read | 读取单条用例的完整 spec、步骤和断言 |
| `get_run` | read | 读取一次执行的状态和汇总 |
| `list_run_results` | read | 读取逐用例结果，可按失败/错误过滤并带请求响应 |
| `start_run` | write | 触发项目执行（可指定用例或维度） |
| `rerun_failed` | write | 重跑一次执行中的失败/错误用例 |
| `reparse_source` | write | 被测系统文档更新后重新解析知识源（只更新 Shakedown 副本） |
| `run_consistency_check` | write | 重新计算 OpenAPI / 代码 / 文档一致性 |
| `request_case_enrich` | write | 让 Shakedown 重新评估单条用例（只改用例资产） |
| `request_case_enrich_batch` | write | 按缺口 / 执行状态批量重新评估用例；默认 dry-run 预览 |
| `list_audit` | read | 查看项目审计时间线（反馈 / 例外 / webhook / trace） |
| `list_exceptions` / `update_exception` | read/write | 查看 / 处置无人值守例外队列 |
| `get_automation_health` | read | 查看最近 run、队列、例外、SLA、执行状态摘要 |
| `trigger_automation_run` | write | 按运行策略触发自动化 run（部署 / CI 回调） |
| `get_automation_gate` | read | CI gate：把 run 收敛成 pass/fail 和阻断项 |
| `verify_artifacts` | read | Shakedown 侧读取产物并计算 SHA-256 |
| `environment_action` | write | 启停项目配置的被测环境（需显式允许） |
| `list_verification_commands` / `run_verification_command` | read/write | 执行项目白名单进程/权限/产物验证命令 |
| `generate_test_data` | write | 按字段语义生成测试数据 |
| `run_performance` | write | 执行 load / stress / soak 性能采样 |
| `list_visual_baselines` / `set_visual_baseline` / `compare_visual_baseline` | read/write | 视觉基线管理与比较 |
| `get_case_a11y` / `audit_a11y` | read | 读取用例 a11y 指标并做基础 WCAG 规则审计 |
| `set_case_gap_status` | write | 处置契约缺口：认领 / 已修复待复检 / 接受风险 / 不修复 / 重新打开 |
| `set_finding_status` | write | 处置缺陷：确认 / 误报 / 接受风险 / 不修复 / 重新打开，并可指派负责人 |
| `sync_external_issues` | write | 同步 GitHub / Jira 工单状态（只同步展示，不代替回归验证） |
| `list_finding_groups` | read | 列出 AI 分诊后的修复组 |
| `get_finding` | read | 读取单条 finding 的完整证据、代码位置、复现 |
| `claim_finding` | write | 认领修复工作，状态进入 in_progress，避免重复处理 |
| `get_fix_plan` | read | 获取 Markdown 修复清单 |
| `create_github_issue` | write | 为一个修复组创建 GitHub Issue |
| `create_jira_issue` | write | 为一个修复组创建 Jira 工单 |
| `report_finding_fix` | write | 提交修复声明（commit / PR / 文件 / 测试结果），进入待验证 |
| `report_case_verification` | write | 提交用例验证报告（构建产物 / 进程 / 浏览器 / 权限 / 人工），只作证据 |

### 契约缺口闭环

被测系统的文档 / OpenAPI 不完整时，Shakedown 只生成缺口报告，不修改被测系统。被测系统的编码 Agent 可以通过 MCP 自助处理：

```text
list_case_gaps / get_case_gap_report
  → 在被测系统自己的仓库里补 schema / 文档 / 代码
  → list_sources（找到知识源）
  → reparse_source（让 Shakedown 重新解析）
  → run_consistency_check（重算三方一致性）
  → request_case_enrich（可选：让 Shakedown 重新评估受影响用例）
  → start_run + list_run_results（回归验证）
```

缺口也有处置状态：`open / claimed / fixed / accepted / wont_fix`，可记录负责人和备注。
`accepted` / `wont_fix` 会从默认缺口列表隐藏，但保留审计记录；`fixed` 表示已修待复检。

边界：

- `reparse_source` / `run_consistency_check` / `request_case_enrich` / `request_case_enrich_batch` 只更新 Shakedown 自己的知识副本和用例资产；
- 被测系统的文档、OpenAPI、代码由被测系统维护者或他们授权、在其仓库中工作的 Agent 修改；
- Shakedown 不会自动提交、推送或写回被测系统。

### 修复生命周期

Agent 修复后不能直接把缺陷改成“已修复”，而是：

```text
open
  → in_progress（Agent 认领，避免重复处理）
  → claimed_fixed（Agent 提交修复声明，等待验证）
  → verified_fixed（Shakedown 自己执行通过）
  → reopened（Shakedown 执行失败）

开发 Agent 的 report_case_verification 不进入这条状态机，
只写入 case_verifications 作为证据。
```

没有可执行用例时进入 `needs_human_verification` / `needs_external_verification`，
由人工或后续补的用例处置。

`report_finding_fix` 至少需要 commit、PR、测试结果或修复说明之一；平台会记录客户端身份和完整修复历史。缺陷详情里可以查看修复声明，验证通过 / 失败只由 Shakedown 的回归执行或本地人工复核写入。

### 缺陷处置

除修复生命周期外，缺陷支持最终处置：

```text
pending / confirmed / false_positive / accepted / wont_fix
```

- `accepted`：已知问题，接受风险，不再进入 actionable 修复清单；
- `wont_fix`：明确不修复；
- 两种处置都可记录负责人、备注和时间，并写入 `human_feedbacks` 审计；
- 外部工单状态可调用 `sync_external_issues` 同步展示，但工单关闭不等于回归验证通过。

### 误报复核（开发 Agent 自助）

误报不是“删除”，开发 Agent 可以通过 MCP 拉取并复核：

```text
list_finding_groups(project_id=11, verdict="false_positive")
  → get_finding(finding_id) 看证据 / 代码位置 / 复现
  → 认为确实是误报：set_finding_status(finding_id, status="false_positive", note="复核通过")
  → 认为不是误报：set_finding_status(finding_id, status="confirmed", note="复现了，重新打开")
```

也可以直接拉 Markdown：

```text
get_fix_plan(project_id=11, verdict="false_positive")
```

只读 scope 足够查看和复核；改状态需要 write scope。开发 Agent 不需要改 Shakedown 的数据结构，只做“确认 / 重新打开”。

### 外部用例验证

用例支持多种验证方式：

```text
api           API 黑盒执行
process       进程 / 信号 / 服务生命周期
artifact      构建产物 / 图片 / 文件
frontend      浏览器 / 前端行为
privileged    权限 / Keychain / root
manual        只能人工
```

构建产物、进程、浏览器等无法通过 API 直接跑的场景，由开发 Agent 执行检查后调用：

```text
report_case_verification
```

例如：

```json
{
  "case_id": 3764,
  "passed": true,
  "mode": "artifact",
  "command": "go run ./tools/genicon && identify apple-touch-icon.png",
  "result": "180x180，内容与整图缩放一致",
  "artifacts": ["apple-touch-icon.png"],
  "note": "构建产物检查通过"
}
```

开发 Agent 只能**报告**，不能改验证状态：

- `report_finding_fix` 把 finding 置为 `claimed_fixed`，表示“我修了，等待验证”；
- `report_case_verification` 只写 `case_verifications`（状态 `reported_passed / reported_failed`），保留命令、结果、产物、commit、环境指纹和有效期；
- 只有 Shakedown 自己执行用例通过，才写 `verified_fixed`；执行失败写 `reopened`；
- MCP 不提供直接验证 finding 的工具。开发 Agent 不能调用任何入口把 finding 改成 `verified_fixed`。

请求可附带证据字段（都会写入 `case_verifications`）：

| 字段 | 说明 |
|---|---|
| `commit_sha` | 修复所在提交 |
| `artifact_hashes` | `{文件名: hash}`，客户端上报值只作对照 |
| `env_fingerprint` | 环境指纹，如 `{"base_url": "...", "runner_version": "..."}` |
| `expires_at` | 报告有效期（ISO8601）；不传时默认 7 天 |

`report_case_verification` 写入 `case_verifications` 时 `evidence_level=self_reported`；
Shakedown 独立重放会另写一条 `evidence_level=replayed` 的记录，`finding_fixes`
通过 `verification_id` 关联到真正用于验证的那条证据。

验证是某个时间点、某个版本的结论。代码或环境变化后，Shakedown 通过更新/新建
用例重新验证，不会回头改写旧 finding 的 `verified_fixed`。旧记录仍可按
commit 和环境指纹追溯。

MCP client 支持 `project_ids` allowlist：为空表示不限制；非空时只能访问指定项目。
还可传 `tenant`（只能访问同 tenant 项目）和 `actor`（绑定 `ProjectMember.actor`，
用于 owner / maintainer / viewer 角色判定；留空时用 client name 匹配）。
创建 client 时传入：

```json
{
  "name": "dev-agent",
  "scopes": ["read", "write"],
  "project_ids": [11],
  "tenant": "team-a",
  "actor": "qa-lead"
}
```

角色规则：`viewer` 只读，`maintainer` 可写，`owner` 可管理成员；显式绑定 `actor`
但项目里没有对应成员会被拒绝。没有绑定 actor 且没有成员记录时按 token scope 推导
（read→viewer，write→maintainer），保持旧 client 兼容。

注意：stdio 模式不经过 HTTP Token，默认拥有完整权限；只适合本机受信任进程。跨机器访问请使用原生 HTTP + Token。
