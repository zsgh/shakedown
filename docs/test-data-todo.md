# 测试数据能力 TODO

> 对照上一轮分析中的 P0 / P1 / P2。✅ 已实现；◐ 部分实现；⏭ 评估后跳过。

## P0

- [x] **P0-1 数据资产模型 / 迁移 / API / UI**
  - `test_datasets`、`test_data_rows`、`case_data_bindings`、`test_files`
  - 迁移 `c7d9e1f3a5b7_test_data_assets.py`
  - `/api/projects/{id}/test-datasets`、`/api/test-cases/{id}/data-bindings`
  - 前端「测试数据」页
- [x] **P0-2 fixture 生命周期**
  - 用例 `fixtures`：api / sql / file / command / dataset / http_login / manual
  - setup 失败 → `error`，不跑 steps
  - 用例 cleanup + fixture cleanup 总执行；清理失败 → `error`
  - `output.cleanup_errors` / `output.fixture_assertions`
- [x] **P0-3 生成器接入执行链路**
  - 数据集行 → `{{data.*}}`
  - `single` / `iterate` 执行
  - 嵌套对象 / 数组 / `$ref` / `oneOf` / `allOf`
  - 约束、关系（lt/lte/gt/gte/eq/ne）、unique、negative、PII 标记
- [x] **P0-4 数据库隔离与 seed 接入 worker**
  - 只读 DSN：`target_read_dsn`，`db_state` 默认只读
  - 每 run 执行 migrate / seed / account / init_script
  - `isolation.per_run` 创建 `{service}-{run_id}`，只删除本次创建的服务
  - 数据准备失败直接把 run 置为 failed
  - [ ] 事务回滚 / savepoint：⏭ 跳过（SUT 使用独立连接，未提交事务不可见）
- [x] **P0-5 账号预检 / 登录 / 最小注入 / 刷新**
  - `POST /projects/{id}/accounts/validate`
  - `http_login` 真正执行登录（token 或 login_path）
  - 401 时尝试登录 + token_endpoint 刷新一次
  - 只注入用例引用的账号
  - [ ] 账号池 / 租约：⏭ 跳过（账号不是可复制资源，租约只能串行化 run）

## P1

- [x] **P1-6 文件 fixture**
  - 项目文件上传 / 删除 / 列表
  - multipart `files` 步骤
  - 浏览器 `upload` 动作
  - spec 内 base64 注入沙箱
- [ ] **P1-7 外部依赖虚拟化**：⏭ 跳过（邮件 / HTTP mock / 时钟需要独立服务与网络注入，单独立项）
- [x] **P1-8 数据驱动执行**
  - 数据集绑定 → 多行迭代
  - 结果记录 `output.iterations`
  - 兼容旧 `{{field}}` 写法
- [x] **P1-9 清理 / 健康度 / 可观测性（基础版）**
  - `GET /projects/{id}/test-data/health`
  - `POST /projects/{id}/test-data/cleanup`
  - 归档数据集行清理、失效绑定清理、丢失文件清理
  - [ ] 业务表按 `run_id` 自动孤儿扫描：⏭ 暂不做（需要项目级表清单/清理策略，避免误删）

## P2

- [x] **P2-10 PII 分类 / 合成 / 脱敏（基础版）**
  - 字段级 PII 标记
  - 数据集 `meta.pii_fields`
  - `mask_pii=1` 预览脱敏
  - [ ] 完整数据策略 / 合规审计：◐ 后续
- [ ] **P2-11 多数据源（MySQL / Redis / ES）**：⏭ 跳过（runner 只带 psycopg，需要独立连接抽象与驱动）
- [ ] **P2-12 数据库快照 / 分支 / ephemeral 环境**：◐ 仅完成 per-run berth 服务；数据库快照/分支 ⏭ 跳过
- [x] **P2-13 数据目录 UI**
  - 新建数据集 / 预览行 / 重新生成 / 删除
  - 绑定用例 / PII 标识
  - 文件上传 / 删除
  - 健康度 / 一键清理

## 验证

- 后端：`cd backend && pytest -q` → 365 passed
- 前端：`cd frontend && pnpm run typecheck && pnpm run build`
- 迁移：空库 `alembic upgrade head` 通过；开发库已升级到 `c7d9e1f3a5b7`
