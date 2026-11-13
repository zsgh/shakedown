# Shakedown 执行器运行时说明

沙箱里执行的是「由数据库生成的代码」，不是手写脚本：

```
用例 JSON（数据库）
   │  app/executor/compiler.py
   ▼
shakedown_runtime.py   ← 运行时（断言引擎、变量渲染、结果落盘）
test_shakedown_cases.py ← 由用例 JSON 编译出的 pytest 文件
   │  pytest
   ▼
results/case_<id>.json ← 结果（请求 / 响应 / 断言明细 / 日志）
```

## 关键约定

1. **断言类型**
   - `status_code`：状态码
   - `json_path`：JSON 路径（`$.data.items[0].id`）
   - `db_state`：数据库状态（直连被测库）
   - `cross_api`：跨接口一致性
   - `timing`：响应时间（阈值必须来自 SLA）
   - `text_contains`：响应文本包含
2. **待确认断言**：`status != confirmed` 或 `evidence.source_type == none` 的断言只记录，不判失败。
3. **变量渲染**：`{{run_id}}`、`{{uuid}}`、`{{timestamp}}`、`{{entity_id}}`（步骤 `save` 里保存）。
4. **隔离**：数据统一带 `{{run_id}}` 前缀；执行结束后可按 `cleanup` 步骤清理。

## 沙箱运行方式

- berth 模式：`POST /scripts/run`（`image=runner`，`language=bash`），脚本会写文件、跑 pytest、汇总结果。
- 本地模式（开发降级）：在临时目录里直接 `python -m pytest`。
