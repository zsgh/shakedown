"""无人值守对外触发与运行健康摘要。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.case_state import (
    executed_case_ids,
    latest_source_parsed_at,
    states_for_case,
)
from app.environment_check import check_target
from app.exceptions import list_exceptions, record_exception
from app.models import Project, ProjectSchedule, Run, Task, TestCase
from app.run_service import create_run_for_project
from app.sla import sla_breaches


def check_run_quota(db: Session, project: Project) -> dict[str, Any]:
    automation = (project.settings_json or {}).get("automation")
    automation = automation if isinstance(automation, dict) else {}
    now = datetime.now(timezone.utc)
    max_per_day = automation.get("max_runs_per_day")
    if max_per_day is not None:
        count = db.scalar(
            select(func.count()).where(
                Run.project_id == project.id,
                Run.created_at >= now - timedelta(days=1),
            )
        ) or 0
        if count >= int(max_per_day):
            return {"ok": False, "reason": f"24 小时运行次数已达上限 {max_per_day}"}
    max_concurrent = automation.get("max_concurrent_runs")
    if max_concurrent is not None:
        count = db.scalar(
            select(func.count()).where(
                Run.project_id == project.id,
                Run.status.in_(["queued", "preparing", "running", "analyzing"]),
            )
        ) or 0
        if count >= int(max_concurrent):
            return {"ok": False, "reason": f"当前并发运行数已达上限 {max_concurrent}"}
    return {"ok": True, "reason": ""}


def trigger_run(
    db: Session,
    project: Project,
    *,
    policy: dict[str, Any] | None = None,
    trigger: str = "deploy",
    note: str = "",
) -> dict[str, Any]:
    quota = check_run_quota(db, project)
    if not quota.get("ok"):
        return {"run_id": None, "status": "quota_exceeded", "reason": quota["reason"], "created": False}
    readiness = check_target(project)
    if not readiness.get("ok"):
        reason = str(readiness.get("reason") or "环境未就绪")
        run = Run(
            project_id=project.id,
            trigger=trigger,
            status="failed",
            case_ids=[],
            env_snapshot={"base_url": project.base_url, "readiness": readiness},
            stats={"total": 0, "environment": readiness, "note": note},
            error=f"环境未就绪：{reason}",
        )
        db.add(run)
        db.flush()
        record_exception(
            db,
            project,
            kind="environment_unavailable",
            target_type="run",
            target_id=run.id,
            severity="high",
            note=f"环境未就绪：{reason}",
            payload=readiness,
        )
        try:
            from app.push import queue_event

            queue_event(db, project, "environment_unavailable", readiness)
        except Exception:  # noqa: BLE001
            pass
        return {"run_id": run.id, "status": run.status, "readiness": readiness, "created": False}

    run = create_run_for_project(
        db,
        project,
        trigger=trigger,
        policy=policy or {},
        note=note,
        skip_empty=True,
    )
    if run is None:
        return {"run_id": None, "status": "skipped", "readiness": readiness, "created": False}
    return {"run_id": run.id, "status": run.status, "readiness": readiness, "created": True}


def automation_health(db: Session, project: Project) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    last_run = db.scalar(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(1)
    )
    queued = db.scalar(
        select(func.count()).where(
            Task.project_id == project.id,
            Task.status.in_(["queued", "running"]),
        )
    ) or 0
    failed_recent = db.scalar(
        select(func.count()).where(
            Task.project_id == project.id,
            Task.status == "failed",
            Task.updated_at >= now - timedelta(hours=24),
        )
    ) or 0
    cases = list(db.scalars(select(TestCase).where(TestCase.project_id == project.id)).all())
    executed = executed_case_ids(db, project.id, [case.id for case in cases])
    latest_parsed_at = latest_source_parsed_at(db, project.id)
    state_counts: dict[str, int] = {}
    for case in cases:
        for state in states_for_case(case, executed=executed, latest_parsed_at=latest_parsed_at):
            state_counts[state] = state_counts.get(state, 0) + 1
    schedules = list(
        db.scalars(
            select(ProjectSchedule).where(ProjectSchedule.project_id == project.id)
        ).all()
    )
    open_exceptions = len(list_exceptions(db, project, status="open", limit=500))
    breaches = sla_breaches(db, project)
    return {
        "project_id": project.id,
        "last_run": (
            {
                "id": last_run.id,
                "status": last_run.status,
                "trigger": last_run.trigger,
                "created_at": last_run.created_at.isoformat() if last_run.created_at else None,
                "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
                "stats": last_run.stats or {},
                "error": last_run.error,
            }
            if last_run
            else None
        ),
        "queue": {"active": queued, "failed_24h": failed_recent},
        "schedules": {
            "total": len(schedules),
            "enabled": sum(1 for item in schedules if item.enabled),
            "next_run_at": min(
                (item.next_run_at.isoformat() for item in schedules if item.enabled and item.next_run_at),
                default=None,
            ),
        },
        "execution_states": state_counts,
        "exceptions": {"open": open_exceptions},
        "sla": {"breaches": breaches["count"], "unassigned": breaches["unassigned"]},
        "at": now.isoformat(),
    }
