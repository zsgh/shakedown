# 运维：备份、恢复与高可用

## 备份

```bash
SHAKEDOWN_DATABASE_URL=postgresql://user:pass@host:5432/shakedown \
  ./scripts/backup_db.sh ./backups
```

生成：

- `*.dump`：`pg_dump --format=custom`
- `*.dump.sha256`：校验文件

建议：

- 每日全量 + 关键操作前手动备份；
- 备份文件放独立存储，不与数据库同盘；
- 定期做恢复演练，不只校验文件存在。

## 恢复

```bash
SHAKEDOWN_DATABASE_URL=postgresql://user:pass@host:5432/shakedown \
  ./scripts/restore_db.sh ./backups/shakedown-xxx.dump
```

恢复会校验 SHA-256，然后执行 `pg_restore --clean --if-exists`。
生产恢复前先停 worker，避免任务在恢复过程中继续写库。

## API / worker 分离部署

`docker-compose.ha.yml` 提供最小高可用拓扑：

- `api`：只提供 HTTP / MCP，`SHAKEDOWN_WORKER_ENABLED=false`
- `worker`：独立进程 `python -m app.worker`，执行任务和调度
- `db`：Postgres + pgvector

worker 可横向扩容；任务队列使用数据库租约，调度器使用 `FOR UPDATE SKIP LOCKED`，
多实例不会重复领取。API 的 `--workers 2` 只扩展 HTTP，不重复启动 worker。

灾备 / 灰度清单见 `docs/dr.md`。

## 高可用建议

- API 与 worker 分离部署；
- worker 可横向扩容，任务队列用 `FOR UPDATE SKIP LOCKED`，天然支持多实例；
- 调度器与 worker 同进程启动，多实例也不会重复领取 schedule；
- HTTP 指标写入共享 `metric_counters` 表，多 worker / 多 API 实例的 `/metrics`
  会聚合；若关闭 `SHAKEDOWN_METRICS_DB_ENABLED`，每个进程只看到自己的计数；
- Postgres 使用主从/托管实例，开启 PITR；
- 对象存储放 artifacts，定期清理过期 run；
- 升级前备份 + 先在 staging 跑 `alembic upgrade head`；
- 对外暴露的 MCP / automation trigger 必须配置 token 和网络访问控制。

## 迁移

```bash
cd backend
alembic upgrade head
```

迁移前备份；迁移后至少跑一遍 health + 一条 smoke run。
