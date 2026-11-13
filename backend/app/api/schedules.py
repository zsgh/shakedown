"""项目调度 / 环境就绪 API。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.environment_check import check_target
from app.errors import NotFoundError
from app.models import Project, ProjectSchedule, Run
from app.run_service import create_run_for_project
from app.schemas import ProjectScheduleCreate, ProjectScheduleOut, ProjectScheduleUpdate

router = APIRouter(tags=["schedules"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


def _schedule(db: Session, schedule_id: int) -> ProjectSchedule:
    schedule = db.get(ProjectSchedule, schedule_id)
    if schedule is None:
        raise NotFoundError(f"调度不存在：{schedule_id}")
    return schedule


@router.get("/projects/{project_id}/schedules", response_model=list[ProjectScheduleOut])
def list_schedules(project_id: int, db: Session = Depends(get_db)) -> list[ProjectSchedule]:
    _project(db, project_id)
    return list(
        db.scalars(
            select(ProjectSchedule)
            .where(ProjectSchedule.project_id == project_id)
            .order_by(ProjectSchedule.id)
        ).all()
    )


@router.post("/projects/{project_id}/schedules", response_model=ProjectScheduleOut, status_code=201)
def create_schedule(
    project_id: int,
    payload: ProjectScheduleCreate,
    db: Session = Depends(get_db),
) -> ProjectSchedule:
    project = _project(db, project_id)
    now = _now()
    schedule = ProjectSchedule(
        project_id=project.id,
        name=payload.name or "默认调度",
        enabled=payload.enabled,
        interval_seconds=payload.interval_seconds,
        next_run_at=payload.next_run_at or (now + timedelta(seconds=payload.interval_seconds)),
        policy=payload.policy.model_dump(),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.get("/schedules/{schedule_id}", response_model=ProjectScheduleOut)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> ProjectSchedule:
    return _schedule(db, schedule_id)


@router.patch("/schedules/{schedule_id}", response_model=ProjectScheduleOut)
def update_schedule(
    schedule_id: int,
    payload: ProjectScheduleUpdate,
    db: Session = Depends(get_db),
) -> ProjectSchedule:
    schedule = _schedule(db, schedule_id)
    if payload.name is not None:
        schedule.name = payload.name
    if payload.enabled is not None:
        schedule.enabled = payload.enabled
        if payload.enabled and schedule.next_run_at is None:
            schedule.next_run_at = _now() + timedelta(seconds=max(60, schedule.interval_seconds or 86400))
        if not payload.enabled:
            schedule.next_run_at = None
    if payload.interval_seconds is not None:
        schedule.interval_seconds = payload.interval_seconds
        if schedule.enabled:
            schedule.next_run_at = _now() + timedelta(seconds=payload.interval_seconds)
    if payload.next_run_at is not None:
        schedule.next_run_at = payload.next_run_at
    if payload.policy is not None:
        schedule.policy = payload.policy.model_dump()
    db.commit()
    db.refresh(schedule)
    return schedule


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    schedule = _schedule(db, schedule_id)
    db.delete(schedule)
    db.commit()
    return {"deleted": True, "schedule_id": schedule_id}


@router.get("/projects/{project_id}/readiness")
def project_readiness(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    return check_target(project)


@router.post("/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    schedule = _schedule(db, schedule_id)
    project = _project(db, schedule.project_id)
    readiness = check_target(project)
    if not readiness.get("ok"):
        reason = str(readiness.get("reason") or "环境未就绪")
        run = Run(
            project_id=project.id,
            trigger="schedule",
            status="failed",
            case_ids=[],
            env_snapshot={"base_url": project.base_url, "readiness": readiness},
            stats={"total": 0, "environment": readiness, "schedule_id": schedule.id},
            error=f"环境未就绪：{reason}",
        )
        db.add(run)
        db.flush()
        schedule.last_run_id = run.id
        schedule.last_run_at = _now()
        schedule.last_status = "environment_unavailable"
        schedule.last_error = reason
        try:
            from app.exceptions import record_exception

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
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.push import queue_event

            queue_event(db, project, "environment_unavailable", readiness)
        except Exception:  # noqa: BLE001
            pass
        db.commit()
        return {"run_id": run.id, "status": run.status, "readiness": readiness, "created": False}

    run = create_run_for_project(
        db,
        project,
        trigger="schedule",
        policy=schedule.policy or {},
        skip_empty=True,
    )
    schedule.last_run_at = _now()
    if run is None:
        schedule.last_status = "skipped"
        schedule.last_error = "没有可执行用例"
        db.commit()
        return {"run_id": None, "status": "skipped", "readiness": readiness, "created": False}
    schedule.last_run_id = run.id
    schedule.last_status = run.status
    schedule.last_error = ""
    db.commit()
    return {"run_id": run.id, "status": run.status, "readiness": readiness, "created": True}
