"""用例 / 执行 / 缺陷 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TestCaseCreate(BaseModel):
    title: str = Field(min_length=1)
    dimension: str = "functional"
    case_type: str = "api"
    feature_id: int | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    priority: int = 100
    tags: list[str] = Field(default_factory=list)
    generation: str = "manual"
    verification_mode: str = "api"


class TestCaseUpdate(BaseModel):
    title: str | None = None
    dimension: str | None = None
    feature_id: int | None = None
    spec: dict[str, Any] | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None
    verification_mode: str | None = None


class TestCaseOut(BaseModel):
    id: int
    project_id: int
    feature_id: int | None
    title: str
    dimension: str
    case_type: str
    status: str
    generation: str
    priority: int
    tags: list[str]
    preconditions: dict[str, Any]
    steps: list[dict[str, Any]]
    expected: dict[str, Any]
    evidence: list[dict[str, Any]]
    spec: dict[str, Any]
    missing_input: str | None
    verification_mode: str = "api"
    verification_status: str = "unverified"
    verification: dict[str, Any] = Field(default_factory=dict)
    verified_at: datetime | None = None
    verified_by: str | None = None
    feature_name: str | None = None
    finding_ids: list[int] = Field(default_factory=list)
    execution_states: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CaseStatusUpdate(BaseModel):
    status: str = Field(description="pending / enabled / disabled / needs_review")
    note: str = ""
    confirm_risk: bool = Field(default=False, description="高风险操作（删除数据 / 修改配置）需要显式确认")


class BatchStatusUpdate(BaseModel):
    case_ids: list[int]
    status: str
    note: str = ""
    confirm_risk: bool = Field(default=False, description="高风险操作需要显式确认")


class CaseValidationRequest(BaseModel):
    case_ids: list[int] | None = Field(default=None, description="为空时按优先级自动挑选")
    limit: int = Field(default=50, ge=1, le=200, description="本批最多试运行多少条")


class CaseRepairRequest(BaseModel):
    case_ids: list[int] | None = Field(default=None, description="为空时按优先级挑选 needs_review")
    limit: int = Field(default=20, ge=1, le=50, description="本批最多修复多少条")
    dry_run: bool = True


class CliGenerateRequest(BaseModel):
    case_ids: list[int] | None = None
    limit: int = Field(default=10, ge=1, le=30)
    dry_run: bool = True


class RunCreate(BaseModel):
    case_ids: list[int] | None = None
    dimensions: list[str] | None = None
    trigger: str = "manual"
    note: str = ""


class RunOut(BaseModel):
    id: int
    project_id: int
    trigger: str
    status: str
    env_snapshot: dict[str, Any]
    stats: dict[str, Any]
    case_ids: list[int]
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    task_id: int | None
    berth_task_id: str | None
    backtest: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class CaseResultOut(BaseModel):
    id: int
    run_id: int
    case_id: int
    project_id: int
    status: str
    request: dict[str, Any]
    response: dict[str, Any]
    duration_ms: float
    logs: str
    screenshot_path: str | None
    trace_path: str | None
    attribution: dict[str, Any]
    rerun: bool
    seq: int
    output: dict[str, Any]
    case_title: str | None = None
    dimension: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FindingOut(BaseModel):
    id: int
    project_id: int
    source: str
    title: str
    description: str
    severity: str
    evidence: list[dict[str, Any]]
    code_location: dict[str, Any]
    result_id: int | None
    case_id: int | None
    module: str
    risk_type: str
    repro: str
    status: str
    confirmed_by: str | None
    confirmed_at: datetime | None
    cluster_key: str | None = None
    triage_status: str = "untriaged"
    triage: dict[str, Any] = Field(default_factory=dict)
    duplicate_of: int | None = None
    fix_status: str = "open"
    fix_verified_at: datetime | None = None
    fix_claim: dict[str, Any] = Field(default_factory=dict)
    resolution: dict[str, Any] = Field(default_factory=dict)
    assignee: str = ""
    created_at: datetime

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence(cls, value):
        """兼容历史上/第三方写入的字符串证据，避免整个 findings 接口 500。"""
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        result = []
        for item in items:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"source_type": "note", "quote": item})
            else:
                result.append({"source_type": "note", "quote": str(item)})
        return result

    model_config = {"from_attributes": True}


class FindingUpdate(BaseModel):
    status: str | None = None
    severity: str | None = None
    title: str | None = None
    description: str | None = None
    confirmed_by: str | None = None
    assignee: str | None = None
    note: str = ""


class FindingMergeRequest(BaseModel):
    duplicate_id: int = Field(description="要合并到当前 finding 的重复项 ID")
    note: str = ""


class FindingBatchUpdate(BaseModel):
    finding_ids: list[int] = Field(min_length=1)
    status: str = Field(description="pending / confirmed / false_positive / accepted / wont_fix")
    assignee: str | None = None
    note: str = ""


class CaseGapUpdate(BaseModel):
    status: str = Field(description="open / claimed / fixed / accepted / wont_fix")
    owner: str = ""
    note: str = ""
    actor: str = "local"


class CaseGapBatchFilters(BaseModel):
    """批量处置的筛选条件；字段为空表示不限制。"""

    kind: str = Field(default="all", description="all / pending / no_steps / missing_input")
    dimension: str | None = None
    feature_id: int | None = None
    evidence_source: str | None = None
    owner: str | None = None
    missing_input_kind: str | None = None
    owner_kind: str | None = Field(default=None, description="test-side / sut-contract / environment")
    gap_status: str | None = None
    execution_state: str | None = Field(
        default=None,
        description="never_executed / enabled_no_result / pending_not_enabled / stale_after_source",
    )
    include_closed: bool = False
    include_disabled: bool = False


class CaseGapBatchUpdate(BaseModel):
    status: str = Field(description="open / claimed / fixed / accepted / wont_fix")
    filters: CaseGapBatchFilters = Field(default_factory=CaseGapBatchFilters)
    case_ids: list[int] | None = Field(default=None, description="为空时作用于全部筛选结果")
    owner: str | None = None
    note: str | None = None
    actor: str = "local"
    dry_run: bool = Field(default=True, description="默认只预览；执行必须显式传 false")


class CaseEnrichBatchRequest(BaseModel):
    """批量重新评估缺口用例；默认只预览，执行必须显式 dry_run=false。"""

    filters: CaseGapBatchFilters = Field(default_factory=CaseGapBatchFilters)
    case_ids: list[int] | None = None
    dry_run: bool = True
    limit: int = Field(default=200, ge=1, le=200)
    actor: str = "local"
    note: str = ""


class AssertionSelection(BaseModel):
    case_id: int
    index: int


class AssertionBatchConfirm(BaseModel):
    selections: list[AssertionSelection] = Field(default_factory=list)
    actor: str = "local"
    note: str = ""


class AssertionBatchRisk(BaseModel):
    case_ids: list[int] = Field(default_factory=list)
    actor: str = "local"
    note: str = ""


class CaseReevaluateRequest(BaseModel):
    snapshot_id: int | None = None
    case_ids: list[int] | None = None
    dry_run: bool = True
    actor: str = "local"
    note: str = ""


class SlaRecomputeRequest(BaseModel):
    case_ids: list[int] | None = None
    dry_run: bool = True
    actor: str = "local"
    note: str = ""


class AssertionMigrationRequest(BaseModel):
    """把无依据 pending 断言迁成 policy 检查或待确认问题。"""

    case_ids: list[int] | None = None
    dry_run: bool = True
    actor: str = "local"
    note: str = ""


class ProbeGenerateRequest(BaseModel):
    """通用 probe 生成：只挂 policy 检查，不依赖项目文档。"""

    endpoint_ids: list[int] | None = None
    limit: int = 40
    dry_run: bool = True


class FindingFixCreate(BaseModel):
    commit_sha: str = ""
    pr_url: str = ""
    files: list[str] = Field(default_factory=list)
    note: str = ""
    test_command: str = ""
    test_result: str = ""
    actor: str = "manual"


class FindingClaim(BaseModel):
    assignee: str = Field(min_length=1, max_length=120)
    note: str = ""


class FindingFixVerify(BaseModel):
    verified: bool
    note: str = ""
    test_command: str = ""
    test_result: str = ""
    actor: str = "manual"
    verification_id: int | None = None


class FindingFixOut(BaseModel):
    id: int
    project_id: int
    finding_id: int
    status: str
    actor: str
    source: str
    commit_sha: str
    pr_url: str
    files: list[str]
    note: str
    test_command: str
    test_result: str
    verification_id: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FindingIntegrationCreate(BaseModel):
    group_key: str = Field(min_length=1, max_length=512)
    dry_run: bool = False


class FindingAutoVerify(BaseModel):
    finding_ids: list[int] | None = None


class CaseVerificationCreate(BaseModel):
    passed: bool
    mode: str | None = None
    command: str = ""
    result: str = ""
    artifacts: list[str] = Field(default_factory=list)
    note: str = ""
    actor: str = "manual"
    # 证据字段：开发 Agent 的报告只入 case_verifications，不改验证状态。
    commit_sha: str = ""
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    env_fingerprint: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None


class CaseVerificationOut(BaseModel):
    id: int
    project_id: int
    case_id: int
    finding_id: int | None
    mode: str
    status: str
    actor: str
    source: str
    command: str
    result: str
    artifacts: list[str]
    note: str
    evidence_level: str
    commit_sha: str
    artifact_hashes: dict[str, str]
    env_fingerprint: dict[str, Any]
    expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AttributionUpdate(BaseModel):
    category: str
    reason: str = ""
    note: str = ""


class FeedbackCreate(BaseModel):
    target_type: str
    target_id: int
    action: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    author: str = "local"


class FeedbackOut(BaseModel):
    id: int
    project_id: int
    target_type: str
    target_id: int
    action: str
    before: dict[str, Any]
    after: dict[str, Any]
    note: str
    author: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentTraceOut(BaseModel):
    id: int
    project_id: int | None
    run_id: int | None
    task_id: int | None
    kind: str
    step: str
    model: str
    input: dict[str, Any]
    output: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    duration_ms: int
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskOut(BaseModel):
    id: int
    project_id: int | None
    run_id: int | None
    kind: str
    status: str
    priority: int
    payload: dict[str, Any]
    result: dict[str, Any]
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    error: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}
