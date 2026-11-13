"""视觉基线 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.a11y import audit_metrics, latest_a11y
from app.db import get_db
from app.errors import NotFoundError
from app.models import Project
from app.schemas import VisualBaselineCompare, VisualBaselineCreate, VisualBaselineOut
from app.visual import compare_baseline, latest_screenshot, list_baselines, set_baseline

router = APIRouter(tags=["visual"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/visual/baselines", response_model=list[VisualBaselineOut])
def get_visual_baselines(project_id: int, db: Session = Depends(get_db)) -> list[Any]:
    return list_baselines(db, _project(db, project_id))


@router.get("/projects/{project_id}/visual/latest")
def get_latest_screenshot(
    project_id: int,
    case_id: int = Query(ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        return latest_screenshot(db, project, case_id) or {}
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/projects/{project_id}/ui/a11y")
def get_case_a11y(
    project_id: int,
    case_id: int = Query(ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        return latest_a11y(db, project, case_id) or {"case_id": case_id, "metrics": {}, "passed": None}
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc


@router.post("/projects/{project_id}/ui/a11y/audit")
def audit_a11y_metrics(project_id: int, metrics: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    _project(db, project_id)
    return audit_metrics(metrics)


@router.post("/projects/{project_id}/visual/baselines", response_model=VisualBaselineOut, status_code=201)
def create_visual_baseline(
    project_id: int,
    payload: VisualBaselineCreate,
    db: Session = Depends(get_db),
) -> Any:
    project = _project(db, project_id)
    try:
        row = set_baseline(
            db,
            project,
            payload.case_id,
            name=payload.name,
            sha256=payload.sha256,
            screenshot_path=payload.screenshot_path,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    db.commit()
    db.refresh(row)
    return row


@router.post("/projects/{project_id}/visual/compare")
def compare_visual_baseline(
    project_id: int,
    payload: VisualBaselineCompare,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        return compare_baseline(db, project, payload.case_id, name=payload.name)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
