# 无人值守自动化：调度、环境就绪与运行策略

## 1. 调度

`project_schedules` 保存项目级定时任务。worker 启动时同时启动调度线程（`SHAKEDOWN_SCHEDULER_ENABLED=true`，默认开启），扫描到期 schedule 并创建 run。

### API

```http
GET    /api/projects/{project_id}/schedules
POST   /api/projects/{project_id}/schedules
GET    /api/schedules/{schedule_id}
PATCH  /api/schedules/{schedule_id}
DELETE /api/schedules/{schedule_id}
POST   /api/schedules/{schedule_id}/run-now
GET    /api/projects/{project_id}/readiness
```

创建示例：

```json
{
  "name": "夜间全量回归",
  "enabled": true,
  "interval_seconds": 86400,
  "policy": {
    "mode": "full",
    "dimensions": ["functional", "security", "boundary"],
    "max_cases": 200
  }
}
```

`interval_seconds` 最小 60 秒。`run-now` 与调度线程使用同一套环境和运行策略。

## 2. 环境就绪

调度触发前会先做 readiness 检查：

- `base_url` 必填；
- 未显式配置时依次尝试 `GET {base_url}/health`、`/api/health`、`/`；
- 可通过项目 `settings` 覆盖：
  - `readiness_path`：显式指定后只请求该路径
  - `readiness_expected_status`：默认 `[200, 204]`
  - `readiness_timeout`：默认 5 秒
  - `readiness_headers`：自定义请求头
  - `readiness_probes`：多探针列表，每项支持 `method/path/expected_status/headers/timeout`

环境不可达时：

- 创建一个 `failed` run；
- `error` 写“环境未就绪：…”，不生成产品缺陷；
- 如果配置了 webhook，入队 `environment_unavailable` 事件；
- schedule 的 `last_status=environment_unavailable`。

## 3. 运行策略

```json
{
  "mode": "full | smoke | impacted",
  "dimensions": ["functional"],
  "max_cases": 200,
  "priority_max": 50,
  "tags": ["smoke"],
  "smoke_priority": 50,
  "smoke_tags": ["smoke"],
  "smoke_fallback_limit": 20
}
```

- `full`：全部 enabled 且有步骤的用例。
- `smoke`：带 smoke 标签或 priority <= smoke_priority；没有命中时取前 N 条。
- `impacted`：带 `change_impact` 或 `change_candidates` 的 enabled 用例。
- `max_cases`：按 priority/id 排序后截断。
- 只跑 `enabled` 且有 steps 的用例；pending 用例仍需人工审核启用。

## 4. 配置

```bash
SHAKEDOWN_WORKER_ENABLED=true
SHAKEDOWN_SCHEDULER_ENABLED=true
SHAKEDOWN_SCHEDULER_POLL_INTERVAL=10
```

调度线程与 worker 同进程启动；多实例部署时依赖数据库行锁 `FOR UPDATE SKIP LOCKED`，不会重复领取同一条 schedule。

## 5. 外部触发与健康摘要

### 部署 / CI 触发

```http
POST /api/projects/{project_id}/automation/trigger
X-Shakedown-Trigger-Token: <token>
```

```json
{
  "trigger": "deploy",
  "policy": {"mode": "smoke", "max_cases": 30},
  "note": "部署后冒烟"
}
```

项目 `settings.automation_trigger_token` 非空时校验 token；环境未就绪会创建 failed run
并进入例外队列，不产生产品缺陷。

### 健康摘要

```http
GET /api/projects/{project_id}/automation/health
```

返回最近一次 run、队列 active/failed、调度信息、执行状态分布、开放例外数、SLA 超时数。

## 6. 角色矩阵

```http
GET    /api/projects/{id}/members
POST   /api/projects/{id}/members
PATCH  /api/members/{id}
DELETE /api/members/{id}
```

角色：`owner` / `maintainer` / `viewer`。MCP client 通过 token 的 `actor`（为空时用
client name）匹配 `ProjectMember.actor`：`viewer` 只读、`maintainer` 可写、
`owner` 可管理成员。显式绑定 actor 但项目中没有对应成员会被拒绝；没有绑定 actor
且没有成员记录时保留旧行为，按 token scope 推导。

API 可选使用 `X-Shakedown-Actor` / `X-Shakedown-Tenant` 头做同样的角色与租户校验；
不传头时保持旧行为。未内置登录态，API 身份应由可信网关注入。

MCP client 创建时还可设置 `tenant`：非空时只能访问同 tenant 的项目。

## 7. 例外队列与 SLA

```http
GET   /api/projects/{project_id}/exceptions?status=open
PATCH /api/exceptions/{exception_id}
GET   /api/projects/{project_id}/sla/breaches
```

- 环境未就绪、低置信度分诊等自动动作无法安全处理时进入例外队列；
- 同一 kind + target 只保留一个未关闭例外；
- SLA 默认：critical 4h / high 24h / medium 3d / low 7d，可由
  `settings.finding_sla_minutes` 覆盖。

## 7.1 失败归因与自动修复

失败先走确定性规则（`app/failure_attribution.py`），规则命中就不调用模型：

| 类别 | 含义 | 自动动作 |
| --- | --- | --- |
| `environment` | 请求没到被测系统 / 沙箱不可用 | 用例保持 pending，记环境例外，不继续排队 |
| `platform` | 执行器、断言归一化、变量渲染缺口 | 记能力缺口；能修的就地修断言并重试一次 |
| `case` | 期望或步骤写错 | 标 `needs_review`，进入修复队列 |
| `defect` | 5xx、堆栈/SQL 泄露 | 挂疑似缺陷，不改用例 |
| `evidence` | 缺契约 / 缺入口，构造不出可执行用例 | 停用并保留缺口说明 |
| `unconfirmed` | 规则与模型都拿不准 | 保留现场，交人工 |

规则覆盖的典型平台问题：

- 命令步骤的断言被写成 `status_code`（拿退出码比文本）；
- 期望文本只出现在 stderr，断言却查 stdout；
- `text_contains` 指向 `headers.location`；
- JSONPath 过滤表达式 `$[?(...)]`；
- 未保存的模板变量（如 `{{admin_cookie}}`）。

试运行（`trigger=validation`）失败时同样先归因：平台问题自动修断言后重试一次，
环境问题不降级用例，疑似缺陷只记录不重写。存量用例可用
`app.attribution_backfill.backfill_project()` 回填「为什么停用」。

## 7.2 攻击用例包

`docs/attack-pack.md` 定义了可注册的攻击模式：开放重定向、改密会话失效、
越权矩阵 / IDOR、CSRF、路径穿越、文件上传、重复提交、并发竞态、SSRF、
默认口令、Cookie 属性、安全响应头、JSON 畸形输入、双账号差分越权、破坏性操作鉴权、令牌吊销、协议畸形输入、
依赖/工具链漏洞。入口：

```http
GET  /api/projects/{id}/attack-pack
POST /api/projects/{id}/attack-pack/generate
```

所有攻击断言都用 `SEC-*` 策略作为依据，失败由归因器直接判为缺陷并自动确认。

## 8. 自动更新与自动启用（可选）

项目 `settings.automation`：

```json
{
  "auto_enrich_impacted": true,
  "auto_enrich_limit": 50,
  "auto_enable": true,
  "auto_enable_priority_max": 100
}
```

- `auto_enrich_impacted`：变更影响面命中用例后自动入队 `enrich_case`；
- `auto_enable`：enrich 完成后，若用例有步骤、无 missing_input、非高风险，
  自动把 pending 改为 enabled；
- `max_runs_per_day` / `max_concurrent_runs`：运行配额，超过后触发器返回
  `quota_exceeded`，调度器只记录状态，不创建 run；
- 默认关闭，开启前请确认项目风险策略。

## 9. 环境生命周期（可选命令）

项目 `target_start`：

```json
{
  "type": "command",
  "allow_execute": true,
  "start_command": "./scripts/dev-up.sh",
  "stop_command": "./scripts/dev-down.sh",
  "cwd": "/repo",
  "timeout": 60,
  "wait_seconds": 30,
  "auto_start": true
}
```

同时在项目 `settings.automation.allow_target_commands=true` 才会执行。
默认命令进沙箱：`SHAKEDOWN_SANDBOX_MODE=berth` 时走 berth；`local` 沙箱没有隔离，
默认拒绝。宿主执行必须同时设置：

```json
{"command_execution": "host", "allow_host_commands": true}
```

返回结果带 `execution`（`berth` / `local` / `host` / `denied`）和 `sandboxed` 标记。
接口：

```http
GET  /api/projects/{id}/environment/status
POST /api/projects/{id}/environment/start
POST /api/projects/{id}/environment/stop
```

调度器在 readiness 失败且 `auto_start=true` 时会尝试启动环境；定时 run 成功结束后，
如果 `auto_stop=true` 会尝试执行 stop_command。stop 成功后可继续执行
`cleanup_command` 清理测试数据 / 容器 / 临时目录。

## 10. 白名单验证命令（进程 / 权限 / 产物）

项目 `settings.verification_commands`：

```json
[
  {
    "name": "service-exit",
    "description": "服务收到 SIGTERM 后 5 秒内退出",
    "mode": "process",
    "command": ["./scripts/check-exit.sh"],
    "cwd": "/repo",
    "timeout": 60
  }
]
```

接口：

```http
GET  /api/projects/{id}/verification-commands
POST /api/projects/{id}/verification-commands/{name}/run
{"case_id": 123}
```

只有项目配置的命令能执行，MCP 不能提交任意 command。默认进沙箱；`local` 沙箱
没有隔离，默认拒绝，需显式 `automation.allow_local_sandbox_commands=true`；
宿主执行需 `command_execution=host` + `allow_host_commands=true`。结果会标注
`execution` / `sandboxed`。带 `case_id` 时写入
`case_verifications`，`evidence_level=replayed`，供人工复核和审计。

## 11. 产物独立校验

```http
POST /api/projects/{id}/artifacts/verify
{"paths": ["dist/app.zip", "icon.png"]}
```

读取 `settings.artifact_root` 下的文件，由 Shakedown 侧计算 SHA-256、大小，
并拒绝 `../` 越界路径。

## 12. 测试数据

```http
POST /api/projects/{id}/test-data/generate
{
  "endpoint_id": 123,
  "fields": [
    {"name": "email", "type": "string"},
    {"name": "amount", "type": "number"},
    {"name": "status", "type": "string", "enum": ["new", "paid"]}
  ],
  "count": 20,
  "unicode": true
}
```

支持 email / phone / amount / status / date / url / 长文本 / Unicode 等字段语义；
不传 fields 时可从 endpoint request_body schema 推断。
数值字段支持 `distribution: uniform | normal`，并始终受 minimum/maximum 约束。

项目设置 `redact_pii=true` 时，执行结果落库前会脱敏：

- email / 手机号 / 银行卡
- Bearer token
- password / secret / token / authorization / cookie 等字段
- 嵌套 dict / list / 日志文本

原始请求已经执行完毕，脱敏只影响留档证据，不影响断言。

## 13. 性能执行

```http
POST /api/projects/{id}/performance/run
{
  "method": "GET",
  "path": "/api/items",
  "concurrency": 10,
  "iterations": 200,
  "duration_seconds": 30
}
```

返回成功/失败数、RPS、min/avg/p50/p95/p99/max 延迟和错误样例。
单次上限：并发 50、迭代 5000、时长 300 秒。

项目可配置 SLO：

```json
{
  "performance_slo": {
    "p95_ms": 300,
    "p99_ms": 800,
    "error_rate": 0.01,
    "min_rps": 20
  }
}
```

结果里的 `slo.passed` / `slo.violations` 可直接用于报告和告警。
`stress` / `soak` 还会采集进程 RSS、打开 FD、线程数，默认检查内存增长 128MB、
FD 增长 32、线程增长 8；可用项目配置覆盖：

```json
{
  "performance_resource_limits": {
    "max_memory_growth_mb": 64,
    "max_fd_growth": 16,
    "max_thread_growth": 4
  }
}
```

`stress` 额外返回 `analysis`：P95 增长比、RPS 峰值 / 回落、成功率下降和
`breakpoint_concurrency`（首次 P95 翻倍或成功率 < 95% 的并发）。
每次结果会写入 `performance_runs`，可通过：

```http
GET /api/projects/{id}/performance/history?path=/api/items&limit=50
```

查看 p95 / p99 / RPS / success_rate 趋势。

调度器支持性能模式：

```json
{
  "mode": "performance",
  "targets": [
    {"method": "GET", "path": "/api/items", "concurrency": 10, "iterations": 200}
  ]
}
```

调度器会入队 `performance_run` 任务，由 worker 执行并写入性能历史。

## 14. UI / 可访问性基础

浏览器步骤执行后，响应 body 会附加：

```json
{
  "url": "https://example.com/orders",
  "title": "订单",
  "a11y": {
    "images_without_alt": 0,
    "buttons_without_label": 0,
    "inputs_without_label": 1,
    "links_without_text": 0,
    "duplicate_ids": [],
    "heading_jumps": 0,
    "has_main_landmark": true,
    "has_title": true,
    "html_lang": "zh-CN"
  }
}
```

同时 artifacts 目录保存全页截图，并输出 `screenshot_sha256`。
这不是完整 axe/WCAG 审计，但能作为 UI 健康度 probe 和断言依据。

视觉基线：

```http
GET  /api/projects/{id}/visual/latest?case_id=123
GET  /api/projects/{id}/visual/baselines
POST /api/projects/{id}/visual/baselines   {"case_id":123,"name":"default"}
POST /api/projects/{id}/visual/compare     {"case_id":123,"name":"default"}
```

基线与最近一次截图 hash 比较，返回 `same` / `changed` / `no_screenshot`。
MCP 提供 `list_visual_baselines` / `set_visual_baseline` / `compare_visual_baseline`。

基础 WCAG 规则审计：

```http
GET  /api/projects/{id}/ui/a11y?case_id=123
POST /api/projects/{id}/ui/a11y/audit   {a11y metrics}
```

内置规则：image-alt、button-name、label、link-name、duplicate-id、heading-order、
document-title、html-lang、main-landmark。返回 `passed` / `checks` / `violations`。
MCP 提供 `get_case_a11y` / `audit_a11y`。

## 15. GitHub PR / push 触发

```http
POST /api/projects/{id}/automation/github
X-GitHub-Event: pull_request
X-Hub-Signature-256: sha256=...
```

- 项目 `settings.github_webhook_secret` 用于 HMAC 校验；
- 默认接受 `pull_request` / `push`；
- PR 默认策略由 `settings.github_trigger_policy` 决定，默认 `smoke`；
- 验签失败返回 403，不会触发 run。

GitLab：

```http
POST /api/projects/{id}/automation/gitlab
X-Gitlab-Event: Merge Request Hook
X-Gitlab-Token: ...
```

项目设置 `settings.gitlab_webhook_secret` 和 `gitlab_trigger_policy`；
支持 Merge Request Hook / Push Hook。

## 16. 边界

- 调度只负责“按时创建 run”，不自动审核 pending 用例；
- 环境失败不计入产品缺陷；
- 运行策略不会绕过 `enabled` 状态；
- 不在调度器里执行任意命令；非 API 验证仍按人工/外部证据流程处理。

## 17. 生命周期、质量与可观测性（自动补齐）

### 用例退役

```http
GET  /api/projects/{id}/cases/retire?stale_days=30
POST /api/projects/{id}/cases/retire?dry_run=false&stale_days=30
```

- 过期：pending / needs_review 且超过 `stale_days` 未更新；
- 重复：同功能点 + 归一化标题，保留 priority 最高的一条；
- 非破坏性：置为 `disabled`，加 `retired` tag，并写 `spec.retirement`；不删除执行历史。

### 断言质量 / mutation / oracle 趋势

```http
GET /api/projects/{id}/assertion-quality
GET /api/projects/{id}/assertion-quality/mutation
GET /api/projects/{id}/oracle-trend
```

- 强度评分看断言类型、依据来源、operator、expected 具体度；
- mutation 用最近成功响应构造错误响应，统计断言 kill rate；`exists` 等弱断言会被标出；
- oracle trend 按最近 run 统计 contract / policy / manual / none 的断言来源。

### 性能分层

`POST /api/projects/{id}/performance/run` 增加 `mode`：

- `baseline` / `load` / `soak`：固定并发采样；所有模式都采集 RSS / FD / 线程样本；
- `stress`：按并发阶梯执行，返回 `stages`、`resources` 和 `analysis.breakpoint_concurrency`；
- `soak` / `stress` 默认检查资源增长，超限会让 `slo.passed=false`；
- `GET /api/projects/{id}/performance/history` 增加 `degradation`，与历史中位数比较 P95/P99/RPS/成功率。

### 探索式执行

```http
POST /api/projects/{id}/exploratory/run
```

支持 `differential`（多组参数响应差异）、`fuzz`（5xx / 堆栈 / SQL 泄露信号）、
`metamorphic`（创建→读取字段一致性）、`property`（显式属性执行）。属性定义：

```json
{
  "mode": "property",
  "method": "GET",
  "path": "/api/items",
  "properties": [
    {"name": "ok", "kind": "status_in", "values": [200]},
    {"name": "has_id", "kind": "body_has_keys", "keys": ["id"]},
    {"name": "id_int", "kind": "body_type", "field": "id", "expected": "integer"},
    {"name": "fast", "kind": "latency_le", "limit_ms": 500},
    {"name": "stable", "kind": "repeat_equal", "times": 2}
  ],
  "cases": [{"name": "default", "query": {"page": 1}}]
}
```

也支持 `generator` 按字段语义生成输入，或把默认属性放在
`settings.exploratory_properties`。只对项目 `base_url` 发请求，写方法必须
`allow_write=true`；失败会返回反例，不直接生成硬断言或 confirmed finding。

### Flaky 隔离

项目 `settings.automation`：

```json
{"flaky_quarantine": true, "flaky_threshold": 2}
```

失败自动重跑出现两次不一致后累计 `flaky_stats`；达到阈值把 enabled 用例置为
`needs_review`，加 `flaky-quarantined` tag，并写例外队列。

### SLA 升级与 digest

```http
POST /api/projects/{id}/sla/escalate?dry_run=false
GET  /api/projects/{id}/sla/escalations
```

项目 `settings.automation`：

```json
{
  "sla_escalation_enabled": true,
  "sla_escalation_min_severity": "medium",
  "sla_escalation_include_unassigned": true,
  "digest_enabled": true,
  "digest_interval_seconds": 86400
}
```

- 同一 finding 只建一个未关闭 `sla_breach` 例外；首次升级才发 webhook / 邮件；
- 项目未开启 `sla_escalation_enabled` 时，`dry_run=false` 会 no-op；手工强制执行需
  显式 `force=true`，避免 API 绕过 opt-in；
- `digest_interval_seconds >= 7 天` 时投递 `weekly_digest`，否则 `digest`；
- 通知目标互相独立：`settings.webhook_url`（Slack / 任意 webhook）或
  `settings.notification_email`（SMTP）任一配置即可投递。

### 自动回归与强 trace

- `settings.automation.auto_rerun_impacted=true`：变更影响面命中后自动创建
  `change-impact` run；该 run 允许 `needs_review` 用例（显式白名单），用于先回归再人工复核；
- `GET /api/metrics`：Prometheus 文本指标（HTTP、任务、run、例外、用例结果）；
  HTTP 计数批量写入 `metric_counters` 表，多 worker / 多实例共享，抓取时合并；
  可用 `SHAKEDOWN_METRICS_DB_ENABLED=false` 退回进程内计数；
- 所有响应带 `X-Shakedown-Trace-Id`；
- `GET /api/health/live` / `GET /api/health/ready` 用于部署探针；
- `scripts/canary_check.py` 做部署后 smoke + gate 判定。

### 新项目 bootstrap：抽功能点 → 生成 → 试运行

空项目抽完功能点后，平台会自动入队一次 `generate_cases`（`only_missing=true`），
不需要再手工点「生成用例」。触发条件：

- 本次抽取产出了功能点；
- 项目当前用例数为 0；
- 没有排队 / 执行中的 `generate_cases` 任务；
- `settings.automation.auto_generate_on_bootstrap` 未被关闭（默认开启）。

已有用例的项目重新抽取功能点不会触发全量生成，仍走 change_impact / enrich。
生成完成后接下面的试运行，形成「抽点 → 生成 → 跑一次 → 通过启用」的完整 bootstrap。

### 生成后试运行（validation）

生成出来的用例默认是 `pending`，没人跑过就不算测试资产。平台会在生成完成后
自动挑出「可判定」的用例跑一次，用结果决定是否启用。

**可判定条件**（全部满足）：

- 有步骤，且步骤数 > 0；
- 至少一条 `status=confirmed` 且 evidence 不是 `none` 的断言（只有 pending 断言会「空转通过」）；
- 不是高风险操作（DELETE / destructive / `risk=high`，以及项目 `risk_whitelist` 之外的写操作）。

**执行语义**：

- 新建 `trigger=validation` 的 run，只跑本批选中的 `pending` 用例；
- 只跑一次：不重跑失败用例、不做 LLM 归因、不生成 finding、不发 digest；
- 通过 → 自动置为 `enabled`；失败 / error / flaky → `needs_review` + `case_validation_failed` 例外；
- 整批都是 `error` 时按环境问题处理：用例保持 pending，写 `case_validation_environment` 例外，等环境恢复；
- 每批最多 50 条，跑完自动接下一批；SSE / NDJSON 响应只读 5 秒或 64KB 就断开，避免整批挂死。

**配置**（项目 `settings.automation`，默认都开启）：

```json
{
  "auto_validate_generated": true,
  "auto_enable_after_validation": true
}
```

**API**：

```http
GET  /api/projects/{id}/cases/validation/preview   # 可试运行数量、被挡原因、当前 active run
POST /api/projects/{id}/cases/validation/run       # {"case_ids": [...], "limit": 50}
```

`case_ids` 为空时按 priority / id 自动挑选；已有排队或执行中的试运行时直接返回它。
生成任务完成后会自动调用同一套逻辑，只针对本次新生成的用例。
