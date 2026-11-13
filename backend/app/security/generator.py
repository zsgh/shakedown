"""攻击用例包生成器：扫描 → 幂等生成 → 可选验证。"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.case_validation import create_validation_run
from app.enums import CaseStatus, FindingSource, GenerationSource
from app.executor.case_schema import collect_assertions, validate_case_spec
from app.logging import get_logger
from app.models import Finding, Project, TestCase
from app.security.context import AttackContext, build_context
from app.security.registry import (
    AttackPattern,
    CaseDraft,
    FindingDraft,
    all_patterns,
    get_pattern,
    load_builtin_patterns,
)

logger = get_logger("shakedown.security.generator")


def _existing_cases(db: Session, project: Project) -> tuple[dict[str, TestCase], dict[str, TestCase]]:
    by_key: dict[str, TestCase] = {}
    by_title: dict[str, TestCase] = {}
    for case in db.scalars(select(TestCase).where(TestCase.project_id == project.id)).all():
        by_title[case.title] = case
        attack = (case.spec or {}).get("attack") if isinstance(case.spec, dict) else {}
        key = str((attack or {}).get("key") or "") if isinstance(attack, dict) else ""
        if key:
            by_key[key] = case
    return by_key, by_title


def _existing_findings(db: Session, project: Project) -> dict[str, Finding]:
    rows = db.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.cluster_key.like("attack-pack:%"),
        )
    ).all()
    return {str(row.cluster_key): row for row in rows if row.cluster_key}


def _pattern_rows(
    ctx: AttackContext,
    patterns: Iterable[AttackPattern],
    *,
    include_findings: bool,
) -> tuple[list[dict[str, Any]], list[tuple[AttackPattern, CaseDraft]], list[tuple[AttackPattern, FindingDraft]]]:
    rows: list[dict[str, Any]] = []
    drafts: list[tuple[AttackPattern, CaseDraft]] = []
    findings: list[tuple[AttackPattern, FindingDraft]] = []
    for pattern in patterns:
        row: dict[str, Any] = {
            "id": pattern.id,
            "title": pattern.title,
            "category": pattern.category,
            "severity": pattern.severity,
            "description": pattern.description,
            "reference": pattern.reference,
            "version": pattern.version,
            "applicable": False,
            "reason": "",
            "cases": 0,
            "findings": 0,
        }
        try:
            applicable, reason = pattern.applicable(ctx)
        except Exception as exc:  # noqa: BLE001 - 单个模式失败不影响整包
            applicable, reason = False, f"模式检查失败：{type(exc).__name__}: {exc}"
        row["applicable"] = applicable
        row["reason"] = reason
        if applicable:
            try:
                built = pattern.build(ctx)
            except Exception as exc:  # noqa: BLE001
                row["reason"] = f"生成失败：{type(exc).__name__}: {exc}"
                built = []
            for draft in built:
                drafts.append((pattern, draft))
            row["cases"] = len(built)
            if include_findings:
                try:
                    found = pattern.findings(ctx)
                except Exception as exc:  # noqa: BLE001
                    row["reason"] = f"扫描失败：{type(exc).__name__}: {exc}"
                    found = []
                for finding in found:
                    findings.append((pattern, finding))
                row["findings"] = len(found)
        rows.append(row)
    return rows, drafts, findings


def scan_project(
    db: Session,
    project: Project,
    *,
    include_findings: bool = False,
    pattern_ids: list[str] | None = None,
) -> dict[str, Any]:
    load_builtin_patterns()
    ctx = build_context(db, project)
    patterns = [get_pattern(item) for item in pattern_ids] if pattern_ids else all_patterns()
    selected = [item for item in patterns if item is not None]
    rows, drafts, findings = _pattern_rows(ctx, selected, include_findings=include_findings)
    by_key, by_title = _existing_cases(db, project)
    existing_findings = _existing_findings(db, project)

    draft_rows: list[dict[str, Any]] = []
    for pattern, draft in drafts:
        existing = by_key.get(draft.attack_key) or by_title.get(draft.title)
        draft_rows.append(
            {
                "pattern_id": pattern.id,
                "title": draft.title,
                "attack_key": draft.attack_key,
                "policy_ref": draft.policy_ref,
                "tags": draft.tags,
                "existing_case_id": existing.id if existing is not None else None,
            }
        )
    finding_rows: list[dict[str, Any]] = []
    for pattern, finding in findings:
        existing = existing_findings.get(f"attack-pack:{finding.key}")
        finding_rows.append(
            {
                "pattern_id": pattern.id,
                "key": finding.key,
                "title": finding.title,
                "severity": finding.severity,
                "description": finding.description,
                "existing_finding_id": existing.id if existing is not None else None,
            }
        )
    report = ctx.scratch.get("dependency_report")
    return {
        "project_id": project.id,
        "patterns": rows,
        "cases": draft_rows,
        "findings": finding_rows,
        "summary": {
            "patterns": len(rows),
            "applicable": sum(1 for row in rows if row["applicable"]),
            "cases": len(draft_rows),
            "new_cases": sum(1 for row in draft_rows if row["existing_case_id"] is None),
            "findings": len(finding_rows),
            "new_findings": sum(1 for row in finding_rows if row["existing_finding_id"] is None),
        },
        "dependencies": {
            "manifests": getattr(report, "manifests", []),
            "count": len(getattr(report, "dependencies", []) or []),
            "toolchains": getattr(report, "toolchains", []),
            "errors": getattr(report, "errors", []),
        }
        if report is not None
        else {},
    }


def _create_case(db: Session, project: Project, pattern: AttackPattern, draft: CaseDraft) -> TestCase:
    spec = dict(draft.spec)
    spec.setdefault("title", draft.title)
    spec.setdefault("dimension", draft.dimension)
    spec.setdefault("type", "api")
    spec.setdefault("steps", [])
    spec.setdefault("assertions", [])
    spec.setdefault("evidence", [])
    spec.setdefault("attack", {"key": draft.attack_key, "policy": draft.policy_ref})
    tags = list(dict.fromkeys([*(spec.get("tags") or []), *draft.tags, f"attack:{pattern.id}"]))
    spec["tags"] = tags
    case = TestCase(
        project_id=project.id,
        title=draft.title,
        dimension=draft.dimension,
        case_type="api",
        preconditions={"items": spec.get("preconditions") or []},
        steps=spec.get("steps") or [],
        expected={"assertions": collect_assertions(spec)},
        evidence=spec.get("evidence") or [],
        spec=spec,
        generation=GenerationSource.AGENT.value,
        status=CaseStatus.PENDING.value,
        priority=draft.priority,
        tags=tags,
        verification_mode="api",
    )
    db.add(case)
    db.flush()
    return case


def _upsert_finding(db: Session, project: Project, draft: FindingDraft) -> Finding:
    cluster_key = f"attack-pack:{draft.key}"
    finding = db.scalar(
        select(Finding).where(Finding.project_id == project.id, Finding.cluster_key == cluster_key)
    )
    if finding is None:
        finding = Finding(
            project_id=project.id,
            source=FindingSource.WHITEBOX.value,
            title=draft.title[:400],
            description=draft.description,
            severity=draft.severity,
            evidence=draft.evidence,
            risk_type=draft.risk_type,
            module="依赖 / 工具链",
            status="confirmed",
            confirmed_by="auto:attack-pack",
            cluster_key=cluster_key,
        )
        db.add(finding)
        db.flush()
        return finding
    finding.title = draft.title[:400]
    finding.description = draft.description
    finding.severity = draft.severity
    finding.evidence = draft.evidence
    finding.risk_type = draft.risk_type
    return finding


def generate_cases(
    db: Session,
    project: Project,
    *,
    pattern_ids: list[str] | None = None,
    dry_run: bool = False,
    validate: bool = False,
) -> dict[str, Any]:
    report = scan_project(db, project, include_findings=True, pattern_ids=pattern_ids)
    if dry_run:
        return {**report, "created_case_ids": [], "created_finding_ids": [], "validation_run_id": None}

    created_cases: list[TestCase] = []
    seen_titles = {row["title"] for row in report["cases"] if row["existing_case_id"] is not None}
    for row in report["cases"]:
        if row["existing_case_id"] is not None or row["title"] in seen_titles:
            continue
        pattern = get_pattern(row["pattern_id"])
        if pattern is None:
            continue
        draft = next(
            (
                item
                for item in pattern.build(build_context(db, project))
                if item.attack_key == row["attack_key"]
            ),
            None,
        )
        if draft is None:
            continue
        errors = validate_case_spec(draft.spec)
        if errors:
            logger.warning("攻击用例 schema 不合法：%s %s", draft.title, errors[:3])
            continue
        created_cases.append(_create_case(db, project, pattern, draft))
        seen_titles.add(draft.title)

    created_findings: list[Finding] = []
    for row in report["findings"]:
        if row["existing_finding_id"] is not None:
            continue
        pattern = get_pattern(row["pattern_id"])
        if pattern is None:
            continue
        ctx = build_context(db, project)
        try:
            drafts = pattern.findings(ctx)
        except Exception:  # noqa: BLE001
            drafts = []
        draft = next((item for item in drafts if item.key == row["key"]), None)
        if draft is None:
            continue
        created_findings.append(_upsert_finding(db, project, draft))

    validation_run_id = None
    if validate and created_cases:
        run, _created = create_validation_run(db, project, case_ids=[case.id for case in created_cases])
        validation_run_id = run.id if run is not None else None

    db.flush()
    return {
        **report,
        "created_case_ids": [case.id for case in created_cases],
        "created_finding_ids": [finding.id for finding in created_findings],
        "validation_run_id": validation_run_id,
    }


__all__ = ["generate_cases", "scan_project"]
