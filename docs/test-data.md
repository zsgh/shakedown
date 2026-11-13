# 测试数据（Test Data）

> 状态：P0 / P1 / P2 的首批能力已落地；数据库事务回滚、账号池、外部服务虚拟化、多数据源和数据库快照仍是后续项。

## 1. 能力总览

| 能力 | 状态 | 入口 |
|---|---|---|
| 数据集资产（schema / seed / 版本 / 行） | ✅ | `POST /api/projects/{id}/test-datasets` |
| 数据行绑定用例（single / iterate） | ✅ | `POST /api/test-cases/{case_id}/data-bindings` |
| 执行期 `{{data.字段}}` 渲染 | ✅ | `backend/app/executor/templates/runtime.py` |
| fixture setup / cleanup / 失败传播 | ✅ | 用例 `fixtures` |
| 文件 fixture / multipart / 浏览器上传 | ✅ | `test_files` + 步骤 `files` |
| 数据库只读 DSN / 断言读写分离 | ✅ | 项目设置「只读 DSN」 |
| 每次 run 的 migrate / seed / account / init_script | ✅ | `target_start.*_command` + `isolation.init_script` |
| 账号预检 / 真实登录 / token 刷新 | ✅（基础版） | `POST /api/projects/{id}/accounts/validate` |
| PII 标记 / 可选预览脱敏 | ✅（基础版） | 数据集 `pii_fields` / `mask_pii=1` |
| 数据目录 UI | ✅ | `/projects/{id}/test-data` |
| 数据健康度 / 清理 | ✅（基础版） | `GET /test-data/health`、`POST /test-data/cleanup` |
| 账号池 / 租约 | ⏭ 跳过 | 账号不是可复制资源，租约只能串行化 run，收益低 |
| 数据库事务回滚 / savepoint | ⏭ 跳过 | 被测系统使用独立连接，未提交事务对 SUT 不可见 |
| 外部服务虚拟化 | ⏭ 跳过 | 需要独立 mock 服务与网络注入，单独立项 |
| MySQL / Redis / ES 多数据源 | ⏭ 跳过 | 当前 runner 只带 psycopg，需按数据源增加驱动与连接抽象 |
| 数据库快照 / 分支 | ⏭ 跳过 | 依赖 PostgreSQL 快照或存储层能力，当前 run 级 schema/service 已覆盖基础隔离需求 |

## 2. 数据集

创建：

```http
POST /api/projects/{id}/test-datasets
{
  "name": "users-normal",
  "count": 20,
  "seed": 42,
  "fields": [
    {"name": "email", "type": "string", "format": "email", "unique": true},
    {"name": "name", "type": "string", "minLength": 3},
    {"name": "amount", "type": "number", "minimum": 1, "maximum": 1000}
  ],
  "schema": {
    "relations": [
      {"left": "min_amount", "right": "amount", "kind": "lt"}
    ]
  }
}
```

也支持 `endpoint_id`：从接口 `request_body` 推断字段。生成器支持：

- 类型：string / integer / number / boolean / array / object / null；
- 格式：email / phone / date / date-time / uri / uuid；
- 约束：enum / const / minimum / maximum / exclusive* / multipleOf / minLength / maxLength / pattern（简单集合）；
- 嵌套：`properties` / `required` / `items` / `oneOf` / `anyOf` / `allOf` / 本地 `$ref`；
- 行级：`unique`、`relations`（lt/lte/gt/gte/eq/ne）、`negative`；
- 命名空间：生成时保留 `{{run_id}}`，执行时由 runtime 渲染；也可在服务层传入 namespace。

> 生成器不访问网络、不写库；重复调用同一 seed 结果一致。

## 3. 用例绑定与执行

```http
POST /api/test-cases/{case_id}/data-bindings
{"dataset_id": 1, "mode": "iterate"}
```

用例里：

```json
{
  "steps": [
    {
      "action": "request",
      "method": "POST",
      "path": "/api/users",
      "body": {"email": "{{data.email}}", "amount": "{{data.amount}}"}
    }
  ]
}
```

- `single`：只取第一行（或指定 `row_index`）；
- `iterate`：每行完整执行一次，任意一行失败则用例失败/异常；
- 结果 `output.iterations` 记录每行状态、断言数、日志；
- 兼容旧写法：`{{field}}` 会从当前行注入同名变量，但推荐 `{{data.field}}`。

## 4. Fixtures

```json
{
  "fixtures": [
    {
      "kind": "api",
      "name": "seed-user",
      "steps": [
        {"action": "request", "method": "POST", "path": "/api/users", "body": {"name": "seed-{{run_id}}"}, "save": {"user_id": "$.id"}}
      ],
      "cleanup": [
        {"action": "request", "method": "DELETE", "path": "/api/users/{{user_id}}"}
      ]
    },
    {
      "kind": "sql",
      "name": "flags",
      "sql": "INSERT INTO flags(name, enabled) VALUES (%s, true)",
      "params": ["shakedown-{{run_id}}"]
    },
    {
      "kind": "file",
      "name": "avatar",
      "file_id": 3
    }
  ]
}
```

- fixture setup 失败 → 用例 `error`，不会继续跑 steps；
- 用例 `cleanup` 与 fixture `cleanup` 总是执行；
- 清理失败会让“本应通过”的用例变成 `error`，并在 `output.cleanup_errors` 里列出；
- 支持 `api` / `sql` / `file` / `command` / `dataset` / `http_login` / `manual`。

## 5. 文件

项目文件：

```http
POST /api/projects/{id}/test-files   (multipart form: file, name)
```

请求步骤：

```json
{
  "action": "request",
  "method": "POST",
  "path": "/api/upload",
  "files": {"file": {"file_id": 3}}
}
```

浏览器步骤：

```json
{"op": "upload", "selector": "input[type=file]", "file_id": 3}
```

文件会随 spec 以 base64 注入沙箱，runtime 解码成 multipart 或临时文件；最大 10MB。

## 6. 数据库与环境准备

- 项目 `target_dsn`：读写；`target_read_dsn`：只读，`db_state` 断言优先使用；
- `db_fixture` / `fixtures.kind=sql` 使用读写 DSN，失败会阻断用例；
- `isolation.per_run=true`：reset 模式为每次 run 创建 `{db_service}-{run_id}` 服务，只销毁本次创建的服务；
- `target_start.migrate_command / seed_command / account_command` 在每次 run 的 preparing 阶段执行；
- `isolation.init_script` 会在 seed 前执行（PostgreSQL，`allow_execute` 打开时）；
- 数据准备失败会把 run 置为 `failed`，不会拿脏库继续跑；
- `external` 环境不再需要平台启动 SUT，也能执行 migrate/seed/account。

## 7. 账号

- 项目级加密账号矩阵；
- 执行时只注入用例显式引用的账号（`account.<name>` / `auth.account` / `account_ref`）；
- `http_login` 会真正执行登录：token 优先，否则用 `settings.profile.auth.login_path` + 用户名密码；
- 401 时会尝试用账号密码登录 + token_endpoint 刷新一次；
- `POST /accounts/validate` 预检 token / 登录，不修改账号；
- CLI / 生成代码仍能读到注入的账号和 DSN，沙箱按不可信代码对待；生产建议使用短时凭据和只读 DSN。

## 8. PII

- 生成器按字段名 / format 标记 `pii_fields`（email / phone / name / address / 证件 / 银行卡等）；
- 数据集 `meta.pii_fields` 持久化；
- `GET /test-datasets?mask_pii=1` 或 `GET /test-datasets/{id}?mask_pii=1` 返回脱敏预览；
- 执行结果仍由项目 `settings.redact_pii` 控制落库前脱敏。

## 9. 已知边界

- 数据集版本是“重新生成即 +1”，不是不可变快照；
- `iterate` 的每行结果合并到一条 `case_result`，不是每行一条结果；
- fixture 失败只保证不继续执行 steps，不自动回滚已执行的 setup；
- 数据清理以用例 / fixture 的显式 cleanup 为准，没有按 `run_id` 自动扫描业务表；
- 账号池、数据库事务、外部 mock、多数据源、DB 快照未实现。

## 10. 任务清单

详细的完成项与跳过原因见 [test-data-todo.md](test-data-todo.md)。
