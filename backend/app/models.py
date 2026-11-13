"""ORM 模型 —— 对应 todo.md 第 2 节的数据模型。

约定：
- 所有表带 id / created_at / updated_at；
- 半结构化数据一律 JSONB；
- 向量字段用 pgvector（`chunk.embedding`）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db import Base

EMBEDDING_DIM = get_settings().embedding_dim


class TimestampMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Project(Base, TimestampMixin):
    """测试工作空间：一个项目 = 一套被测环境 + 一套知识源 + 一套用例。"""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # 租户隔离：MCP client 与项目 tenant 不一致时拒绝访问；空/ default 表示默认租户。
    tenant: Mapped[str] = mapped_column(String(64), default="default", server_default="default", nullable=False, index=True)
    # 被测系统对外地址
    base_url: Mapped[str] = mapped_column(String(500), default="")
    # 被测系统启动方式：{"type": "docker"|"command"|"external", ...}
    target_start: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # 隔离配置：{"db_service": "proj-x-db", "db_port": 15xxx, "init_script": "...", "mode": "reset"|"prefix"}
    isolation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # 加密存储：{"username": "...", "password": "...", "token": "..."}
    test_accounts_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 加密存储：被测库 DSN（用于数据库状态断言）
    target_dsn_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 其它项目级配置（超时、SLA 等）
    settings_json: Mapped[dict[str, Any]] = mapped_column("settings", JSONB, default=dict)

    knowledge_sources: Mapped[list["KnowledgeSource"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    test_cases: Mapped[list["TestCase"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    endpoints: Mapped[list["Endpoint"]] = relationship(back_populates="project", cascade="all, delete-orphan")
    features: Mapped[list["Feature"]] = relationship(back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_projects_name", "name"),)


class ProjectMember(Base, TimestampMixin):
    """项目角色矩阵：owner / maintainer / viewer，用于负责人和通知路由。"""

    __tablename__ = "project_members"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default="viewer", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "actor", name="uq_project_member_actor"),
    )


class ProjectSchedule(Base, TimestampMixin):
    """项目级无人值守调度：定时 / 部署后触发一次 run。"""

    __tablename__ = "project_schedules"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=86400, nullable=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_status: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # {"mode":"full|smoke|impacted","dimensions":[],"max_cases":200,"priorities":[]}
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (Index("ix_project_schedules_due", "enabled", "next_run_at"),)


class KnowledgeSource(Base, TimestampMixin):
    """知识源：本地代码库 / Markdown 文档 / OpenAPI spec。"""

    __tablename__ = "knowledge_sources"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # code/markdown/openapi
    # spec = 接口规范（能作为断言依据）；notes = 设计笔记 / 开发日志（只用于理解设计意图）
    role: Mapped[str] = mapped_column(String(16), default="spec", index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    # 本地路径或 URL
    location: Mapped[str] = mapped_column(Text, nullable=False)
    # 版本标识：commit hash / 内容 hash
    version: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 附加信息：{"files": 12, "chunks": 30, "spec_hash": "..."}
    meta_json: Mapped[dict[str, Any]] = mapped_column("meta", JSONB, default=dict)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="knowledge_sources")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    snapshots: Mapped[list["SpecSnapshot"]] = relationship(back_populates="source", cascade="all, delete-orphan")

    @property
    def meta(self) -> dict[str, Any]:
        """列名是 meta，Python 属性名是 meta_json；这里给 API 层一个直观别名。

        下划线开头的键是内部结构快照等大对象，不通过知识源 API 暴露。
        """
        return {
            key: value
            for key, value in (self.meta_json or {}).items()
            if not key.startswith("_")
        }

    @property
    def reevaluate_candidates(self) -> int:
        """解析后提示：当前项目里缺说明/缺步骤、值得重新评估的用例数。"""
        return int((self.meta_json or {}).get("reevaluate_candidates") or 0)

    __table_args__ = (Index("ix_knowledge_sources_project_kind", "project_id", "kind"),)


class Chunk(Base, TimestampMixin):
    """知识切片：带向量，用于检索。"""

    __tablename__ = "chunks"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 来源位置：{"file_path": ..., "start_line": ..., "end_line": ...} 或 {"section_path": [...]}
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    meta_json: Mapped[dict[str, Any]] = mapped_column("meta", JSONB, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    embedding: Mapped[Optional[Any]] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    source: Mapped[KnowledgeSource] = relationship(back_populates="chunks")

    __table_args__ = (Index("ix_chunks_project_hash", "project_id", "content_hash"),)


class Endpoint(Base, TimestampMixin):
    """归一化后的接口清单（多来源合并）。"""

    __tablename__ = "endpoints"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    # 参数：{"path": [...], "query": [...], "header": [...]}
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    request_body: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    responses: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    security: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    examples: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # 来源：["openapi", "code", "doc"] + 细节 {"openapi": {"ref": "#/paths/~1users/get"}}
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # 一致性状态：{"state": "consistent", "mismatches": [...], "checked_at": ...}
    consistency: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")

    project: Mapped[Project] = relationship(back_populates="endpoints")

    __table_args__ = (
        UniqueConstraint("project_id", "method", "path", name="uq_endpoints_project_method_path"),
        Index("ix_endpoints_project_path", "project_id", "path"),
    )


class Feature(Base, TimestampMixin):
    """功能点（Agent 从文档 + 接口 + 代码抽取）。"""

    __tablename__ = "features"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    module: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # 来源引用：[{"type": "doc", "ref": "docs/api.md#登录", "quote": "..."}]
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # 关联接口：[endpoint_id, ...]
    endpoint_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    unmapped: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    project: Mapped[Project] = relationship(back_populates="features")


class TestCase(Base, TimestampMixin):
    """测试用例（真身在数据库，执行器据此生成代码）。"""

    __tablename__ = "test_cases"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    feature_id: Mapped[Optional[int]] = mapped_column(ForeignKey("features.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    case_type: Mapped[str] = mapped_column(String(16), nullable=False)  # api/web
    # 前置条件 / 步骤 / 预期结果 / 预期依据
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    expected: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # 完整统一用例 JSON（执行器只认这个）
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    generation: Mapped[str] = mapped_column(String(16), default="agent")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    data_template: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    missing_input: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 验证方式：api / process / artifact / frontend / privileged / manual
    verification_mode: Mapped[str] = mapped_column(String(16), default="api", nullable=False, index=True)
    # 验证状态：unverified / passed_external / failed_external（只由 Shakedown 写）
    verification_status: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    project: Mapped[Project] = relationship(back_populates="test_cases")

    __table_args__ = (Index("ix_test_cases_project_status", "project_id", "status"),)


class CaseVerification(Base, TimestampMixin):
    """用例外部验证记录（构建产物 / 进程 / 浏览器 / 权限等非 API 验证）。"""

    __tablename__ = "case_verifications"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    finding_id: Mapped[Optional[int]] = mapped_column(ForeignKey("findings.id", ondelete="SET NULL"), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="manual")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    command: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")
    artifacts: Mapped[list[str]] = mapped_column(JSONB, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    # 证据等级：self_reported（外部自报）/ replayed（Shakedown 独立重放）/ manual（人工复核）。
    evidence_level: Mapped[str] = mapped_column(String(24), default="self_reported", nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    # 产物 hash 由调用方上传时计算；客户端自报的 hash 只作对照，不作为验证依据。
    artifact_hashes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    env_fingerprint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base, TimestampMixin):
    """一次执行。"""

    __tablename__ = "runs"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # 环境快照：base_url、db 信息、沙箱配置
    env_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    case_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    # 与 tasks.run_id 互相引用，用 use_alter 打破建表顺序循环
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", use_alter=True, name="fk_runs_task_id"), nullable=True
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    berth_task_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    backtest: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    results: Mapped[list["CaseResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class CaseResult(Base, TimestampMixin):
    """单条用例的执行结果。"""

    __tablename__ = "case_results"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    logs: Mapped[str] = mapped_column(Text, default="")
    screenshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trace_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 失败归因：{"category": "defect", "reason": "...", "evidence": [...], "confidence": 0.7}
    attribution: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    rerun: Mapped[bool] = mapped_column(Boolean, default=False)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    run: Mapped[Run] = relationship(back_populates="results")

    __table_args__ = (Index("ix_case_results_run_status", "run_id", "status"),)


class Finding(Base, TimestampMixin):
    """缺陷线索：黑盒执行发现 或 白盒走查发现。"""

    __tablename__ = "findings"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), default="execution")
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="medium")
    # 依据：断言 / 文档章节 / 代码位置
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    code_location: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result_id: Mapped[Optional[int]] = mapped_column(ForeignKey("case_results.id", ondelete="SET NULL"), nullable=True)
    case_id: Mapped[Optional[int]] = mapped_column(ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    module: Mapped[str] = mapped_column(String(200), default="")
    risk_type: Mapped[str] = mapped_column(String(64), default="")
    repro: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # AI 分诊：只写派生信息，原始标题/描述/证据/代码位置永不覆盖
    cluster_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    triage_status: Mapped[str] = mapped_column(String(32), default="untriaged", nullable=False, index=True)
    triage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    duplicate_of: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 修复生命周期：open / claimed_fixed / verified_fixed / reopened
    fix_status: Mapped[str] = mapped_column(String(40), default="open", nullable=False, index=True)
    fix_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fix_claim: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # 处置：accepted / wont_fix 等关闭原因、指派人与备注
    resolution: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    assignee: Mapped[str] = mapped_column(String(120), default="", nullable=False)


class CaseGapState(Base, TimestampMixin):
    """契约缺口的人工处置状态（接受风险 / 不修复 / 认领 / 已修复）。"""

    __tablename__ = "case_gap_states"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (UniqueConstraint("case_id", name="uq_case_gap_states_case"),)


class CaseGapAudit(Base, TimestampMixin):
    """缺口处置审计：谁、何时、从什么状态改到什么状态、原因。

    只追加不覆盖；单条处置和批量处置都会写一条。
    """

    __tablename__ = "case_gap_audits"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    owner: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor: Mapped[str] = mapped_column(String(120), default="local", nullable=False)
    source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_case_gap_audits_project_created", "project_id", "created_at"),
        Index("ix_case_gap_audits_case_created", "case_id", "created_at"),
    )


class FindingFix(Base, TimestampMixin):
    """缺陷修复声明 / 验证历史；原始 finding 永远不覆盖。"""

    __tablename__ = "finding_fixes"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), default="claimed_fixed", nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), default="manual")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    pr_url: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[list[str]] = mapped_column(JSONB, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    test_command: Mapped[str] = mapped_column(Text, default="")
    test_result: Mapped[str] = mapped_column(Text, default="")
    # 验证结论引用的证据记录；自报通过不会写 verified_fixed，只有独立重放/人工复核才写。
    verification_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("case_verifications.id", ondelete="SET NULL", name="fk_finding_fixes_verification_id"),
        nullable=True,
    )

    __table_args__ = (Index("ix_finding_fixes_finding_created", "finding_id", "created_at"),)


class MetricCounter(Base, TimestampMixin):
    """跨进程 / 多 worker 聚合的计数器（HTTP 指标等）。"""

    __tablename__ = "metric_counters"

    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    labels_key: Mapped[str] = mapped_column(String(500), nullable=False)
    value: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    __table_args__ = (UniqueConstraint("name", "labels_key", name="uq_metric_counter_name_labels"),)


class PerformanceRun(Base, TimestampMixin):
    """一次性能采样结果，用于趋势和 SLO 退化检测。"""

    __tablename__ = "performance_runs"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(16), default="GET", nullable=False)
    path: Mapped[str] = mapped_column(Text, default="", nullable=False, index=True)
    concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    requests_per_second: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    p50_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    p95_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    p99_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    slo_passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    slo: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class VisualBaseline(Base, TimestampMixin):
    """视觉基线：保存浏览器用例的截图 hash，用于后续回归比较。"""

    __tablename__ = "visual_baselines"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), default="default", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    screenshot_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("project_id", "case_id", "name", name="uq_visual_baseline_case_name"),
    )


class AutomationException(Base, TimestampMixin):
    """无人值守例外队列：环境、低置信度分诊、无法自动验证等需要人处理的事项。"""

    __tablename__ = "automation_exceptions"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    target_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_automation_exceptions_project_status", "project_id", "status", "severity"),
        Index("ix_automation_exceptions_target", "target_type", "target_id"),
    )


class IntegrationIssue(Base, TimestampMixin):
    """缺陷组已创建的外部工单（GitHub Issue / Jira），用于幂等和回链。"""

    __tablename__ = "integration_issues"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    group_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(500), default="")
    external_key: Mapped[str] = mapped_column(String(200), default="")
    external_url: Mapped[str] = mapped_column(Text, default="")
    finding_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="open")

    __table_args__ = (
        UniqueConstraint("project_id", "provider", "group_key", name="uq_integration_issue_group"),
    )


class McpClient(Base, TimestampMixin):
    """MCP HTTP 接入客户端：只存 token 哈希，明文只在创建时返回一次。"""

    __tablename__ = "mcp_clients"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(24), default="")
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=lambda: ["read"])
    # 空列表表示不限制项目；非空表示只能访问这些 project_id
    project_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    # 租户：非空时只能访问相同 tenant 的项目
    tenant: Mapped[str] = mapped_column(String(64), default="", server_default="", nullable=False)
    # 绑定 ProjectMember.actor；为空时用 client name 匹配成员
    actor: Mapped[str] = mapped_column(String(120), default="", server_default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTrace(Base, TimestampMixin):
    """Agent 每次规划 / 生成 / 分析的输入输出与工具调用。"""

    __tablename__ = "agent_traces"

    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="chat")
    step: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class HumanFeedback(Base, TimestampMixin):
    """人工修正记录：用于后续优化与审计。"""

    __tablename__ = "human_feedbacks"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(120), default="local")


class Task(Base, TimestampMixin):
    """后台任务（数据库驱动的状态机 + worker 队列）。"""

    __tablename__ = "tasks"

    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AppSetting(Base):
    """全局设置（可视化配置）：LLM provider、沙箱模式等。

    值存在数据库里，覆盖同名的环境变量；改完即时生效（不用重启）。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = ()


class WebhookDelivery(Base, TimestampMixin):
    """项目级 webhook 投递记录：失败可重试，不影响执行主流程。"""

    __tablename__ = "webhook_deliveries"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_webhook_deliveries_project_created", "project_id", "created_at"),)


class SpecSnapshot(Base, TimestampMixin):
    """OpenAPI spec 快照，用于重新拉取后的 diff。"""

    __tablename__ = "spec_snapshots"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(128), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # diff 结果：{"added": [...], "removed": [...], "changed": [...]}
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    source: Mapped[KnowledgeSource] = relationship(back_populates="snapshots")
