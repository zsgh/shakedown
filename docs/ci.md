# CI / PR 接入示例

## GitHub Actions 触发 Shakedown

```yaml
name: Shakedown Smoke
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  shakedown:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Shakedown
        run: |
          curl -fsS -X POST \
            "$SHAKEDOWN_URL/api/projects/$PROJECT_ID/automation/trigger" \
            -H "Content-Type: application/json" \
            -H "X-Shakedown-Trigger-Token: $TRIGGER_TOKEN" \
            -d '{"trigger":"pr","policy":{"mode":"smoke","max_cases":30},"note":"GitHub PR"}'
        env:
          SHAKEDOWN_URL: ${{ secrets.SHAKEDOWN_URL }}
          PROJECT_ID: ${{ secrets.SHAKEDOWN_PROJECT_ID }}
          TRIGGER_TOKEN: ${{ secrets.SHAKEDOWN_TRIGGER_TOKEN }}
```

也可以直接把 GitHub 的 PR webhook 配到：

```text
POST /api/projects/{project_id}/automation/github
```

需要设置项目 `settings.github_webhook_secret`，并用同一个 secret 配置 GitHub webhook。
默认 PR/push 触发 `smoke` 策略。

## 建议的合并前策略

- PR：`mode=smoke`，只跑高优先级/冒烟用例，控制在 30-50 条；
- main 合并：`mode=impacted`，跑变更影响面命中的用例；
- 定时：`mode=full`，夜间全量回归；
- 部署后：`trigger=deploy`，`mode=smoke` + readiness gate。

## CI gate

触发 run 后，用 gate 判断是否允许合并：

```http
GET /api/projects/{project_id}/automation/gate?run_id=123
```

返回：

```json
{
  "passed": false,
  "status": "fail",
  "blockers": [
    {"kind": "failed_cases", "message": "3 条用例失败/错误"},
    {"kind": "confirmed_findings", "message": "1 条高严重度确认缺陷"},
    {"kind": "slo", "message": "1 次性能 SLO 未通过"}
  ],
  "warnings": []
}
```

阻断项包括：run 未完成、环境错误、失败用例、高严重度确认缺陷、环境例外、性能 SLO 未通过。
性能 SLO 只统计该 run 时间窗内（从 run 创建到下一次 run 创建）的结果，旧 run 不会被之后
新产生的 SLO 失败误阻断。返回里的 `slo_window` 会标明窗口起止。
也可以由 MCP `get_automation_gate` 读取。

## 失败处理

- 环境未就绪：创建 failed run + 例外队列，不阻断合并判断为产品缺陷；
- 执行失败：进入 finding / triage；
- 低置信度分诊：进入例外队列，由人工确认；
- SLA 超时：通过 `/api/projects/{id}/sla/breaches` 查看。
