"""全局枚举：与 todo.md 第 2 节的字段取值保持一致。"""
from __future__ import annotations

from enum import StrEnum


class KnowledgeKind(StrEnum):
    CODE = "code"
    MARKDOWN = "markdown"
    OPENAPI = "openapi"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    READY = "ready"
    ERROR = "error"


class EndpointSource(StrEnum):
    OPENAPI = "openapi"
    CODE = "code"
    DOC = "doc"


class ConsistencyState(StrEnum):
    UNKNOWN = "unknown"          # 只有一个来源，未做对照
    CONSISTENT = "consistent"    # 三方（或实际存在的来源）一致
    INCONSISTENT = "inconsistent"
    CONFLICT = "conflict"        # 多个来源给出的接口签名互相冲突


class Dimension(StrEnum):
    FUNCTIONAL = "functional"
    BOUNDARY = "boundary"
    SECURITY = "security"
    PERFORMANCE = "performance"


class CaseType(StrEnum):
    API = "api"
    WEB = "web"


class GenerationSource(StrEnum):
    AGENT = "agent"
    MANUAL = "manual"


class CaseStatus(StrEnum):
    PENDING = "pending"        # 待确认（Agent 生成默认态）
    ENABLED = "enabled"        # 人工启用，参与执行
    DISABLED = "disabled"      # 人工停用
    NEEDS_REVIEW = "needs_review"  # 依赖变更（spec diff 等）需要重新确认


class RunTrigger(StrEnum):
    MANUAL = "manual"
    BACKTEST = "backtest"
    SCHEDULE = "schedule"
    VALIDATION = "validation"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CaseResultStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    FLAKY = "flaky"
    ERROR = "error"
    SKIPPED = "skipped"


class AttributionCategory(StrEnum):
    ENVIRONMENT = "environment"  # 环境问题：请求没到被测系统 / 沙箱不可用
    PLATFORM = "platform"        # 平台能力缺口：执行器、断言归一化、变量渲染
    CASE = "case"                # 用例问题：期望或步骤写错
    DEFECT = "defect"            # 疑似真实缺陷：5xx / 泄露实现细节
    EVIDENCE = "evidence"        # 依据不足：缺契约、缺入口，构造不出可执行用例
    UNCONFIRMED = "unconfirmed"  # 规则与模型都拿不准


class FindingSource(StrEnum):
    EXECUTION = "execution"
    WHITEBOX = "whitebox"
    EXTERNAL = "external"


class FindingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    ACCEPTED = "accepted"          # 已知问题，接受风险
    WONT_FIX = "wont_fix"          # 明确不修复


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class TaskKind(StrEnum):
    PLAN_FEATURES = "plan_features"
    GENERATE_CASES = "generate_cases"
    EXECUTE_RUN = "execute_run"
    ANALYZE_RUN = "analyze_run"
    WHITEBOX_REVIEW = "whitebox_review"
    ENRICH_CASE = "enrich_case"
    VERIFY_FIXES = "verify_fixes"
    TRIAGE_FINDINGS = "triage_findings"
    BACKTEST = "backtest"
    REPARSE_SOURCE = "reparse_source"
    DELIVER_WEBHOOK = "deliver_webhook"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FeedbackTarget(StrEnum):
    CASE = "case"
    RESULT = "result"
    FINDING = "finding"
    ATTRIBUTION = "attribution"


class FeedbackAction(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    ENABLE = "enable"
    DISABLE = "disable"
    CONFIRM = "confirm"
    FALSE_POSITIVE = "false_positive"
    ACCEPT = "accept"
    WONT_FIX = "wont_fix"
    COMMENT = "comment"


class GapStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    FIXED = "fixed"
    ACCEPTED = "accepted"
    WONT_FIX = "wont_fix"
