# 灾备与灰度清单

## 目标

- API：多实例 + 负载均衡；
- worker：多实例，任务租约保证不重复；
- Postgres：主从 / 托管实例 + PITR；
- artifacts：对象存储或独立卷；
- 备份：每日全量 + 关键操作前手动备份。

## RPO / RTO 建议

| 数据 | RPO | RTO |
|---|---|---|
| 用例 / 缺陷 / 执行结果 | <= 24h（每日备份） | <= 2h |
| 关键操作前 | 0（手动备份） | <= 1h |
| artifacts | 与对象存储策略一致 | <= 4h |

## 故障切换

1. 停止 worker，避免恢复期间继续写库；
2. 恢复 Postgres（`scripts/restore_db.sh`）；
3. 校验 `alembic current` 与备份版本一致；
4. 启动 API，检查 `/api/health`；
5. 启动 worker，确认队列无积压；
6. 跑一条 smoke run。

## 灰度升级

- 先在 staging 执行 `alembic upgrade head`；
- API 与 worker 分开滚动；
- 新 worker 启动后旧 worker 自然退出；
- 观察异常队列、失败率、调度器 last_status；
- 出现异常时回滚镜像，数据库迁移只做向前兼容（避免破坏旧版本）。

## 演练

- 每季度做一次恢复演练；
- 记录实际 RTO；
- 校验备份 SHA-256；
- 检查 webhook / MCP token / 加密密钥是否可恢复。
