"""用例依据缺口：待确认断言、没有步骤、待补全说明。

这里把散落在各条用例里的「缺依据 / 缺步骤」统一汇总，
用于导出给被测系统的维护者或其授权的编码 Agent，而不是让它们淹没在用例列表里。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.case_state import executed_case_ids, latest_source_parsed_at, states_for_case
from app.executor.case_schema import assertion_refs, collect_assertions, collect_contract_questions
from app.models import CaseGapAudit, CaseGapState, Feature, HumanFeedback, Project, TestCase

CLOSED_GAP_STATUSES = {"accepted", "wont_fix"}
GAP_STATUSES = {"open", "claimed", "fixed", "accepted", "wont_fix"}
# 缺口归属：测试侧资产 / 被测系统契约 / 环境。只有 sut-contract 会进入 SUT 报告。
OWNER_KINDS = {"test-side", "sut-contract", "environment"}
MISSING_INPUT_KINDS = {"sla", "response_schema", "auth", "api_param", "environment", "other"}

_SLA_MARKERS = ("sla", "响应时间", "响应时延", "性能", "延迟", "latency", "阈值", "p95", "耗时")
_SCHEMA_MARKERS = (
    "schema",
    "响应体",
    "响应结构",
    "响应 schema",
    "返回结构",
    "字段",
    "response",
    "maxlength",
)
_AUTH_MARKERS = ("鉴权", "认证", "权限", "token", "登录", "authorization", "越权", "cookie", "session")
_API_MARKERS = ("接口", "参数", "路径", "endpoint", "必填", "query", "body")
_ENV_MARKERS = ("环境", "账号", "网络", "依赖", "数据库", "db", "服务未启动", "端口")


def classify_missing_input(text: Any, *, dimension: str | None = None) -> str:
    """把自由文本的 missing_input 归到可批量处置的类别。

    SLA 只在性能维度成立：其他维度即使顺带提到「无 SLA / 超时阈值」，
    也不是该用例的主缺口，应按响应 schema / 鉴权 / 接口参数等主因归类。
    """
    value = str(text or "").strip().lower()
    if not value:
        return ""
    if dimension == "performance" and any(marker in value for marker in _SLA_MARKERS):
        return "sla"
    if any(marker in value for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in value for marker in _SCHEMA_MARKERS):
        return "response_schema"
    if any(marker in value for marker in _ENV_MARKERS):
        return "environment"
    if any(marker in value for marker in _API_MARKERS):
        return "api_param"
    return "other"


def _is_sla_missing(text: Any) -> bool:
    """判断文本是否在说「缺少 SLA」；调用方需自行保证用例属于性能维度。"""
    value = str(text or "").strip().lower()
    return bool(value) and any(marker in value for marker in _SLA_MARKERS)


def _assertion_owner_kind(assertion: dict[str, Any]) -> str:
    evidence = assertion.get("evidence") or {}
    source = str(evidence.get("source_type") or "")
    if source in {"openapi", "code", "doc"} and evidence.get("ref"):
        return "test-side"
    description = f"{assertion.get('description') or ''} {assertion.get('kind') or ''}".lower()
    if any(marker in description for marker in ("schema", "响应", "response", "字段", "鉴权", "auth")):
        return "sut-contract"
    return "test-side"


def gap_owner_kinds(item: dict[str, Any]) -> list[str]:
    """一条用例可能同时有测试债和契约缺口；这里给出全部归属。"""
    kinds: list[str] = []
    missing_kind = item.get("missing_input_kind") or ""
    if missing_kind == "sla" or item.get("no_steps"):
        kinds.append("test-side")
    if missing_kind == "environment":
        kinds.append("environment")
    if missing_kind in {"response_schema", "auth", "api_param"}:
        kinds.append("sut-contract")
    for assertion in item.get("pending_assertions") or []:
        kinds.append(_assertion_owner_kind(assertion))
    for question in item.get("contract_questions") or []:
        kinds.append(_question_owner_kind(question))
    if not kinds:
        kinds.append("test-side")
    return sorted(set(kinds))


def _question_owner_kind(question: dict[str, Any]) -> str:
    owner = str(question.get("owner_kind") or "")
    return owner if owner in OWNER_KINDS else "sut-contract"


def _primary_owner_kind(item: dict[str, Any], kinds: list[str]) -> str:
    # 优先级：环境 > 契约缺口（鉴权/schema/接口参数）> 测试侧（SLA/无步骤）> 测试侧兜底。
    # 无步骤往往正是「契约没定义」导致的结果，不能无条件把主归属判给测试侧。
    if "environment" in kinds:
        return "environment"
    missing_kind = item.get("missing_input_kind") or ""
    if missing_kind in {"auth", "response_schema", "api_param"} and "sut-contract" in kinds:
        return "sut-contract"
    if missing_kind == "sla" or item.get("no_steps"):
        return "test-side"
    if "sut-contract" in kinds:
        return "sut-contract"
    return "test-side"


def assertion_is_pending(assertion: dict[str, Any]) -> bool:
    """与运行时判定保持一致：未确认或没有可引用来源都算待确认。"""
    evidence = assertion.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    return assertion.get("status") != "confirmed" or evidence.get("source_type") in (None, "", "none")


def _endpoint_of_step(case: TestCase, step_index: Any) -> str:
    steps = case.steps or []
    if not isinstance(step_index, int) or not (0 <= step_index < len(steps)):
        return ""
    step = steps[step_index] if isinstance(steps[step_index], dict) else {}
    method = str(step.get("method") or "").upper()
    path = str(step.get("path") or "")
    return f"{method} {path}".strip()


def _default_endpoint(case: TestCase) -> str:
    """顶层补充断言没有 step_index，按用例首个请求步骤归属接口。"""
    for step in case.steps or []:
        if not isinstance(step, dict) or not step.get("method"):
            continue
        method = str(step.get("method") or "").upper()
        path = str(step.get("path") or "")
        return f"{method} {path}".strip()
    return ""


def _pending_assertions(case: TestCase) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    default_endpoint = _default_endpoint(case)
    for index, raw in enumerate(collect_assertions(case.spec or {})):
        if not assertion_is_pending(raw):
            continue
        evidence = raw.get("evidence") or {}
        if not isinstance(evidence, dict):
            evidence = {}
        source_type = str(evidence.get("source_type") or "")
        assertions.append(
            {
                "index": index,
                "kind": raw.get("kind") or "",
                "description": raw.get("description") or "",
                "operator": raw.get("operator") or "eq",
                "expected": raw.get("expected"),
                "step_index": raw.get("step_index"),
                "endpoint": _endpoint_of_step(case, raw.get("step_index")) or default_endpoint,
                "confirmable": bool(source_type in {"openapi", "code", "doc"} and evidence.get("ref")),
                "evidence": {
                    "source_type": source_type,
                    "ref": evidence.get("ref") or "",
                    "quote": evidence.get("quote") or "",
                    "note": evidence.get("note") or "",
                },
            }
        )
    return assertions


def collect_case_gaps(db: Session, project: Project, *, include_disabled: bool = False) -> list[dict[str, Any]]:
    """返回有缺口的用例（一条用例一条记录，内部列出所有缺口）。

    处置状态来自 case_gap_states；没有记录的视为 open。
    """
    stmt = select(TestCase).where(TestCase.project_id == project.id)
    if not include_disabled:
        stmt = stmt.where(TestCase.status != "disabled")
    cases = list(db.scalars(stmt.order_by(TestCase.updated_at.desc(), TestCase.id.desc())).all())
    feature_ids = [case.feature_id for case in cases if case.feature_id]
    features = {
        feature.id: feature.name
        for feature in db.scalars(select(Feature).where(Feature.id.in_(feature_ids))).all()
    } if feature_ids else {}
    states = {
        state.case_id: state
        for state in db.scalars(
            select(CaseGapState).where(CaseGapState.project_id == project.id)
        ).all()
    }
    executed = executed_case_ids(db, project.id, [case.id for case in cases])
    latest_parsed_at = latest_source_parsed_at(db, project.id)

    items: list[dict[str, Any]] = []
    for case in cases:
        pending = _pending_assertions(case)
        questions = collect_contract_questions(case.spec or {})
        no_steps = not (case.steps or [])
        if not pending and not questions and not case.missing_input and not no_steps:
            continue
        state = states.get(case.id)
        item = {
            "case_id": case.id,
            "title": case.title,
            "status": case.status,
            "dimension": case.dimension,
            "feature_id": case.feature_id,
            "feature_name": features.get(case.feature_id or 0),
            "step_count": len(case.steps or []),
            "assertion_count": len(collect_assertions(case.spec or {})),
            "contract_questions": questions,
            "question_count": len(questions),
            "probe": bool((case.spec or {}).get("probe")),
            "missing_input": case.missing_input,
            "missing_input_kind": classify_missing_input(case.missing_input, dimension=case.dimension),
            "no_steps": no_steps,
            "pending_assertions": pending,
            "gap_status": state.status if state else "open",
            "gap_owner": state.owner if state else "",
            "gap_note": state.note if state else "",
            "gap_updated_at": state.updated_at.isoformat() if state and state.updated_at else None,
            "execution_states": states_for_case(
                case,
                executed=executed,
                latest_parsed_at=latest_parsed_at,
            ),
            "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        }
        kinds = gap_owner_kinds(item)
        item["owner_kinds"] = kinds
        item["owner_kind"] = _primary_owner_kind(item, kinds)
        items.append(item)
    return items


def gap_facets(items: list[dict[str, Any]]) -> dict[str, Any]:
    """缺口分类统计，供批量处置按类型筛选。"""
    def _count(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            raw = item.get(key)
            for value in raw if isinstance(raw, list) else [raw]:
                label = str(value or "unknown")
                counts[label] = counts.get(label, 0) + 1
        return counts

    by_feature: dict[str, int] = {}
    for item in items:
        label = str(item.get("feature_name") or "未映射功能点")
        by_feature[label] = by_feature.get(label, 0) + 1
    return {
        "by_missing_input_kind": _count("missing_input_kind"),
        "by_owner_kind": _count("owner_kind"),
        "by_dimension": _count("dimension"),
        "by_execution_state": _count("execution_states"),
        "by_feature": by_feature,
        "by_evidence_source": _count_evidence_source(items),
        "by_question_owner_kind": _count_question_owner(items),
    }


def _count_question_owner(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for question in item.get("contract_questions") or []:
            owner = _question_owner_kind(question)
            counts[owner] = counts.get(owner, 0) + 1
    return counts


def _count_evidence_source(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for assertion in item.get("pending_assertions") or []:
            source = str((assertion.get("evidence") or {}).get("source_type") or "none")
            counts[source] = counts.get(source, 0) + 1
    return counts


def gap_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["gap_status"]] = by_status.get(item["gap_status"], 0) + 1
    return {
        "cases": len(items),
        "pending_assertions": sum(len(item["pending_assertions"]) for item in items),
        "contract_questions": sum(item.get("question_count", 0) for item in items),
        "cases_with_questions": sum(1 for item in items if item.get("contract_questions")),
        "cases_with_pending": sum(1 for item in items if item["pending_assertions"]),
        "cases_without_steps": sum(1 for item in items if item["no_steps"]),
        "cases_with_missing_input": sum(1 for item in items if item["missing_input"]),
        "by_status": by_status,
    }


def filter_gaps(
    items: list[dict[str, Any]],
    kind: str,
    *,
    include_closed: bool = False,
    dimension: str | None = None,
    feature_id: int | None = None,
    evidence_source: str | None = None,
    owner: str | None = None,
    missing_input_kind: str | None = None,
    owner_kind: str | None = None,
    gap_status: str | None = None,
    execution_state: str | None = None,
) -> list[dict[str, Any]]:
    rows = items
    if not include_closed:
        rows = [item for item in rows if item["gap_status"] not in CLOSED_GAP_STATUSES]
    if kind == "pending":
        rows = [item for item in rows if item["pending_assertions"]]
    elif kind == "questions":
        rows = [item for item in rows if item.get("contract_questions")]
    elif kind == "no_steps":
        rows = [item for item in rows if item["no_steps"]]
    elif kind == "missing_input":
        rows = [item for item in rows if item["missing_input"]]
    if dimension:
        rows = [item for item in rows if item.get("dimension") == dimension]
    if feature_id is not None:
        rows = [item for item in rows if item.get("feature_id") == feature_id]
    if evidence_source:
        rows = [
            item
            for item in rows
            if any(
                (assertion.get("evidence") or {}).get("source_type") == evidence_source
                for assertion in item["pending_assertions"]
            )
        ]
    if owner:
        rows = [item for item in rows if owner in str(item.get("gap_owner") or "")]
    if missing_input_kind:
        rows = [item for item in rows if item.get("missing_input_kind") == missing_input_kind]
    if owner_kind:
        rows = [item for item in rows if owner_kind in (item.get("owner_kinds") or [item.get("owner_kind")])]
    if gap_status:
        rows = [item for item in rows if item.get("gap_status") == gap_status]
    if execution_state:
        state = "never_executed" if execution_state == "never" else execution_state
        rows = [item for item in rows if state in (item.get("execution_states") or [])]
    return rows


def _upsert_gap_state(
    db: Session,
    project: Project,
    case: TestCase,
    *,
    status: str,
    owner: str | None,
    note: str | None,
    actor: str,
    source: str,
    detail: dict[str, Any] | None = None,
) -> CaseGapState:
    if status not in GAP_STATUSES:
        raise ValueError(f"非法缺口状态：{status}")
    state = db.scalar(select(CaseGapState).where(CaseGapState.case_id == case.id))
    if state is None:
        state = CaseGapState(project_id=project.id, case_id=case.id, status="open")
        db.add(state)
        db.flush()
    from_status = state.status or "open"
    state.status = status
    # 约定：显式传非空值才覆盖，空值不清空已有负责人 / 备注。
    if owner:
        state.owner = owner.strip()[:120]
    if note:
        state.note = note.strip()
    db.add(
        CaseGapAudit(
            project_id=project.id,
            case_id=case.id,
            from_status=from_status,
            to_status=status,
            owner=state.owner or "",
            note=state.note or "",
            actor=actor or "local",
            source=source,
            detail=detail or {},
        )
    )
    return state


def set_case_gap_state(
    db: Session,
    project: Project,
    case_id: int,
    *,
    status: str,
    owner: str | None = "",
    note: str | None = "",
    actor: str = "local",
    source: str = "manual",
    detail: dict[str, Any] | None = None,
) -> CaseGapState:
    if status not in GAP_STATUSES:
        raise ValueError(f"非法缺口状态：{status}")
    case = db.get(TestCase, case_id)
    if case is None or case.project_id != project.id:
        raise ValueError(f"用例不存在或不属于该项目：{case_id}")
    state = _upsert_gap_state(
        db, project, case, status=status, owner=owner, note=note, actor=actor, source=source, detail=detail
    )
    db.commit()
    db.refresh(state)
    return state


def batch_set_case_gap_state(
    db: Session,
    project: Project,
    *,
    filters: dict[str, Any],
    status: str,
    owner: str | None = None,
    note: str | None = None,
    case_ids: list[int] | None = None,
    actor: str = "local",
    source: str = "batch",
) -> dict[str, Any]:
    """按筛选条件批量处置缺口；case_ids 为空时作用于全部筛选结果。"""
    items = collect_case_gaps(
        db,
        project,
        include_disabled=bool(filters.get("include_disabled")),
    )
    matched = filter_gaps(
        items,
        str(filters.get("kind") or "all"),
        include_closed=bool(filters.get("include_closed")),
        dimension=filters.get("dimension") or None,
        feature_id=filters.get("feature_id"),
        evidence_source=filters.get("evidence_source") or None,
        owner=filters.get("owner") or None,
        missing_input_kind=filters.get("missing_input_kind") or None,
        owner_kind=filters.get("owner_kind") or None,
        gap_status=filters.get("gap_status") or None,
    )
    if case_ids:
        wanted = {int(case_id) for case_id in case_ids}
        matched = [item for item in matched if item["case_id"] in wanted]
    updated: list[int] = []
    for item in matched:
        case = db.get(TestCase, item["case_id"])
        if case is None or case.project_id != project.id:
            continue
        _upsert_gap_state(
            db,
            project,
            case,
            status=status,
            owner=owner,
            note=note,
            actor=actor,
            source=source,
            detail={"filters": filters, "batch": True},
        )
        updated.append(case.id)
    db.commit()
    return {
        "matched": len(matched),
        "updated": len(updated),
        "case_ids": updated,
    }


def list_case_gap_audits(
    db: Session,
    project: Project,
    case_id: int,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CaseGapAudit)
        .where(CaseGapAudit.project_id == project.id, CaseGapAudit.case_id == case_id)
        .order_by(CaseGapAudit.created_at.desc(), CaseGapAudit.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": row.id,
            "from_status": row.from_status,
            "to_status": row.to_status,
            "owner": row.owner,
            "note": row.note,
            "actor": row.actor,
            "source": row.source,
            "detail": row.detail or {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]



ASSERTION_GROUP_KEYS = {"endpoint", "kind", "evidence_source", "feature", "owner_kind"}


def group_pending_assertions(
    items: list[dict[str, Any]],
    group_by: str = "endpoint",
) -> list[dict[str, Any]]:
    """把待确认断言按接口 / 类型 / 证据来源 / 功能点聚合，供批量复核。"""
    if group_by not in ASSERTION_GROUP_KEYS:
        group_by = "endpoint"
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        for assertion in item.get("pending_assertions") or []:
            if group_by == "endpoint":
                key = assertion.get("endpoint") or "未指定接口"
            elif group_by == "kind":
                key = assertion.get("kind") or "unknown"
            elif group_by == "evidence_source":
                key = (assertion.get("evidence") or {}).get("source_type") or "none"
            elif group_by == "owner_kind":
                key = item.get("owner_kind") or "test-side"
            else:
                key = item.get("feature_name") or "未映射功能点"
            group = groups.setdefault(
                key,
                {"key": key, "count": 0, "confirmable": 0, "assertions": []},
            )
            group["count"] += 1
            if assertion.get("confirmable"):
                group["confirmable"] += 1
            group["assertions"].append(
                {
                    "case_id": item["case_id"],
                    "case_title": item["title"],
                    "feature_name": item.get("feature_name"),
                    "owner_kind": item.get("owner_kind"),
                    "selection": {"case_id": item["case_id"], "index": assertion["index"]},
                    **assertion,
                }
            )
    return sorted(groups.values(), key=lambda group: (-group["count"], group["key"]))


def confirm_pending_assertions(
    db: Session,
    project: Project,
    selections: list[dict[str, Any]],
    *,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    """批量确认断言：只有带 openapi / code / doc 依据的断言才允许确认。"""
    by_case: dict[int, list[int]] = {}
    for selection in selections:
        try:
            case_id = int(selection.get("case_id"))
            index = int(selection.get("index"))
        except (TypeError, ValueError):
            continue
        by_case.setdefault(case_id, []).append(index)

    confirmed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for case_id, indexes in by_case.items():
        case = db.get(TestCase, case_id)
        if case is None or case.project_id != project.id:
            skipped.append({"case_id": case_id, "index": None, "reason": "用例不存在或不属于该项目"})
            continue
        spec = copy.deepcopy(case.spec or {})
        refs = assertion_refs(spec)
        changed = False
        for index in indexes:
            if not (0 <= index < len(refs)):
                skipped.append({"case_id": case_id, "index": index, "reason": "断言下标越界"})
                continue
            raw = refs[index]
            evidence = raw.get("evidence") or {}
            if not isinstance(evidence, dict):
                evidence = {}
            source = str(evidence.get("source_type") or "")
            if source not in {"openapi", "code", "doc"} or not evidence.get("ref"):
                skipped.append(
                    {"case_id": case_id, "index": index, "reason": "没有可引用的 openapi/code/doc 依据，需人工逐条处理或接受风险"}
                )
                continue
            if raw.get("status") == "confirmed":
                skipped.append({"case_id": case_id, "index": index, "reason": "断言已是 confirmed"})
                continue
            before = dict(raw)
            raw["status"] = "confirmed"
            raw["evidence"] = {
                **evidence,
                "note": note or evidence.get("note") or "",
                "confirmed_by": actor,
                "confirmed_at": now,
            }
            changed = True
            confirmed.append({"case_id": case_id, "index": index, "before": before, "after": dict(raw)})
        if changed:
            case.spec = spec
            case.steps = spec.get("steps") or case.steps
            db.add(
                HumanFeedback(
                    project_id=project.id,
                    target_type="test_case_assertion",
                    target_id=case_id,
                    action="confirm_assertions",
                    before={"confirmed": [item for item in confirmed if item["case_id"] == case_id]},
                    after={"actor": actor, "note": note},
                    note=note,
                    author=actor,
                )
            )
    db.commit()
    return {"confirmed": confirmed, "skipped": skipped, "confirmed_count": len(confirmed)}


def accept_pending_assertion_risk(
    db: Session,
    project: Project,
    case_ids: list[int],
    *,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    """批量接受风险：把用例缺口置为 accepted，并写入处置审计。"""
    updated: list[int] = []
    for case_id in dict.fromkeys(int(case_id) for case_id in case_ids):
        case = db.get(TestCase, case_id)
        if case is None or case.project_id != project.id:
            continue
        _upsert_gap_state(
            db,
            project,
            case,
            status="accepted",
            owner=None,
            note=note or None,
            actor=actor,
            source="assertion_risk",
            detail={"action": "accept_assertion_risk"},
        )
        updated.append(case_id)
    db.commit()
    return {"updated": len(updated), "case_ids": updated}


def _md_escape(text: Any) -> str:
    return str(text or "").replace("\n", " ").strip()


def render_gaps_markdown(
    project: Project,
    items: list[dict[str, Any]],
    *,
    owner_kind: str | None = None,
) -> str:
    if owner_kind:
        items = [item for item in items if item.get("owner_kind") == owner_kind]
    pending_count = sum(len(item["pending_assertions"]) for item in items)
    question_count = sum(item.get("question_count", 0) for item in items)
    lines = [
        f"# 契约缺口报告 · {project.name}",
        "",
        f"> 项目 #{project.id} · {len(items)} 条用例有缺口 · {pending_count} 条待确认断言 · {question_count} 个待确认问题",
        "",
        "本报告由 Shakedown 生成，描述被测系统在文档 / 契约上的缺口。",
        "Shakedown 只做测试与报告，不会修改被测系统的 OpenAPI、文档或代码；",
        "请由被测系统维护者确认后，决定是否补充 schema / 文档或调整实现。",
        "",
        "待确认断言不代表用例失败，只表示缺少可引用的契约 / 文档依据；"
        "请不要直接把它当作产品缺陷。",
        "",
        "待确认问题不是断言：它没有期望值，也不参与执行判定；"
        "请先回答「期望行为是什么」，再决定补充契约或调整实现。",
        "",
        "处理完成后，请把对应断言标为「已确认」，并由 Shakedown 重新执行验证。",
        "",
    ]
    if owner_kind == "sut-contract":
        lines.extend(
            [
                "> 本清单只包含归属为「被测系统契约」（sut-contract）的缺口；",
                "> 测试资产缺口（test-side）和环境缺口（environment）由 Shakedown 测试侧自行处理，不会出现在这里。",
                "",
            ]
        )

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item.get("feature_name") or "未映射功能点", []).append(item)

    for group, cases in groups.items():
        lines.extend([f"## {_md_escape(group)}", ""])
        for case in cases:
            lines.append(
                f"- [ ] #{case['case_id']} {_md_escape(case['title'])}"
                f"（{_md_escape(case['status'])} / {_md_escape(case['dimension'])}，"
                f"{case['step_count']} 步 / {case['assertion_count']} 条断言；"
                f"归属：{_md_escape(case.get('owner_kind') or 'test-side')}；"
                f"处置：{_md_escape(case['gap_status'])}"
                f"{'，负责人：' + _md_escape(case['gap_owner']) if case.get('gap_owner') else ''}）"
            )
            if case.get("gap_note"):
                lines.append(f"  - 处置备注：{_md_escape(case['gap_note'])}")
            if case["missing_input"]:
                lines.append(f"  - 待补全说明：{_md_escape(case['missing_input'])}")
            if case["no_steps"]:
                lines.append("  - 缺少可执行步骤：请先补全接口、参数和请求步骤。")
            for assertion in case["pending_assertions"]:
                endpoint = f" `{assertion['endpoint']}`" if assertion["endpoint"] else ""
                lines.append(
                    f"  - 待确认断言{endpoint}：`{assertion['kind']}` "
                    f"{_md_escape(assertion['description'])}"
                )
                expected = _md_escape(assertion["expected"])
                if expected:
                    lines.append(f"    - 期望：`{expected}`")
                evidence = assertion["evidence"]
                source = " / ".join(
                    part for part in (evidence.get("source_type"), evidence.get("ref"), evidence.get("quote")) if part
                )
                if source:
                    lines.append(f"    - 依据：{_md_escape(source)}")
            questions = case.get("contract_questions") or []
            if owner_kind:
                questions = [item for item in questions if _question_owner_kind(item) == owner_kind]
            for question in questions:
                lines.append(f"  - 待确认问题：{_md_escape(question.get('question'))}")
                missing = _md_escape(question.get("missing_contract"))
                if missing:
                    lines.append(f"    - 缺少契约：{missing}")
            lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
