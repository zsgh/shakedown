/** 展示层统一文案：所有枚举在这里翻译，页面里禁止直接显示英文状态。 */

export const RUN_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: '排队中', tone: 'info' },
  preparing: { label: '准备环境', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  analyzing: { label: '分析中', tone: 'violet' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'muted' },
}

export const CASE_STATUS: Record<string, { label: string; tone: string }> = {
  pending: { label: '待审核', tone: 'warning' },
  enabled: { label: '已启用', tone: 'success' },
  disabled: { label: '已停用', tone: 'muted' },
  needs_review: { label: '待复核', tone: 'warning' },
}


export const EXECUTION_STATE: Record<string, { label: string; tone: string }> = {
  never_executed: { label: '从未执行', tone: 'muted' },
  enabled_no_result: { label: '已启用但无结果', tone: 'warning' },
  pending_not_enabled: { label: '待审核未启用', tone: 'info' },
  stale_after_source: { label: '知识源更新后未复核', tone: 'violet' },
  executed: { label: '已执行', tone: 'success' },
}

export const RESULT_STATUS: Record<string, { label: string; tone: string }> = {
  pass: { label: '通过', tone: 'success' },
  fail: { label: '失败', tone: 'danger' },
  flaky: { label: '不稳定', tone: 'warning' },
  error: { label: '错误', tone: 'violet' },
  skipped: { label: '跳过', tone: 'muted' },
}

export const FINDING_STATUS: Record<string, { label: string; tone: string }> = {
  pending: { label: '待确认', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'danger' },
  false_positive: { label: '误报', tone: 'muted' },
  accepted: { label: '已接受风险', tone: 'muted' },
  wont_fix: { label: '不修复', tone: 'muted' },
}

export const GAP_STATUS: Record<string, { label: string; tone: string }> = {
  open: { label: '待处理', tone: 'warning' },
  claimed: { label: '已认领', tone: 'blue' },
  fixed: { label: '待复检', tone: 'info' },
  accepted: { label: '已接受风险', tone: 'muted' },
  wont_fix: { label: '不修复', tone: 'muted' },
}

export const GAP_OWNER_KIND: Record<string, { label: string; tone: string }> = {
  'test-side': { label: '测试侧资产', tone: 'blue' },
  'sut-contract': { label: '被测方契约', tone: 'warning' },
  environment: { label: '环境', tone: 'violet' },
}

export const MISSING_INPUT_KIND: Record<string, { label: string; tone: string }> = {
  sla: { label: 'SLA / 性能阈值', tone: 'success' },
  response_schema: { label: '响应 schema', tone: 'warning' },
  auth: { label: '鉴权语义', tone: 'violet' },
  api_param: { label: '接口 / 参数', tone: 'blue' },
  environment: { label: '环境', tone: 'danger' },
  other: { label: '其他', tone: 'muted' },
  unknown: { label: '未分类', tone: 'muted' },
}

export const EVIDENCE_SOURCE: Record<string, { label: string; tone: string }> = {
  openapi: { label: 'OpenAPI', tone: 'success' },
  code: { label: '代码', tone: 'violet' },
  doc: { label: '文档', tone: 'blue' },
  manual: { label: '人工', tone: 'warning' },
  policy: { label: '通用策略', tone: 'danger' },
  project_config: { label: '项目配置', tone: 'info' },
  none: { label: '无依据', tone: 'muted' },
}

export const WEBHOOK_EVENT: Record<string, string> = {
  'finding.created': '新缺陷',
  'finding.updated': '缺陷状态变更',
  'case_gap.updated': '缺口处置',
  'case_gap.batch_updated': '缺口批量处置',
  digest: '摘要 digest',
}

export const FINDING_FIX_STATUS: Record<string, { label: string; tone: string }> = {
  open: { label: '待修复', tone: 'muted' },
  in_progress: { label: '修复中', tone: 'info' },
  claimed_fixed: { label: '待验证修复', tone: 'warning' },
  verified_fixed: { label: '修复已验证', tone: 'success' },
  reopened: { label: '验证失败', tone: 'danger' },
  needs_human_verification: { label: '需人工验证', tone: 'warning' },
  needs_external_verification: { label: '待外部验证', tone: 'warning' },
}

export const CASE_VERIFICATION_MODE: Record<string, string> = {
  api: 'API 黑盒',
  process: '进程 / 服务',
  artifact: '构建产物',
  frontend: '浏览器 / 前端',
  privileged: '权限 / Keychain',
  manual: '仅人工',
}

export const CASE_VERIFICATION_STATUS: Record<string, { label: string; tone: string }> = {
  unverified: { label: '未验证', tone: 'muted' },
  passed_external: { label: 'Shakedown 验证通过', tone: 'success' },
  failed_external: { label: 'Shakedown 验证失败', tone: 'danger' },
  reported_passed: { label: '开发自报通过', tone: 'info' },
  reported_failed: { label: '开发自报失败', tone: 'warning' },
}

export const DIMENSION: Record<string, { label: string; tone: string }> = {
  functional: { label: '功能', tone: 'blue' },
  boundary: { label: '边界', tone: 'orange' },
  security: { label: '安全', tone: 'danger' },
  performance: { label: '性能', tone: 'success' },
}

export const SOURCE_KIND: Record<string, { label: string; tone: string }> = {
  code: { label: '代码库', tone: 'violet' },
  markdown: { label: 'Markdown', tone: 'blue' },
  openapi: { label: 'OpenAPI', tone: 'success' },
}

export const TASK_KIND: Record<string, string> = {
  plan_features: '抽取功能点',
  generate_cases: '生成用例',
  execute_run: '执行用例',
  reparse_source: '重新解析知识源',
  whitebox_review: '白盒走查',
  enrich_case: 'AI 补全用例',
  verify_fixes: '自动验证修复',
  triage_findings: 'AI 分诊缺陷',
  backtest: '回测',
}

export const TASK_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: '排队中', tone: 'info' },
  running: { label: '运行中', tone: 'info' },
  succeeded: { label: '成功', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'muted' },
}

export const ATTACK_CATEGORY: Record<string, string> = {
  redirect: '开放重定向',
  session: '会话与凭据',
  authz: '越权 / 访问控制',
  csrf: '跨站请求伪造',
  traversal: '路径穿越',
  injection: '注入',
  'supply-chain': '供应链 / 依赖',
  transport: '传输与响应头',
  logic: '业务逻辑',
}

export const GATE_BLOCKER: Record<string, string> = {
  no_run: '没有执行记录',
  run_not_completed: '执行未完成',
  environment: '环境异常',
  failed_cases: '存在失败用例',
  confirmed_findings: '存在已确认缺陷',
  exceptions: '存在未关闭例外',
  slo: 'SLO 未达标',
}

export const EXCEPTION_KIND: Record<string, string> = {
  case_validation_failed: '试运行未通过',
  case_validation_environment: '试运行时环境异常',
  platform_capability_gap: '平台能力缺口',
  suspect_defect: '疑似产品缺陷',
  environment_issue: '环境问题',
  environment_unavailable: '环境未就绪',
  onboarding_low_confidence: '接入置信度不足',
  triage_low_confidence: '分诊置信度不足',
  triage_policy_exception: '分诊策略例外',
  test_issue: '测试侧问题',
  flaky_case: '用例不稳定',
  sla_breach: 'SLA 超时',
}

export const EXCEPTION_TARGET: Record<string, string> = {
  case: '用例',
  run: '执行',
  finding: '缺陷',
  task: '任务',
  project: '项目',
}

export const EXCEPTION_STATUS: Record<string, string> = {
  open: '待处理',
  acked: '已认领',
  resolved: '已解决',
  ignored: '已忽略',
}

export const SEVERITY: Record<string, { label: string; tone: string }> = {
  critical: { label: '严重', tone: 'danger' },
  high: { label: '高', tone: 'danger' },
  medium: { label: '中', tone: 'warning' },
  low: { label: '低', tone: 'muted' },
}

export const ENDPOINT_STATE: Record<string, { label: string; tone: string }> = {
  consistent: { label: '一致', tone: 'success' },
  inconsistent: { label: '不一致', tone: 'danger' },
  conflict: { label: '冲突', tone: 'danger' },
  partial: { label: '单侧覆盖', tone: 'warning' },
  unknown: { label: '单一来源', tone: 'muted' },
}

export const RUN_CHANGE: Record<string, { label: string; tone: string; hint: string }> = {
  broken: { label: '新失败', tone: 'danger', hint: '上次通过，这次失败' },
  still_failing: { label: '持续失败', tone: 'warning', hint: '两次都失败' },
  fixed: { label: '已修复', tone: 'success', hint: '上次失败，这次通过' },
  new: { label: '新增用例', tone: 'info', hint: '只出现在目标运行' },
  removed: { label: '移除用例', tone: 'muted', hint: '只出现在基线运行' },
  changed: { label: '状态变化', tone: 'violet', hint: '不稳定 / 跳过等变化' },
  still_passing: { label: '持续通过', tone: 'success', hint: '两次都通过' },
}

export const SOURCE_ROLE: Record<string, string> = {
  spec: '接口规范',
  notes: '设计笔记',
}

export const ASSERTION_KIND: Record<string, string> = {
  status_code: '状态码',
  field: '响应字段',
  json_path: 'JSON 路径',
  header: '响应头',
  db: '数据库状态',
  database: '数据库状态',
  duration: '响应时间',
  time: '响应时间',
  text: '文本',
  element: '页面元素',
  url: 'URL',
  count: '数量',
  schema: '结构',
}

export const FEEDBACK_ACTION: Record<string, string> = {
  create: '创建',
  enable: '启用',
  disable: '停用',
  modify: '修改',
  confirm: '确认',
  false_positive: '标记误报',
}

export const ATTRIBUTION: Record<string, { label: string; tone: string }> = {
  defect: { label: '疑似缺陷', tone: 'danger' },
  environment: { label: '环境问题', tone: 'warning' },
  platform: { label: '平台能力缺口', tone: 'warning' },
  evidence: { label: '依据不足', tone: 'muted' },
  case: { label: '用例问题', tone: 'info' },
  test: { label: '用例问题', tone: 'info' },
  unconfirmed: { label: '归因待确认', tone: 'warning' },
  flaky: { label: '不稳定', tone: 'warning' },
}

export function meta(map: Record<string, { label: string; tone: string }>, key?: string | null) {
  if (!key) return { label: '未知', tone: 'muted' }
  return map[key] ?? { label: key, tone: 'muted' }
}

export const NAV_LABEL: Record<string, string> = {
  '': '总览',
  cases: '测试设计',
  runs: '执行',
  findings: '缺陷',
  knowledge: '理解',
  activity: 'Agent 活动',
  settings: '设置',
}
