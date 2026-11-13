# 架构

## 组件

| 组件 | 位置 | 职责 |
|---|---|---|
| 前端看板 | `frontend/` | 项目列表、知识源管理、接口清单、一致性、覆盖树、审核队列、执行详情、缺陷列表、Agent 轨迹 |
| 后端 API | `backend/app/api/` | REST 接口（FastAPI + SQLAlchemy） |
| Agent（skipper） | `backend/app/agent/` | provider 抽象、工具调用循环、功能点抽取、用例生成、失败归因、白盒走查 |
| 知识解析 | `backend/app/ingest/` | Markdown / 代码 / OpenAPI 解析，chunk + 向量，接口归一化，一致性对照 |
| 执行器 | `backend/app/executor/` | 统一用例 schema、编译器（JSON→pytest）、运行时（断言引擎） |
| 沙箱 | `backend/app/sandbox/` | `BerthClient` / `BerthSandbox`，本地降级 `LocalSandbox` |
| 调度 | `backend/app/worker/` | DB 驱动的任务队列 + 状态机 worker |
| 报告 | `backend/app/reporting/` | 自包含 HTML 报告导出 |

## 数据模型

见 `backend/app/models.py`，与 todo.md 第 2 节一一对应：

```
projects ─┬─ knowledge_sources ─┬─ chunks（向量）
          │                      └─ spec_snapshots（spec diff）
          ├─ endpoints（多来源合并 + 一致性状态）
          ├─ features ── test_cases（spec / evidence）
          ├─ runs ── case_results（归因 / 复现）
          ├─ findings（execution / whitebox）
          ├─ agent_traces
          ├─ human_feedbacks
          └─ tasks（worker 队列）
```

要点：

- 半结构化数据一律 `JSONB`；`chunk.embedding` 用 pgvector（`vector(1536)`）。
- 所有表带 `id / created_at / updated_at`。
- `endpoints.sources` 记录每个接口的来源（openapi / code / doc + 定位串），
  `endpoints.consistency` 记录三方对照结果（state + mismatches）。

## LLM provider 抽象

```python
class LLMProvider(ABC):
    def chat(messages, tools=None, ...) -> LLMResponse          # 对话 + 工具调用
    def structured(system, user, schema, context=None) -> dict  # 结构化输出
```

实现：

| provider | 说明 |
|---|---|
| `openai` | 任意 OpenAI 兼容接口（HTTP，不绑定 SDK），支持 tool_calls、usage 统计、JSON 模式 |
| `mock` | 离线确定性规则版 Agent：吃结构化素材，按任务类型产出；无网络也能跑通全链路 |
| `skipper` | 复用 skipper 项目的 LLM 配置（接口待与 skipper 侧确认，见 docs/questions.md） |

`context` 是「结构化素材」：真实模型会把它序列化进 prompt；离线 provider 直接消费它。
两种实现因此共享同一套业务代码。

### 工具调用循环（LangGraph）

Agent 的工具调用循环用 **LangGraph** 状态图实现（`backend/app/agent/loop.py`）：

```
                 ┌──────────────────────────────┐
                 │                              │
      START ──▶ agent ──▶ tools ──▶ agent ──▶ …──▶ END
                 │          (有 tool_calls 时)   │
                 └──────────────────────────────┘
                        (模型给出最终答案时直接 END)
```

- **状态** `AgentState`：`messages`（对话 + 工具结果）、`iterations`（轮数）、`tool_calls`（累计工具调用）；
- **节点**：`agent`（调模型）、`tools`（执行 Shakedown 工具，注入 `ToolContext`）；
- **条件边**：还有 tool_calls 且未超轮数 → `tools`，否则 `END`；
- **强制收尾**：轮数用尽时模型可能还在「我再查查」，此时会**不带工具**再问一次拿最终答案。

模型侧通过 `backend/app/agent/chat_model.py` 适配：把 Shakedown 自己的
`LLMProvider` 包成 LangChain 的 `BaseChatModel`（支持 `bind_tools`），
因此**离线 mock 与真实模型跑的是同一张图**，行为一致。

工具（`backend/app/agent/tools/`）全部只读：

| 工具 | 作用 |
|---|---|
| `list_code_files` | 列代码库文件 |
| `read_code_file` | 读文件（带行号） |
| `grep_code` | 正则搜索 |
| `find_symbol` | 按类/函数名定位定义 |
| `search_knowledge` | pgvector 语义检索（文档 + 代码切片） |
| `list_endpoints` / `read_endpoint` | 接口清单 |
| `list_features` | 功能点 |

每一步都写 `agent_traces`（输入 / 输出 / 工具调用 / token / 耗时 / 错误）。

## Worker 状态机

任务落在 `tasks` 表，worker 用 `FOR UPDATE SKIP LOCKED` 抢占，带租约与心跳，
worker 崩溃后任务自动回队（断点恢复）。

执行类任务（`execute_run`）内部再走一层业务状态机，每步写库：

```
queued → preparing（准备环境：按项目起独立被测库）
       → running（编译 + 沙箱执行 + 失败重跑）
       → analyzing（失败归因 + 生成缺陷）
       → completed / failed
```

## 沙箱

`SandboxBackend` 抽象出两种实现：

- `BerthSandbox`：把编译产物通过 `POST /scripts/run` 丢进 berth（overlayfs 隔离、
  uid 1000、cgroup 限资源、host 网络白名单由 berth 侧保证）。
- `LocalSandbox`：开发降级，在临时目录里跑 pytest（**没有隔离**，仅用于打通链路）。

执行器只依赖 `SandboxBackend`，因此「沙箱换实现」不影响业务代码。

## 隔离与安全

| 层面 | 做法 |
|---|---|
| 被测库 | 每个项目一个 berth 服务名（`{项目}-db`）；`isolation.per_run` 时按 run 创建独立服务；只读 DSN 与读写 DSN 分离 |
| 测试数据 | 数据集资产 + `{{data.*}}` 迭代；fixture setup/cleanup；`{{run_id}}` 前缀 + 显式清理；详见 [test-data.md](test-data.md) |
| 凭据 | 项目测试账号加密存储（Fernet），只在运行时注入该项目的账号 |
| 网络 | 白名单由 berth 侧策略保证（依赖 berth B2），平台侧只配置 `allowed_hosts` |
| 代码 | Agent 产出的代码只在沙箱内执行 |

## 扩展点

- 换模型：改 `SHAKEDOWN_LLM_PROVIDER`。
- 换沙箱：实现 `SandboxBackend` 即可。
- 新断言类型：在 `case_schema.py` 加枚举 + 在 `templates/runtime.py` 加实现。
- 新知识源类型：在 `ingest/` 加解析器 + 在 `ingest/service.py` 加分支。
