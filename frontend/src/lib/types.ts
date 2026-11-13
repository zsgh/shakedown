export type Dimension = 'functional' | 'boundary' | 'security' | 'performance'
export type CaseStatus = 'pending' | 'enabled' | 'disabled' | 'needs_review'
export type ResultStatus = 'pass' | 'fail' | 'flaky' | 'error' | 'skipped'

export interface ProjectSummary {
  id: number
  name: string
  description: string
  tenant?: string
  base_url: string
  case_count: number
  enabled_case_count: number
  feature_count: number
  endpoint_count: number
  source_count: number
  pending_review: number
  findings_pending: number
  coverage_rate: number
  last_run_status?: string | null
  last_run_at?: string | null
  last_run_pass_rate?: number | null
}

export interface Project extends ProjectSummary {
  target_start: Record<string, any>
  isolation: Record<string, any>
  test_accounts: Record<string, any>
  target_dsn_configured: boolean
  settings: Record<string, any>
  created_at: string
  updated_at: string
}

export interface KnowledgeSource {
  id: number
  project_id: number
  kind: 'code' | 'markdown' | 'openapi'
  role?: 'spec' | 'notes'
  name: string
  location: string
  version: string
  status: string
  error?: string | null
  meta: Record<string, unknown>
  reevaluate_candidates?: number
  parsed_at?: string | null
  created_at: string
}

export interface Endpoint {
  id: number
  project_id: number
  method: string
  path: string
  summary: string
  params: Record<string, any>
  request_body: Record<string, any>
  responses: Record<string, any>
  security: Record<string, any>
  examples: Record<string, any>
  sources: Array<{ type: string; ref?: string; summary?: string }>
  consistency: {
    state?: string
    sources?: string[]
    mismatches?: Array<{ kind: string; message: string; evidence?: unknown[] }>
    checked_at?: string
  }
}

export interface Feature {
  id: number
  project_id: number
  name: string
  module: string
  description: string
  source_refs: Array<{ source_type?: string; ref?: string; quote?: string }>
  endpoint_ids: number[]
  unmapped: boolean
}

export interface Evidence {
  source_type?: string
  ref?: string
  quote?: string
  note?: string
}

export interface TestCase {
  id: number
  project_id: number
  feature_id?: number | null
  title: string
  dimension: Dimension
  case_type: string
  status: CaseStatus
  generation: string
  priority: number
  tags: string[]
  preconditions: Record<string, any>
  steps: Array<Record<string, any>>
  expected: Record<string, any>
  evidence: Evidence[]
  spec: Record<string, any>
  missing_input?: string | null
  verification_mode?: 'api' | 'process' | 'artifact' | 'frontend' | 'privileged' | 'manual' | string
  verification_status?: 'unverified' | 'passed_external' | 'failed_external' | string
  verification?: Record<string, any>
  verified_at?: string | null
  verified_by?: string | null
  feature_name?: string | null
  finding_ids?: number[]
  execution_states?: string[]
  created_at: string
  updated_at: string
}

export interface CasePage {
  items: TestCase[]
  total: number
  offset: number
  limit: number
  counts: Record<string, number>
}

export interface Run {
  id: number
  project_id: number
  trigger: string
  status: string
  env_snapshot: Record<string, any>
  stats: Record<string, any>
  case_ids: number[]
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  berth_task_id?: string | null
  backtest: Record<string, any>
  created_at: string
}

export interface CaseResult {
  id: number
  run_id: number
  case_id: number
  status: ResultStatus
  request: Record<string, any>
  response: Record<string, any>
  duration_ms: number
  logs: string
  screenshot_path?: string | null
  trace_path?: string | null
  attribution: Record<string, any>
  rerun: boolean
  seq: number
  output: Record<string, any>
  case_title?: string | null
  dimension?: string | null
  created_at: string
}

export interface ResultDetail extends CaseResult {
  finding_ids: number[]
  run: { id: number; status: string; created_at: string } | null
}

export interface Finding {
  id: number
  project_id: number
  source: 'execution' | 'whitebox' | 'external'
  title: string
  description: string
  severity: string
  evidence: Array<Record<string, any>>
  code_location: Record<string, any>
  result_id?: number | null
  case_id?: number | null
  module: string
  risk_type: string
  repro: string
  status: 'pending' | 'confirmed' | 'false_positive' | 'accepted' | 'wont_fix'
  confirmed_by?: string | null
  confirmed_at?: string | null
  cluster_key?: string | null
  triage_status?: string
  triage?: Record<string, any>
  duplicate_of?: number | null
  fix_status?: 'open' | 'in_progress' | 'claimed_fixed' | 'verified_fixed' | 'reopened' | string
  fix_verified_at?: string | null
  fix_claim?: Record<string, any>
  resolution?: Record<string, any>
  assignee?: string
  created_at: string
}

export interface CoverageTree {
  project_id: number
  total_features: number
  covered_features: number
  coverage_rate: number
  risk?: {
    covered: number
    total: number
    coverage_rate: number
    categories: Array<{
      key: string
      label: string
      covered: boolean
      cases: number
      probe_cases: number
      policy_cases: number
      examples: number[]
    }>
  }
  oracles?: {
    cases: number
    cases_with_oracle: number
    probe_cases: number
    assertions: number
    by_source: Record<string, number>
    oracle_mix: { contract: number; policy: number; manual: number; none: number }
    pending_assertions: number
    unbacked_assertions: number
    avg_assertions_per_case: number
    case_oracle_rate: number
  }
  modules: Array<{
    module: string
    total: number
    covered: number
    features: Array<{
      id: number
      name: string
      description: string
      unmapped: boolean
      source_refs: Evidence[]
      endpoint_ids: number[]
      dimensions: Record<string, number>
      total_cases: number
      enabled_cases: number
      covered: boolean
      missing_input?: string | null
    }>
  }>
}

export interface AttentionItem {
  kind: string
  level: 'info' | 'warning' | 'danger'
  title: string
  detail: string
  count: number
  action: string
  path: string
}

export interface Overview {
  project_id: number
  coverage: CoverageTree
  dimensions: Record<string, { total: number; enabled: number; pending?: number; passed?: number; failed?: number }>
  runs: Array<{
    id: number
    status: string
    created_at: string
    started_at?: string | null
    finished_at?: string | null
    total: number
    passed: number
    failed: number
    flaky: number
    pass_rate: number
  }>
  findings: { total: number; by_source: Record<string, number>; by_status: Record<string, number> }
  inconsistent_endpoints: number
  backtest?: {
    bugs: number
    caught: number
    hit_rate: number | null
    false_positives: number
    items: Array<{ bug_id: string; caught: boolean; matched_cases: string[]; note?: string; reason?: string }>
  }
  attention: AttentionItem[]
  pending_review: number
  enabled_cases: number
  total_cases: number
  endpoint_count: number
  source_count: number
  latest_run_id?: number | null
  last_run_status?: string | null
}

export interface AgentTrace {
  id: number
  project_id?: number | null
  run_id?: number | null
  kind: string
  step: string
  model: string
  input: Record<string, any>
  output: Record<string, any>
  tool_calls: Array<Record<string, any>>
  input_tokens: number
  output_tokens: number
  duration_ms: number
  error?: string | null
  created_at: string
}

export interface Task {
  id: number
  project_id?: number | null
  run_id?: number | null
  kind: string
  status: string
  priority: number
  payload: Record<string, any>
  result: Record<string, any>
  checkpoint?: Record<string, any>
  error?: string | null
  attempts: number
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface LatestCaseResult {
  result: {
    id: number
    run_id: number
    case_id: number
    status: string
    created_at: string
    duration_ms: number
    attribution: Record<string, any>
  } | null
  run: { id: number; status: string; created_at: string } | null
}

export interface ProjectHealth {
  project_id: number
  target: { ok: boolean | null; url: string; status?: number; latency_ms?: number | null; error?: string | null }
  worker: { active_tasks: number }
  knowledge: { sources: number; failed: number; parsing: number }
}

export interface SearchResults {
  cases: Array<{ id: number; title: string; status: string; dimension: string }>
  findings: Array<{ id: number; title: string; status: string; severity: string }>
  endpoints: Array<{ id: number; method: string; path: string; summary: string }>
}

export type GapOwnerKind = 'test-side' | 'sut-contract' | 'environment'
export type GapStatus = 'open' | 'claimed' | 'fixed' | 'accepted' | 'wont_fix'
export type MissingInputKind = 'sla' | 'response_schema' | 'auth' | 'api_param' | 'environment' | 'other' | ''

export interface CaseGapAssertion {
  index: number
  kind: string
  description: string
  operator: string
  expected: unknown
  step_index?: number | null
  endpoint: string
  confirmable: boolean
  evidence: Evidence
}

export interface ContractQuestion {
  question: string
  missing_contract: string
  owner_kind: GapOwnerKind
  step_index?: number | null
  source?: string
}

export interface CaseGap {
  case_id: number
  title: string
  status: string
  dimension: string
  feature_id?: number | null
  feature_name?: string | null
  step_count: number
  assertion_count: number
  contract_questions: ContractQuestion[]
  question_count: number
  probe: boolean
  missing_input?: string | null
  missing_input_kind: MissingInputKind
  owner_kind: GapOwnerKind
  owner_kinds: GapOwnerKind[]
  no_steps: boolean
  execution_states?: string[]
  pending_assertions: CaseGapAssertion[]
  gap_status: GapStatus | string
  gap_owner: string
  gap_note: string
  gap_updated_at?: string | null
  updated_at?: string | null
}

export interface CaseGapSummary {
  cases: number
  pending_assertions: number
  contract_questions: number
  cases_with_questions: number
  cases_with_pending: number
  cases_without_steps: number
  cases_with_missing_input: number
  by_status: Record<string, number>
}

export interface GapFacets {
  by_missing_input_kind: Record<string, number>
  by_owner_kind: Record<string, number>
  by_dimension: Record<string, number>
  by_execution_state: Record<string, number>
  by_feature: Record<string, number>
  by_evidence_source: Record<string, number>
  by_question_owner_kind: Record<string, number>
}

export interface CaseGapResponse {
  summary: CaseGapSummary
  facets: GapFacets
  items: CaseGap[]
}

export interface CaseGapAudit {
  id: number
  from_status: string
  to_status: string
  owner: string
  note: string
  actor: string
  source: string
  detail: Record<string, unknown>
  created_at: string | null
}

export interface CaseGapBatchFilters {
  kind?: string
  dimension?: string
  feature_id?: number
  evidence_source?: string
  owner?: string
  missing_input_kind?: string
  owner_kind?: string
  gap_status?: string
  include_closed?: boolean
  include_disabled?: boolean
}

export interface CaseGapBatchPreview {
  dry_run: true
  matched: number
  status: string
  owner: string
  note: string
  cases: Array<{
    case_id: number
    title: string
    gap_status: string
    missing_input_kind?: string
    owner_kind?: string
  }>
}

export interface CaseGapBatchResult {
  dry_run: false
  matched: number
  updated: number
  case_ids: number[]
}

export interface AssertionGroupItem extends CaseGapAssertion {
  case_id: number
  case_title: string
  feature_name?: string | null
  owner_kind: GapOwnerKind
  selection: { case_id: number; index: number }
}

export interface AssertionGroup {
  key: string
  count: number
  confirmable: number
  assertions: AssertionGroupItem[]
}

export interface AssertionGroupsResponse {
  group_by: string
  total: number
  confirmable: number
  groups: AssertionGroup[]
}

export interface SlaRecomputeCase {
  case_id: number
  title: string
  status: string
  missing_input: string
  reasons: string[]
  old_assertions: Array<{
    operator?: string
    expected?: unknown
    target?: string
    description?: string
  }>
  has_steps: boolean
}

export interface SlaRecomputePreview {
  project_sla_ms: number | null
  count: number
  cases: SlaRecomputeCase[]
  message: string
}

export interface SlaRecomputeResult {
  project_sla_ms: number
  applied: number
  case_ids: number[]
  skipped: Array<{ case_id: number; reason: string }>
}

export interface ReevaluateCase {
  case_id: number
  title: string
  status: string
  endpoints: string[]
  removed_endpoints: string[]
  pending_assertions: Array<{ index: number; kind: string; description: string; evidence: Evidence }>
}

export interface ReevaluatePreview {
  snapshot_id: number | null
  snapshot_version: string
  diff: Record<string, unknown>
  count: number
  cases: ReevaluateCase[]
  message: string
}

export interface ReevaluateResult {
  snapshot_id: number | null
  processed: number
  confirmed_assertions: number
  unlocked_cases: number[]
  case_ids: number[]
  skipped: Array<{ case_id: number; reason: string }>
}

export interface AssertionMigrationItem {
  kind: string
  description: string
  target: string
  expected: unknown
  step_index?: number | null
  action: 'policy' | 'question'
  policy_id?: string
  policy_title?: string
  question?: string
  owner_kind?: GapOwnerKind
}

export interface AssertionMigrationPreview {
  affected_cases: number
  assertions: number
  to_policy: number
  to_question: number
  cases: Array<{
    case_id: number
    title: string
    dimension: string
    assertions: AssertionMigrationItem[]
  }>
}

export interface AssertionMigrationResult {
  applied: number
  to_policy: number
  to_question: number
  cases: Array<{ case_id: number; title: string; policy_assertions: number; contract_questions: number }>
}

export interface ProbeGeneratePreview {
  dry_run: boolean
  endpoints: number
  planned: number
  skipped: number
  created?: number
  case_ids?: number[]
  cases: Array<{ title: string; dimension: string; tag: string; note: string }>
}

export interface ObservedSchemaDrift {
  samples: number
  endpoints: number
  compared: number
  drift_count: number
  message: string
  drift: Array<{
    endpoint: string
    method: string
    path: string
    status_code: number
    samples: number
    added: string[]
    removed: string[]
    type_changed: Array<{ field: string; from: string; to: string }>
    root_changed: { from: string; to: string } | null
  }>
}

export interface FindingRecheckCandidate {
  finding_id: number
  title: string
  severity: string
  case_id?: number | null
  endpoints: string[]
  removed_endpoints: string[]
  assignee?: string | null
  resolution_note: string
  resolution_missing: boolean
  updated_at?: string | null
}

export interface FindingRecheckResponse {
  snapshot_id: number | null
  snapshot_version: string
  count: number
  items: FindingRecheckCandidate[]
}

export interface WebhookDelivery {
  id: number
  event: string
  url: string
  status: string
  attempts: number
  last_error?: string | null
  delivered_at?: string | null
  created_at?: string | null
}

export interface WebhookDeliveriesResponse {
  webhook_configured: boolean
  email_configured?: boolean
  notification_configured?: boolean
  items: WebhookDelivery[]
}

export interface ProjectSchedule {
  id: number
  project_id: number
  name: string
  enabled: boolean
  interval_seconds: number
  next_run_at?: string | null
  last_run_at?: string | null
  last_run_id?: number | null
  last_status: string
  last_error: string
  policy: Record<string, any>
  created_at: string
  updated_at: string
}

export interface ProjectMember {
  id: number
  project_id: number
  actor: string
  role: 'owner' | 'maintainer' | 'viewer'
  active: boolean
  note: string
  created_at: string
  updated_at: string
}

export interface AutomationException {
  id: number
  project_id: number
  kind: string
  target_type: string
  target_id?: number | null
  severity: string
  status: string
  owner: string
  note: string
  payload: Record<string, any>
  created_at: string
  updated_at: string
}

export interface SlaBreach {
  finding_id: number
  title: string
  severity: string
  assignee?: string | null
  overdue_minutes: number
  created_at?: string | null
}

export interface SlaBreaches {
  count: number
  unassigned: number
  sla_minutes: Record<string, number>
  items: SlaBreach[]
}

export interface SlaEscalationResult {
  dry_run: boolean
  enabled?: boolean
  count: number
  created: number
  reason?: string
  min_severity?: string
  items: Array<Record<string, any>>
}

export interface AssertionQuality {
  project_id: number
  cases: number
  assertions: number
  average_score: number
  bands: Record<string, number>
  probe_cases?: number
  weakest_cases: Array<Record<string, any>>
  items: Array<Record<string, any>>
}

export interface MutationAudit {
  project_id: number
  results: number
  mutations?: number
  killed?: number
  kill_rate?: number
  [key: string]: any
}

export interface OracleTrendPoint {
  run_id: number
  created_at?: string | null
  results: number
  assertions: number
  pending: number
  oracle_mix: Record<string, number>
  by_source: Record<string, number>
  pass_rate?: number | null
}

export interface PerformanceHistoryItem {
  id: number
  method: string
  path: string
  concurrency: number
  iterations: number
  success_rate: number
  requests_per_second: number
  p50_ms: number
  p95_ms: number
  p99_ms: number
  slo_passed: boolean
  slo: Record<string, any>
  mode: string
  created_at?: string | null
}

export interface PerformanceHistory {
  project_id: number
  count: number
  items: PerformanceHistoryItem[]
  trend: Record<string, number[]>
  degradation: Record<string, any>
}

export interface VisualBaseline {
  id: number
  project_id: number
  case_id: number
  name: string
  sha256: string
  screenshot_path: string
  meta: Record<string, any>
  created_at: string
  updated_at: string
}

export interface GateResult {
  passed: boolean
  status: string
  run_id?: number | null
  run_status?: string
  blockers: Array<Record<string, any>>
  warnings: Array<Record<string, any>>
  summary?: Record<string, any>
  slo_window?: Record<string, any>
}

export interface AutomationHealth {
  project_id?: number
  latest_run?: Record<string, any> | null
  queue?: Record<string, any>
  schedule?: Record<string, any>
  execution?: Record<string, any>
  exceptions?: Record<string, any>
  sla?: Record<string, any>
  [key: string]: any
}



export interface ConsistencyItem {
  endpoint_id: number
  method: string
  path: string
  summary: string
  state?: string
  sources: string[]
  mismatches: Array<{ kind: string; message: string; evidence?: unknown[] }>
  checked_at: string
}

export interface ConsistencySummary {
  endpoints: number
  api_endpoints: number
  states: Record<string, number>
  strong_signals: number
  weak_signals: number
  doc_coverage: number | null
  non_api: number
}

export interface RunBrief {
  id: number
  status: string
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  total: number
  passed: number
  failed: number
  flaky: number
  errored: number
  pass_rate: number
}

export type RunChange =
  | 'broken'
  | 'still_failing'
  | 'fixed'
  | 'new'
  | 'removed'
  | 'changed'
  | 'still_passing'

export interface RunCompareCase {
  case_id: number
  title: string
  dimension: string
  base_status: string | null
  target_status: string | null
  change: RunChange
  base_result_id: number | null
  target_result_id: number | null
}

export interface RunCompare {
  project_id: number
  base: RunBrief
  target: RunBrief
  delta: { pass_rate: number; passed: number; failed: number }
  summary: Record<RunChange, number>
  cases: RunCompareCase[]
}

export interface SpecSnapshot {
  id: number
  source_id: number
  source_name: string
  version: string
  content_hash: string
  created_at: string
  endpoints: number
  origin: string
  diff: {
    added?: Array<{ method: string; path: string; summary?: string }>
    removed?: Array<{ method: string; path: string; summary?: string }>
    changed?: Array<{ method: string; path: string; details?: string[]; old_hash?: string; new_hash?: string }>
  }
  diff_counts: { added: number; removed: number; changed: number }
}

export interface SpecCompare {
  base: { id: number; version: string; source_id: number; created_at: string; endpoints: number }
  target: { id: number; version: string; source_id: number; created_at: string; endpoints: number }
  diff: {
    added: Array<{ method: string; path: string; summary?: string }>
    removed: Array<{ method: string; path: string; summary?: string }>
    changed: Array<{ method: string; path: string; details?: string[] }>
  }
  affected_cases: Array<{ id: number; title: string; status: string }>
  affected_total: number
}
