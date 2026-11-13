"""存量归因回填：给已停用/待复核的用例补一份「为什么」的说明。

只写派生字段：
- ``case.verification["attribution"]``：用例详情直接展示；
- 最近一次失败结果的 ``attribution``：没被人工改过才写；
- 对应未关闭例外的 ``payload["attribution"]`` 与备注前缀：让例外队列能按类别筛选。

不改变用例状态，不生成新例外，不覆盖人工归因。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import failure_attribution
from app.models import AutomationException, CaseResult, Project, TestCase

_OPEN_STATUSES = {"open", "acked"}


def _latest_result(db: Session, case_id: int) -> CaseResult | None:
    return db.scalar(
        select(CaseResult)
        .where(CaseResult.case_id == case_id, CaseResult.rerun.is_(False))
        .order_by(CaseResult.id.desc())
        .limit(1)
    )


def backfill_project(
    db: Session,
    project: Project,
    *,
    statuses: tuple[str, ...] = ("disabled", "needs_review"),
    apply: bool = True,
) -> dict[str, Any]:
    cases = list(
        db.scalars(
            select(TestCase)
            .where(TestCase.project_id == project.id, TestCase.status.in_(statuses))
            .order_by(TestCase.id)
        ).all()
    )
    rows: list[dict[str, Any]] = []
    updated_cases = 0
    updated_results = 0
    updated_exceptions = 0
    for case in cases:
        result = _latest_result(db, case.id)
        verdict = failure_attribution.classify(case, result)
        rows.append(
            {
                "case_id": case.id,
                "title": case.title,
                "status": case.status,
                "category": verdict.get("category"),
                "category_label": failure_attribution.CATEGORY_LABELS.get(
                    str(verdict.get("category")), str(verdict.get("category"))
                ),
                "reason_code": verdict.get("reason_code"),
                "reason": verdict.get("reason"),
                "confidence": verdict.get("confidence"),
                "result_id": result.id if result is not None else None,
            }
        )
        if not apply:
            continue
        case.verification = {**(case.verification or {}), "attribution": verdict}
        updated_cases += 1
        if result is not None and not (result.attribution or {}).get("manual"):
            result.attribution = verdict
            updated_results += 1
        exceptions = db.scalars(
            select(AutomationException).where(
                AutomationException.project_id == project.id,
                AutomationException.target_type == "case",
                AutomationException.target_id == case.id,
                AutomationException.status.in_(_OPEN_STATUSES),
            )
        ).all()
        for exception in exceptions:
            payload = dict(exception.payload or {})
            payload["attribution"] = {
                key: verdict.get(key) for key in ("category", "reason_code", "reason_label", "reason", "confidence")
            }
            exception.payload = payload
            note = str(exception.note or "")
            prefix = f"【{verdict.get('reason_label') or verdict.get('reason_code')}】"
            if not note.startswith("【"):
                exception.note = (prefix + note)[:2000]
            updated_exceptions += 1
    db.flush()
    summary = failure_attribution.summarize([row for row in rows])
    return {
        "project_id": project.id,
        "scanned": len(rows),
        "updated_cases": updated_cases,
        "updated_results": updated_results,
        "updated_exceptions": updated_exceptions,
        "summary": summary,
        "cases": rows,
    }


__all__ = ["backfill_project"]
