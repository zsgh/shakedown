"""CI gate：把一次 run 的结果收敛成可阻断合并的 pass/fail。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import CaseResultStatus
from app.models import AutomationException, CaseResult, Finding, PerformanceRun, Project, Run

BLOCKING_SEVERITIES = {"critical", "high"}
BLOCKING_EXCEPTION_KINDS = {"environment_unavailable"}


def evaluate_gate(db: Session, project: Project, *, run_id: int | None = None) -> dict[str, Any]:
    if run_id is not None:
        run = db.get(Run, run_id)
        if run is None or run.project_id != project.id:
            raise ValueError(f"执行记录不存在或不属于当前项目：{run_id}")
    else:
        run = db.scalar(
            select(Run).where(Run.project_id == project.id).order_by(Run.id.desc()).limit(1)
        )
    if run is None:
        return {
            "passed": False,
            "status": "no_run",
            "blockers": [{"kind": "no_run", "message": "还没有执行记录"}],
            "warnings": [],
            "run_id": None,
        }

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if run.status != "completed":
        blockers.append({"kind": "run_not_completed", "message": f"run 状态为 {run.status}"})
    if run.error:
        blockers.append({"kind": "environment", "message": run.error})

    results = list(db.scalars(select(CaseResult).where(CaseResult.run_id == run.id)).all())
    failed = [
        row
        for row in results
        if row.status in {CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value}
    ]
    if failed:
        blockers.append(
            {
                "kind": "failed_cases",
                "message": f"{len(failed)} 条用例失败/错误",
                "case_ids": [row.case_id for row in failed][:200],
            }
        )

    result_ids = [row.id for row in results]
    if result_ids:
        findings = list(
            db.scalars(
                select(Finding).where(
                    Finding.project_id == project.id,
                    Finding.result_id.in_(result_ids),
                    Finding.status == "confirmed",
                    Finding.fix_status != "verified_fixed",
                )
            ).all()
        )
        serious = [item for item in findings if item.severity in BLOCKING_SEVERITIES]
        if serious:
            blockers.append(
                {
                    "kind": "confirmed_findings",
                    "message": f"{len(serious)} 条高严重度确认缺陷",
                    "finding_ids": [item.id for item in serious][:200],
                }
            )
        else:
            low = [item for item in findings if item.severity not in BLOCKING_SEVERITIES]
            if low:
                warnings.append(
                    {
                        "kind": "confirmed_findings",
                        "message": f"{len(low)} 条低严重度确认缺陷",
                        "finding_ids": [item.id for item in low][:200],
                    }
                )

    exceptions = list(
        db.scalars(
            select(AutomationException).where(
                AutomationException.project_id == project.id,
                AutomationException.status == "open",
            )
        ).all()
    )
    blocking_exceptions = [item for item in exceptions if item.kind in BLOCKING_EXCEPTION_KINDS]
    if blocking_exceptions:
        blockers.append(
            {
                "kind": "exceptions",
                "message": f"{len(blocking_exceptions)} 个未关闭环境例外",
                "exception_ids": [item.id for item in blocking_exceptions][:200],
            }
        )

    next_run = db.scalar(
        select(Run)
        .where(Run.project_id == project.id, Run.id > run.id)
        .order_by(Run.id)
        .limit(1)
    )
    slo_stmt = select(PerformanceRun).where(
        PerformanceRun.project_id == project.id,
        PerformanceRun.slo_passed.is_(False),
        PerformanceRun.created_at >= run.created_at,
    )
    if next_run is not None and next_run.created_at is not None:
        slo_stmt = slo_stmt.where(PerformanceRun.created_at < next_run.created_at)
    slo_failed = list(db.scalars(slo_stmt).all())
    if slo_failed:
        blockers.append(
            {
                "kind": "slo",
                "message": f"{len(slo_failed)} 次性能 SLO 未通过",
                "performance_ids": [item.id for item in slo_failed][:200],
            }
        )

    return {
        "passed": not blockers,
        "status": "pass" if not blockers else "fail",
        "run_id": run.id,
        "run_status": run.status,
        "slo_window": {
            "start": run.created_at.isoformat() if run.created_at else None,
            "end": next_run.created_at.isoformat() if next_run is not None and next_run.created_at else None,
        },
        "blockers": blockers,
        "warnings": warnings,
        "summary": {
            "cases": len(results),
            "failed": len(failed),
            "blockers": len(blockers),
            "warnings": len(warnings),
        },
    }
