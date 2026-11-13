# Shakedown 无人值守自动化总纲

目标：把“人工点生成 / 点执行 / 点分诊 / 点验证”变成可配置、可审计、可恢复的自动化闭环。
原则：自动化只做可判定动作；环境问题、低置信度、高风险操作进入例外队列。

## A. 控制面（已落地）

| ID | 模块 | 内容 | 状态 |
|---|---|---|---|
| A1 | 调度器 | `project_schedules`、到期扫描、`run-now`、多实例行锁 | 已完成 |
| A2 | 环境就绪 | readiness 探活、环境失败 run、webhook 事件 | 已完成 |
| A3 | 运行策略 | full / smoke / impacted、维度、优先级、max_cases | 已完成 |
| A4 | 执行状态与批量重评 | never_executed / enabled_no_result / pending / stale；批量 enrich | 已完成 |
| A5 | 通知与 SLA | digest/webhook、SLA 超时 API；定时升级 + 日报/周报 + 邮件；邮件-only 目标 | 已完成 |
| A6 | 触发源 | 通用 automation trigger + token；GitHub/GitLab PR、push、deploy 适配器 | 已完成 |
| A7 | 自动化控制台 | 调度、成员、例外、SLA、质量、性能、视觉 / a11y、自动化设置 | 已完成 |

## B. 测试资产自治

| ID | 模块 | 内容 | 状态 |
|---|---|---|---|
| B1 | 变更驱动更新 | change_impact → 自动标记 / 批量 enrich / 自动回归 | 已完成 |
| B2 | 用例审核策略 | 低风险自动启用、高风险人工、过期/重复用例退役 | 已完成 |
| B3 | 断言质量 | 断言强度评分、oracle mutation、oracle 覆盖趋势 | 已完成（基础版） |
| B4 | 探索式生成 | 差分 / 变形 / fuzz / 属性执行层（status / schema / json path / latency / repeat-equal 属性） | 已完成 |
| B5 | 生成后试运行 | 新用例有步骤 + 有依据断言即自动跑一次；通过启用、失败复核；整批 error 视为环境问题 | 已完成 |

## C. 执行与验证

| ID | 模块 | 内容 | 状态 |
|---|---|---|---|
| C1 | 环境管理 | 启动/停止 SUT、DB migration/seed、账号准备、清理；默认沙箱执行，宿主执行需显式 opt-in | 已完成（命令执行受策略限制；每次 run 执行 migrate/seed/account/init_script） |
| C2 | 执行伸缩 | 多 worker、并发/配额、超时、取消、artifact 保留 | 已完成（含 retention API） |
| C3 | 非 API 验证 | 进程/产物/权限/浏览器沙箱重放、artifact hash；项目命令默认进沙箱 | 已完成（基础版；local 沙箱无隔离，默认拒绝命令） |
| C4 | 测试数据 | 业务语义、分布、隔离、清理、PII | 已完成（数据集资产 + fixture + 数据驱动 + 文件；详见 `test-data.md`） |
| C5 | 性能分层 | baseline / load / stress / soak、并发阶梯、资源 / FD / 线程增长、SLO 退化检测 | 已完成（资源检查已接入） |
| C6 | UI / 可访问性 | 视觉回归、a11y、探索式 UI | 已完成（视觉基线/a11y/探索式 API + 自动化控制台；探索式 UI 走 exploratory API） |

## D. 判定与闭环

| ID | 模块 | 内容 | 状态 |
|---|---|---|---|
| D1 | 自动分诊 | 高置信度自动确认、低置信度例外、三分类 def/test/env | 已完成 |
| D2 | 例外队列 | 环境、低置信度分诊统一例外表 + API | 已完成（基础版） |
| D3 | 回归重开 | 修复后自动验证、失败重开、flaky 隔离 | 已完成 |
| D4 | 缺陷去重 | 同根因合并、跨 run 关联、误报复核 | 已完成 |

## E. 运营与规模化

| ID | 模块 | 内容 | 状态 |
|---|---|---|---|
| E1 | 报告通知 | 日报/周报、SLA 升级、Slack(webhook)/邮件 | 已完成（PR 评论通过 webhook 对接外部系统） |
| E2 | 可观测性 | automation health + Prometheus metrics + trace id + readiness | 已完成（HTTP 计数 DB 聚合，多 worker / 多实例共享） |
| E3 | 安全多租户 | RBAC、租户隔离、配额、密钥、审计 | 部分完成（ProjectMember 角色 + tenant + MCP token 绑定；API 身份靠可信网关注入 actor / tenant 头，未内置 SSO） |
| E4 | 部署运维 | HA、备份恢复、迁移、灰度、灾备 | 已完成（HA compose + 备份恢复 + readiness + canary 脚本 + DR 清单；自动回滚由部署系统） |
| E5 | CI/CD | PR gate、变更子集执行、合并前检查 | 部分完成（GitHub/GitLab 触发 + CI gate；PR 评论/状态回写由外部 CI 负责） |

## 推荐顺序

1. A5 通知与 SLA
2. D1/D2 自动分诊 + 例外队列
3. B1/B2 变更驱动用例更新 + 审核策略
4. C1 环境生命周期
5. C3/C4 非 API 验证 + 测试数据
6. A6/E5 CI/CD 触发
7. E2/E3/E4 规模化保障

## 明确边界（不等同于已完成）

- **身份认证**：未内置 SSO / 登录态。MCP 用 `mcp_...` token；API 的
  `X-Shakedown-Actor` / `X-Shakedown-Tenant` 只在可信网关后使用，缺失时保持旧行为。
- **租户隔离**：`projects.tenant` + `mcp_clients.tenant / actor` 做应用层校验，
  不是数据库行级安全（RLS）。
- **命令沙箱**：berth 模式下项目命令进 berth；`local` 沙箱没有 cgroup / 网络隔离，
  默认拒绝执行。宿主执行必须同时设置 `command_execution=host` 与
  `allow_host_commands=true`，结果会标记 `execution=host`。
- **属性测试**：当前是 HTTP 属性执行器（status / schema / json path / latency /
  repeat-equal），不是任意语言内的 property-based 框架。
- **性能 stress/soak**：已有并发阶梯与资源增长检查，但没有 GC / 连接池内部指标；
  资源阈值可由 `settings.performance_resource_limits` 覆盖。
- **CI 回写**：只提供 gate 判定和触发；GitHub/GitLab 的 commit status / PR 评论
  需要外部 CI 调用 API 完成。

