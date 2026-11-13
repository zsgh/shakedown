# 与 berth 的接口约定

Shakedown 只通过 berth 的 HTTP API 调用它（`github.com/zsgh/berth`，文档见 `docs/api.md`）。
berth 侧的改造任务在 berth 仓库的 `berth-todo.md` 里推进。

## 平台侧封装

`backend/app/sandbox/berth.py`：

| Shakedown 需要 | berth 接口 | 依赖的 berth 任务 |
|---|---|---|
| 起 / 停被测库（按项目隔离） | `PUT /services/{name}`、`POST /services/{ref}/stop`、`DELETE /services/{ref}` | B5 服务参数覆盖与初始化、B6 PostgreSQL 模板、B8 服务重置 |
| 跑用例 | `POST /scripts/run`（`image=runner`，`language=bash`） | B4 异步执行与资源上限、B7 runner 镜像 |
| 取回结果 | stdout 里的 `<<<SHAKEDOWN_RESULT>>>` 标记 | （B3 文件进出完成后可换成文件下载） |
| 看沙箱执行详情 | `/ui/#/runs/{task_id}` | B12 标签、U8 按项目过滤、U10 深链约定 |
| 服务日志（归因用） | `GET /services/{ref}/logs`（待提供） | B9 服务日志接口 |
| 起被测系统并等待就绪 | `GET /health` / http 健康检查 | B10 http 健康检查、B11 被测系统运行时 |
| 鉴权 | `X-Berth-Token` / `Authorization: Bearer` | B1 API 鉴权 |
| 网络白名单 | berth 侧策略 | B2 网络策略 |

## 当前实现与降级

- 平台默认 `SHAKEDOWN_SANDBOX_MODE=local`（本地临时目录跑 pytest），
  方便在没有 berth 的机器上打通链路；生产切到 `berth`。
- 被测库隔离：项目配置里填 `isolation.db_service`（建议 `{项目名}-db`）与 `isolation.db_template`，
  平台在 `prepare_environment()` 里 `PUT /services/{name}`，并在执行结束后销毁。
- 模板目前只有 `redis` / `mysql`（berth 侧），PostgreSQL 模板由 B6 补齐；
  在此之前，可在项目配置里指向一个已经跑着的测试库（填 `target_dsn`）。

## 结果回传协议

沙箱脚本执行完 pytest 后打印一行标记：

```
<<<SHAKEDOWN_RESULT>>>[{"case_id": 1, "status": "pass", "request": {...}, "response": {...}, ...}]
```

平台按这个标记解析结果。B3（文件进出）落地后，可以改为从 `/workspace/results/*.json` 下载。

## 沙箱里的执行流程

```bash
set -eu
mkdir -p /workspace/results /workspace/artifacts
cd /workspace
cat > /workspace/shakedown_runtime.py <<'EOF' ... EOF
cat > /workspace/test_shakedown_cases.py <<'EOF' ... EOF
export SHAKEDOWN_BASE_URL=...
python3 -m pytest -q --no-header -p no:cacheprovider test_shakedown_cases.py || true
python3 - <<'EOF'   # 汇总 results/*.json 并打印标记
EOF
```

失败用例的 pytest 退出码非 0 不会中断汇总（`|| true`），结果以 JSON 为准。
