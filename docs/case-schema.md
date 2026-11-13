# 统一用例 JSON 格式（执行器唯一契约）

执行器只认这一种格式。用例的真身存在数据库（`test_cases.spec`），
`app/executor/compiler.py` 把它编译成可执行的 pytest 代码。

## 顶层结构

```jsonc
{
  "schema_version": "1.0",
  "id": "42",                    // 由平台写入（数据库主键）
  "title": "[正常流] 创建用户 - POST /api/users",
  "dimension": "functional",     // functional | boundary | security | performance
  "type": "api",                 // api | web
  "feature": { "id": 7, "name": "创建用户" },
  "description": "……",
  "preconditions": [ /* 见下 */ ],
  "steps":       [ /* 见下 */ ],
  "assertions":  [ /* 见下 */ ],
  "cleanup":     [ /* 同 steps */ ],
  "evidence":    [ /* 用例级依据 */ ],
  "tags": ["normal-flow"],
  "missing_input": ""            // 非空表示「缺少输入」，该用例不参与执行
}
```

Schema 定义见 `backend/app/executor/case_schema.py`（JSON Schema draft 2020-12），
Agent 生成后先校验，不合法则带错误信息重试。

## 前置条件 `preconditions`

```jsonc
[
  { "kind": "http_login", "name": "default", "account_ref": "default", "role": "user" },
  { "kind": "db_fixture", "sql": "INSERT INTO ..." },
  { "kind": "manual", "note": "需要先在后台配置 XX" }
]
```

- `account_ref` 指向项目配置的测试账号（加密存储，运行时才注入沙箱）。
- 第一版**不做**公共前提抽取与复用（见 todo.md Phase 6）。

## 步骤 `steps`

```jsonc
[
  {
    "action": "request",          // request | browser | db | sleep
    "name": "创建用户",
    "method": "POST",
    "path": "/api/users",
    "headers": { "Content-Type": "application/json" },
    "query": { "page": 1 },
    "body": { "name": "shakedown-{{run_id}}-name" },
    "save": { "entity_id": "$.id" },     // 把响应里的值存进变量
    "assert": [ /* 见断言 */ ]
  },
  {
    "action": "browser",
    "browser": {
      "base_url": "http://127.0.0.1:5173",
      "steps": [
        { "op": "goto", "url": "/login" },
        { "op": "fill", "selector": "#username", "value": "u1" },
        { "op": "click", "selector": "#submit" },
        { "op": "wait_for", "selector": ".dashboard" },
        { "op": "text", "selector": ".balance", "save_as": "balance" }
      ]
    },
    "assert": [ /* 见断言 */ ]
  },
  {
    "action": "db",
    "sql": "SELECT status FROM orders WHERE id = %s",
    "params": ["{{entity_id}}"]
  }
]
```

### 变量占位符

执行时统一渲染（步骤的 `path` / `headers` / `query` / `body`，以及断言的
`expected` / `target` / `sql` 等字段都会渲染）：

| 占位符 | 含义 |
|---|---|
| `{{run_id}}` | 本次执行 id（用于生成唯一数据，避免冲突；自动重跑会带 `-r1` 后缀） |
| `{{uuid}}` | 随机串 |
| `{{timestamp}}` | 秒级时间戳 |
| `{{entity_id}}` | 上一步 `save` 里保存的变量 |

> 断言里的期望值可以和请求体使用同一个占位符，例如请求体写
> `"name": "MYMAC_TEST_{{run_id}}"`，断言写
> `"expected": "MYMAC_TEST_{{run_id}}"`；执行器会渲染成同一个值再比较。

### 鉴权

```jsonc
{ "action": "request", "auth": { "account": "default" }, ... }
```

平台按 `account` 从项目测试账号里取 token / 账号密码注入请求头；
**只注入该项目配置的测试账号**，不注入任何生产凭据。

## 断言 `assertions`

断言默认写在**它要校验的那一步**的 `assert` 数组里（这样能准确绑定该步骤的响应）：

```jsonc
{
  "action": "request",
  "method": "PATCH",
  "path": "/api/env/vars/{{created_name}}",
  "body": { "value": "new-value-{{run_id}}" },
  "assert": [
    {
      "kind": "json_path",
      "target": "$.name",
      "operator": "eq",
      "expected": "MYMAC_UPDATE_{{run_id}}",
      "status": "confirmed",
      "evidence": {
        "source_type": "openapi",
        "ref": "endpoint:81",
        "quote": "PATCH /api/env/vars/{name}，200 响应包含 name"
      },
      "description": "修改结果对应指定环境变量"
    }
  ]
}
```

顶层 `assertions` 也支持，用于跨步骤 / 用例级断言；可带 `step_index`
（从 0 开始）指定绑定哪一步的响应。平台展示时会把「步骤内联断言 + 顶层断言」
汇总成一份「断言与依据」列表，并按 `(kind, target, operator, expected)`
去掉两处重复写的同一检查。

```jsonc
{
  "kind": "status_code",     // status_code | json_path | db_state | cross_api | timing | text_contains
  "target": "$.data.items[0].id",
  "operator": "eq",          // eq ne in not_in lt le gt ge contains not_contains exists not_exists matches empty
  "expected": 201,
  "threshold_ms": 200,       // 仅 timing
  "status": "confirmed",     // confirmed | pending（待确认）
  "evidence": {
    "source_type": "openapi",        // doc | openapi | code | manual | none
    "ref": "#/paths/~1api~1users/post",
    "quote": "成功返回 201",
    "endpoint_id": 12,
    "chunk_id": 34
  },
  "description": "创建成功返回 201"
}
```

各断言类型的语义：

| kind | 目标 | 期望 |
|---|---|---|
| `status_code` | HTTP 状态码 | `expected`（数字或数组 + `operator=in`） |
| `json_path` | JSON 路径（`$.a.b[0].c`） | `expected` |
| `db_state` | 直连被测库执行 SQL | 首行首列（或整行）与 `expected` 比较 |
| `cross_api` | 另一个接口的响应 | 内部再发一次请求，校验其 `target` |
| `timing` | 步骤耗时（ms） | `threshold_ms` |
| `text_contains` | 响应文本 | `expected` |

### 「待确认」规则（重要）

满足任一条件即视为**待确认**：

- `status != "confirmed"`；
- `evidence.source_type` 为空或 `"none"`。

待确认断言**只记录、不判失败**（执行结果里落在 `output.pending_assertions`）。
没有依据的预期不允许直接判失败 —— 这是本项目的核心质量红线。

## 清理步骤 `cleanup`

与 `steps` 同结构，但总在用例结束后执行（失败也执行），用于删除测试数据、还原配置。

## 缺失输入 / 待补全说明 `missing_input`

`missing_input` 是**说明字段**，用来记录这条用例还缺什么，不直接阻止执行：

- 有可执行步骤的用例，即使 `missing_input` 非空，仍会进入执行集；
- `steps` 为空的用例无法执行，会被执行器跳过；
- 只是断言缺少依据时，把对应断言标为 `status: pending`（待确认），不要用 `missing_input` 代替；
- 只有连接口、参数、账号或环境都无法确定、导致步骤无法构造时，才把 `missing_input` 当作真正的阻塞项。

示例：

```jsonc
{
  "title": "[性能] 用户列表 - 缺少 SLA 阈值",
  "dimension": "performance",
  "steps": [],
  "missing_input": "缺少 SLA：文档未提供该接口的响应时间阈值"
}
```

这些缺口可以在「理解 → 契约缺口」统一查看，导出契约缺口报告交给被测系统的维护者，或直接对单条用例触发 AI 补全（只改用例，不修改被测系统）。
