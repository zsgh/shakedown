"""用例重评估：按项目 SLA 重算性能断言、按最新 spec 快照复核受影响用例。

两个动作都坚持同一原则：先预览、显式执行、只改用例资产，不动被测系统。
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import assertion_refs, collect_assertions
from app.gaps import _is_sla_missing, assertion_is_pending
from app.ingest.consistency import normalize_path
from app.models import Endpoint, Finding, HumanFeedback, Project, SpecSnapshot, TestCase

_CONFIRMABLE_SOURCES = {"openapi", "code", "doc"}


def project_sla_ms(project: Project) -> int | None:
    settings = project.settings_json or {}
    raw = settings.get("performance_sla_ms")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _endpoint_key(method: str, path: str) -> str:
    return f"{str(method or '').upper()} {normalize_path(str(path or ''))}"


def _load_endpoints(db: Session, project: Project) -> dict[str, Endpoint]:
    rows = db.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all()
    return {_endpoint_key(row.method, row.path): row for row in rows}


def _request_steps(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in spec.get("steps") or []
        if isinstance(step, dict) and str(step.get("method") or "").strip()
    ]


def _case_endpoint(
    spec: dict[str, Any],
    endpoints: dict[str, Endpoint],
) -> Endpoint | None:
    steps = _request_steps(spec)
    if steps:
        return endpoints.get(_endpoint_key(steps[0].get("method", ""), steps[0].get("path", "")))
    return None


def _fallback_endpoint(endpoints: dict[str, Endpoint]) -> Endpoint | None:
    candidates = [row for row in endpoints.values() if "{" not in row.path]
    candidates.sort(key=lambda row: (0 if row.method.upper() == "GET" else 1, len(row.path)))
    return candidates[0] if candidates else None


def _sample_path(path: str) -> str:
    return re.sub(r"\{[^/{}]+\}", "1", path)


def _timing_assertions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [raw for raw in assertion_refs(spec) if raw.get("kind") == "timing"]


def _timing_signature(assertion: dict[str, Any]) -> dict[str, Any]:
    return {
        "operator": assertion.get("operator"),
        "expected": assertion.get("expected"),
        "target": assertion.get("target"),
        "description": assertion.get("description"),
    }


def preview_sla_recompute(db: Session, project: Project) -> dict[str, Any]:
    """列出会受 SLA 重算影响、以及仍缺少 SLA 的性能用例。"""
    sla = project_sla_ms(project)
    cases = db.scalars(
        select(TestCase).where(
            TestCase.project_id == project.id,
            TestCase.status != "disabled",
            TestCase.dimension == "performance",
        )
    ).all()
    affected: list[dict[str, Any]] = []
    for case in cases:
        spec = case.spec or {}
        timing = _timing_assertions(spec)
        reasons: list[str] = []
        if _is_sla_missing(case.missing_input):
            reasons.append("missing_input")
        if sla is not None and any(str(raw.get("expected")) != str(sla) for raw in timing):
            reasons.append("stale_assertion")
        if not reasons:
            continue
        affected.append(
            {
                "case_id": case.id,
                "title": case.title,
                "status": case.status,
                "missing_input": case.missing_input or "",
                "reasons": reasons,
                "old_assertions": [_timing_signature(raw) for raw in timing],
                "has_steps": bool(case.steps),
            }
        )
    return {
        "project_sla_ms": sla,
        "count": len(affected),
        "cases": affected,
        "message": "" if sla else "项目未配置 performance_sla_ms，保持缺失输入，不会硬编阈值。",
    }


def apply_sla_recompute(
    db: Session,
    project: Project,
    *,
    case_ids: list[int] | None = None,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    """按项目 SLA 重算性能用例的 timing 断言。"""
    sla = project_sla_ms(project)
    if sla is None:
        raise ValueError("项目未配置 performance_sla_ms，无法重算 timing 断言")
    preview = preview_sla_recompute(db, project)
    wanted = set(case_ids) if case_ids else None
    targets = [row for row in preview["cases"] if wanted is None or row["case_id"] in wanted]
    endpoints = _load_endpoints(db, project)
    now = datetime.now(timezone.utc).isoformat()

    applied: list[int] = []
    skipped: list[dict[str, Any]] = []
    for row in targets:
        case = db.get(TestCase, row["case_id"])
        if case is None or case.project_id != project.id:
            continue
        spec = copy.deepcopy(case.spec or {})
        steps = spec.setdefault("steps", [])
        request_steps = _request_steps(spec)
        endpoint = _case_endpoint(spec, endpoints) or _fallback_endpoint(endpoints)
        if not request_steps:
            if endpoint is None:
                skipped.append({"case_id": case.id, "reason": "没有可推断的接口，无法构建性能步骤"})
                continue
            request_steps = [
                {
                    "action": "request",
                    "method": endpoint.method.upper(),
                    "path": _sample_path(endpoint.path),
                    "assert": [],
                }
            ]
            steps.extend(request_steps)
            spec["steps"] = steps
        if not request_steps:
            skipped.append({"case_id": case.id, "reason": "用例没有可执行请求步骤"})
            continue

        before_assertions = [_timing_signature(raw) for raw in _timing_assertions(spec)]
        evidence = {
            "source_type": "project_config",
            "ref": f"project:{project.id}:settings.performance_sla_ms",
            "quote": f"performance_sla_ms={sla}",
            "note": note or "按项目 SLA 重算 timing 断言",
        }
        timing_refs = _timing_assertions(spec)
        if not timing_refs:
            timing_refs = [
                {
                    "kind": "timing",
                    "target": "response_time_ms",
                    "operator": "lt",
                }
            ]
            first = request_steps[0]
            first.setdefault("assert", []).append(timing_refs[0])
        for raw in timing_refs:
            raw["expected"] = sla
            raw["operator"] = "lt"
            raw["status"] = "confirmed"
            raw["evidence"] = evidence

        case.spec = spec
        case.steps = spec.get("steps") or case.steps
        if _is_sla_missing(case.missing_input):
            case.missing_input = None
        db.add(
            HumanFeedback(
                project_id=project.id,
                target_type="test_case",
                target_id=case.id,
                action="recompute_sla",
                before={"sla_ms": None, "assertions": before_assertions, "missing_input": row["missing_input"]},
                after={
                    "sla_ms": sla,
                    "assertions": [_timing_signature(raw) for raw in timing_refs],
                    "actor": actor,
                    "at": now,
                },
                note=note,
                author=actor,
            )
        )
        applied.append(case.id)

    db.commit()
    return {
        "project_sla_ms": sla,
        "applied": len(applied),
        "case_ids": applied,
        "skipped": skipped,
    }


# ── spec 变更后的用例重评估 ──────────────────────────────────
def latest_snapshot(db: Session, project: Project, snapshot_id: int | None = None) -> SpecSnapshot | None:
    if snapshot_id is not None:
        snapshot = db.get(SpecSnapshot, snapshot_id)
        if snapshot is None or snapshot.project_id != project.id:
            return None
        return snapshot
    return db.scalar(
        select(SpecSnapshot)
        .where(SpecSnapshot.project_id == project.id)
        .order_by(SpecSnapshot.created_at.desc(), SpecSnapshot.id.desc())
        .limit(1)
    )


def _diff_keys(snapshot: SpecSnapshot | None) -> tuple[set[str], set[str]]:
    if snapshot is None:
        return set(), set()
    diff = snapshot.diff or {}
    changed = {_endpoint_key(item.get("method", ""), item.get("path", "")) for item in diff.get("changed", [])}
    removed = {_endpoint_key(item.get("method", ""), item.get("path", "")) for item in diff.get("removed", [])}
    return changed, removed


def _referenced_keys(case: TestCase) -> set[str]:
    keys: set[str] = set()
    for step in (case.spec or {}).get("steps") or []:
        if isinstance(step, dict) and step.get("method"):
            keys.add(_endpoint_key(step.get("method", ""), step.get("path", "")))
    return keys


def preview_reevaluate(
    db: Session,
    project: Project,
    *,
    snapshot_id: int | None = None,
    case_ids: list[int] | None = None,
) -> dict[str, Any]:
    snapshot = latest_snapshot(db, project, snapshot_id)
    changed, removed = _diff_keys(snapshot)
    touched = changed | removed
    cases = db.scalars(
        select(TestCase).where(TestCase.project_id == project.id, TestCase.status != "disabled")
    ).all()
    wanted = set(case_ids) if case_ids else None
    rows: list[dict[str, Any]] = []
    for case in cases:
        if wanted is not None and case.id not in wanted:
            continue
        hits = _referenced_keys(case) & touched
        if not hits:
            continue
        pending = [
            {
                "index": index,
                "kind": raw.get("kind") or "",
                "description": raw.get("description") or "",
                "evidence": raw.get("evidence") or {},
            }
            for index, raw in enumerate(collect_assertions(case.spec or {}))
            if assertion_is_pending(raw)
        ]
        rows.append(
            {
                "case_id": case.id,
                "title": case.title,
                "status": case.status,
                "endpoints": sorted(hits),
                "removed_endpoints": sorted(hits & removed),
                "pending_assertions": pending,
            }
        )
    return {
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_version": snapshot.version if snapshot else "",
        "diff": snapshot.diff if snapshot else {},
        "count": len(rows),
        "cases": rows,
        "message": "" if snapshot else "项目还没有 spec 快照，无法按 diff 重评估。",
    }


def false_positive_recheck_candidates(
    db: Session,
    project: Project,
    *,
    snapshot_id: int | None = None,
) -> dict[str, Any]:
    """按最新 spec 快照变化，筛出可能需要重新打开的历史误报。"""
    snapshot = latest_snapshot(db, project, snapshot_id)
    changed, removed = _diff_keys(snapshot)
    touched = changed | removed
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.status == "false_positive",
        )
    ).all()
    rows: list[dict[str, Any]] = []
    for finding in findings:
        case = db.get(TestCase, finding.case_id) if finding.case_id else None
        hits = (_referenced_keys(case) & touched) if case is not None else set()
        if not hits:
            continue
        resolution = finding.resolution or {}
        rows.append(
            {
                "finding_id": finding.id,
                "title": finding.title,
                "severity": finding.severity,
                "case_id": finding.case_id,
                "endpoints": sorted(hits),
                "removed_endpoints": sorted(hits & removed),
                "assignee": finding.assignee,
                "resolution_note": str(resolution.get("note") or ""),
                "resolution_missing": not str(resolution.get("note") or "").strip(),
                "updated_at": finding.updated_at.isoformat() if finding.updated_at else None,
            }
        )
    return {
        "snapshot_id": snapshot.id if snapshot else None,
        "snapshot_version": snapshot.version if snapshot else "",
        "count": len(rows),
        "items": rows,
    }


def _response_schema(endpoint: Endpoint) -> dict[str, Any]:
    responses = endpoint.responses or {}
    for status in ("200", "201", "202", "default"):
        entry = responses.get(status)
        if isinstance(entry, dict):
            schema = entry.get("schema")
            if isinstance(schema, dict):
                return schema
    return {}


def _schema_properties(schema: dict[str, Any]) -> set[str]:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return {str(key) for key in properties}
    items = schema.get("items")
    if isinstance(items, dict) and isinstance(items.get("properties"), dict):
        return {str(key) for key in items["properties"]}
    return set()


def _try_confirm(assertion: dict[str, Any], endpoint: Endpoint) -> dict[str, Any] | None:
    """只在能由当前 endpoint schema 明确支撑时返回新的 evidence。"""
    kind = str(assertion.get("kind") or "")
    expected = assertion.get("expected")
    if kind == "status_code":
        status = str(expected if expected is not None else "").strip()
        if status and status in (endpoint.responses or {}):
            return {
                "source_type": "openapi",
                "ref": f"openapi:{endpoint.method.upper()} {endpoint.path}#/responses/{status}",
                "quote": f"OpenAPI 定义了 {status} 响应",
                "note": "spec 重评估自动确认",
            }
        return None
    if kind == "json_path":
        target = str(assertion.get("target") or "")
        field = target.strip("$.[]").split(".")[0].split("[")[0]
        if field and field in _schema_properties(_response_schema(endpoint)):
            return {
                "source_type": "openapi",
                "ref": f"openapi:{endpoint.method.upper()} {endpoint.path}#/components/schemas",
                "quote": f"响应 schema 包含字段 {field}",
                "note": "spec 重评估自动确认",
            }
        return None
    return None


def apply_reevaluate(
    db: Session,
    project: Project,
    *,
    snapshot_id: int | None = None,
    case_ids: list[int] | None = None,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    preview = preview_reevaluate(db, project, snapshot_id=snapshot_id, case_ids=case_ids)
    endpoints = _load_endpoints(db, project)
    now = datetime.now(timezone.utc).isoformat()
    processed: list[int] = []
    skipped: list[dict[str, Any]] = []
    confirmed_total = 0
    unlocked: list[int] = []

    for row in preview["cases"]:
        case = db.get(TestCase, row["case_id"])
        if case is None:
            continue
        spec = copy.deepcopy(case.spec or {})
        case_endpoints = [
            endpoints.get(_endpoint_key(step.get("method", ""), step.get("path", "")))
            for step in _request_steps(spec)
        ]
        case_endpoints = [endpoint for endpoint in case_endpoints if endpoint is not None]
        if not case_endpoints:
            skipped.append({"case_id": case.id, "reason": "受影响接口已从 spec 移除或无法匹配，保留 needs_review"})
            continue

        confirmed: list[dict[str, Any]] = []
        for raw in assertion_refs(spec):
            if not assertion_is_pending(raw):
                continue
            evidence = raw.get("evidence") or {}
            if str(evidence.get("source_type") or "") in _CONFIRMABLE_SOURCES:
                continue
            for endpoint in case_endpoints:
                new_evidence = _try_confirm(raw, endpoint)
                if new_evidence is None:
                    continue
                before = dict(raw)
                raw["status"] = "confirmed"
                raw["evidence"] = new_evidence
                confirmed.append({"before": before, "after": dict(raw)})
                break

        confirmed_total += len(confirmed)
        if confirmed:
            case.spec = spec
            case.steps = spec.get("steps") or case.steps
            db.add(
                HumanFeedback(
                    project_id=project.id,
                    target_type="test_case",
                    target_id=case.id,
                    action="reevaluate_spec",
                    before={"snapshot_id": preview["snapshot_id"], "assertions": [item["before"] for item in confirmed]},
                    after={"assertions": [item["after"] for item in confirmed], "actor": actor, "at": now},
                    note=note,
                    author=actor,
                )
            )
        remaining = [
            raw
            for raw in collect_assertions(spec)
            if assertion_is_pending(raw)
        ]
        impact = spec.get("change_impact")
        has_change_impact = isinstance(impact, dict) and bool(impact.get("reasons"))
        if (
            case.status == "needs_review"
            and not remaining
            and not case.missing_input
            and not has_change_impact
        ):
            case.status = "enabled"
            unlocked.append(case.id)
        processed.append(case.id)

    db.commit()
    return {
        "snapshot_id": preview["snapshot_id"],
        "processed": len(processed),
        "confirmed_assertions": confirmed_total,
        "unlocked_cases": unlocked,
        "case_ids": processed,
        "skipped": skipped,
    }
