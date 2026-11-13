"""存量断言治理：把没有依据的 pending 断言迁成 policy 检查或待确认问题。

生成链路已经不再产出无依据断言；这个模块负责存量用例的一次性迁移，
让「猜测」不再以断言形式挂在用例里。
"""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import (
    assertion_to_contract_question,
    collect_assertions,
    collect_contract_questions,
    evidence_is_backed,
)
from app.models import HumanFeedback, Project, TestCase
from app.policies import match_policy, policy_assertion

MIGRATION_ACTIONS = {"policy", "question"}


def _case_stmt(project_id: int, case_ids: list[int] | None):
    stmt = select(TestCase).where(TestCase.project_id == project_id, TestCase.status != "disabled")
    if case_ids:
        stmt = stmt.where(TestCase.id.in_(case_ids))
    return stmt.order_by(TestCase.id)


def _needs_migration(assertion: dict[str, Any]) -> bool:
    """只治理「还没被确认的猜测」：已确认且写了依据的断言保持原样。"""
    if evidence_is_backed(assertion):
        return False
    evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
    source = str(evidence.get("source_type") or "")
    return assertion.get("status") != "confirmed" or source in ("", "none")


def _classify(assertion: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        str(assertion.get(key) or "") for key in ("description", "target", "expected", "kind")
    )
    policy = match_policy(text)
    if policy is not None:
        return {"action": "policy", "policy_id": policy.id, "policy_title": policy.title}
    question = assertion_to_contract_question(assertion, source="migration")
    return {"action": "question", "question": question["question"], "owner_kind": question["owner_kind"]}


def preview_assertion_migration(
    db: Session, project: Project, *, case_ids: list[int] | None = None
) -> dict[str, Any]:
    """列出会被迁移的断言及去向：policy 检查 / 待确认问题。"""
    cases = list(db.scalars(_case_stmt(project.id, case_ids)).all())
    rows: list[dict[str, Any]] = []
    policy_count = 0
    question_count = 0
    for case in cases:
        items: list[dict[str, Any]] = []
        for assertion in collect_assertions(case.spec or {}):
            if not _needs_migration(assertion):
                continue
            decision = _classify(assertion)
            if decision["action"] == "policy":
                policy_count += 1
            else:
                question_count += 1
            items.append(
                {
                    "kind": assertion.get("kind") or "",
                    "description": assertion.get("description") or "",
                    "target": assertion.get("target") or "",
                    "expected": assertion.get("expected"),
                    "step_index": assertion.get("step_index"),
                    **decision,
                }
            )
        if items:
            rows.append(
                {
                    "case_id": case.id,
                    "title": case.title,
                    "dimension": case.dimension,
                    "assertions": items,
                }
            )
    return {
        "affected_cases": len(rows),
        "assertions": policy_count + question_count,
        "to_policy": policy_count,
        "to_question": question_count,
        "cases": rows,
    }


def migrate_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """就地迁移一份 spec，返回 (新 spec, 转成 policy 的断言, 转成问题的断言)。"""
    spec = copy.deepcopy(spec or {})
    questions = collect_contract_questions(spec)
    seen = {item["question"] for item in questions}
    converted: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []

    def _handle(raw: dict[str, Any], step_index: int | None) -> dict[str, Any] | None:
        if not _needs_migration(raw):
            return raw
        decision = _classify(raw)
        if decision["action"] == "policy":
            new_assertion = policy_assertion(
                decision["policy_id"],
                step_index=step_index if step_index is not None else raw.get("step_index"),
            )
            converted.append(
                {
                    "kind": raw.get("kind") or "",
                    "description": raw.get("description") or "",
                    "policy_id": decision["policy_id"],
                }
            )
            return new_assertion
        question = assertion_to_contract_question(raw, step_index=step_index, source="migration")
        if question["question"] not in seen:
            questions.append(question)
            seen.add(question["question"])
        moved.append(
            {
                "kind": raw.get("kind") or "",
                "description": raw.get("description") or "",
                "expected": raw.get("expected"),
                "question": question["question"],
            }
        )
        return None

    for index, step in enumerate(spec.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step["assert"] = [
            migrated
            for raw in step.get("assert") or []
            if isinstance(raw, dict) and (migrated := _handle(raw, index)) is not None
        ]
    spec["assertions"] = [
        migrated
        for raw in spec.get("assertions") or []
        if isinstance(raw, dict) and (migrated := _handle(raw, None)) is not None
    ]
    spec["contract_questions"] = questions
    return spec, converted, moved


def apply_assertion_migration(
    db: Session,
    project: Project,
    *,
    case_ids: list[int] | None = None,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    """执行迁移；每条用例写一条人工审计，不改被测系统。"""
    cases = list(db.scalars(_case_stmt(project.id, case_ids)).all())
    migrated_cases: list[dict[str, Any]] = []
    policy_count = 0
    question_count = 0
    for case in cases:
        spec, converted, moved = migrate_spec(case.spec or {})
        if not converted and not moved:
            continue
        before = collect_assertions(case.spec or {})
        case.spec = spec
        case.steps = spec.get("steps") or []
        case.expected = {"assertions": collect_assertions(spec)}
        db.add(
            HumanFeedback(
                project_id=project.id,
                target_type="case",
                target_id=case.id,
                action="assertion_migration",
                before={"assertions": before},
                after={
                    "policy_assertions": converted,
                    "contract_questions": moved,
                },
                note=note or "无依据断言的存量迁移",
                author=actor,
            )
        )
        policy_count += len(converted)
        question_count += len(moved)
        migrated_cases.append(
            {
                "case_id": case.id,
                "title": case.title,
                "policy_assertions": len(converted),
                "contract_questions": len(moved),
            }
        )
    db.commit()
    return {
        "applied": len(migrated_cases),
        "to_policy": policy_count,
        "to_question": question_count,
        "cases": migrated_cases,
    }
