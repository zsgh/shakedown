# Shakedown API SLA

本 SLA 针对 Shakedown 自身同步 HTTP API（本地开发实例也按同一基线验收）。

## 响应时间

- 普通同步接口：**P95 ≤ 100ms**
- 复杂检索 / 聚合接口：**P95 ≤ 100ms**
- 所有同步接口：**P99 ≤ 200ms**
- 任何同步接口单次响应超过 **200ms** 视为超出硬上限

## 统计口径

- 统计从请求进入 API 到返回响应的时间，不含客户端网络耗时。
- 以下路径不计入同步 API SLA：LLM 调用、Agent 任务执行、沙箱执行、后台 worker、GitHub / Jira 等外部服务。
- `POST /api/projects/{project_id}/runs` 等只负责入队的接口，只统计入队响应，不统计实际执行时长。
- 5xx 视为失败，不应由非法输入触发；客户端参数错误应返回 4xx。

## 基线

项目级生成配置使用：

```text
performance_sla_ms = 100
performance_samples = 20
```

当知识库文档没有更细的 SLA 时，性能维度生成器使用该值作为 `timing` 断言阈值。

## 执行方式

`timing` 步骤会在同一个执行沙箱内连续发起 N 次相同请求（`samples`，默认 20），记录每次耗时并计算：

```text
P50 / P95 / P99 / min / max
```

当前验证的是**单接口顺序请求延迟 P95**，不是并发吞吐或压力测试。并发压测需要后续增加压测 worker、并发模型和多沙箱聚合。
