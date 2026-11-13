"""用例重评估 API：按项目 SLA 重算性能断言、按最新 spec 快照复核受影响用例。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.assertion_migration import apply_assertion_migration, preview_assertion_migration
from app.case_review import apply_reevaluate, apply_sla_recompute, preview_reevaluate, preview_sla_recompute
from app.db import get_db
from app.errors import NotFoundError
from app.models import Project
from app.schemas import AssertionMigrationRequest, CaseReevaluateRequest, SlaRecomputeRequest

router = APIRouter(tags=["case-review"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/cases/sla-recompute")
def preview_sla(
    project_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """预览按项目 SLA 重算性能 timing 断言会影响哪些用例。"""
    project = _project(db, project_id)
    return preview_sla_recompute(db, project)


@router.post("/projects/{project_id}/cases/sla-recompute")
def run_sla_recompute(
    project_id: int,
    payload: SlaRecomputeRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """执行 SLA 重算；默认 dry_run=true，只返回预览。"""
    project = _project(db, project_id)
    if payload.dry_run:
        preview = preview_sla_recompute(db, project)
        if payload.case_ids:
            wanted = set(payload.case_ids)
            preview["cases"] = [row for row in preview["cases"] if row["case_id"] in wanted]
            preview["count"] = len(preview["cases"])
        return {"dry_run": True, **preview}
    try:
        result = apply_sla_recompute(
            db,
            project,
            case_ids=payload.case_ids,
            actor=payload.actor,
            note=payload.note,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    return {"dry_run": False, **result}


@router.get("/projects/{project_id}/cases/assertions/migrate")
def preview_assertion_migrations(
    project_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """预览存量无依据断言会迁成 policy 检查还是待确认问题。"""
    project = _project(db, project_id)
    return preview_assertion_migration(db, project)


@router.post("/projects/{project_id}/cases/assertions/migrate")
def run_assertion_migrations(
    project_id: int,
    payload: AssertionMigrationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """执行存量迁移；默认 dry_run=true，只返回预览。"""
    project = _project(db, project_id)
    if payload.dry_run:
        return {
            "dry_run": True,
            **preview_assertion_migration(db, project, case_ids=payload.case_ids),
        }
    return {
        "dry_run": False,
        **apply_assertion_migration(
            db,
            project,
            case_ids=payload.case_ids,
            actor=payload.actor,
            note=payload.note,
        ),
    }


@router.get("/projects/{project_id}/cases/reevaluate")
def preview_reevaluate_cases(
    project_id: int,
    snapshot_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """预览 spec diff 后需要重评估的用例范围和 pending 断言。"""
    project = _project(db, project_id)
    return preview_reevaluate(db, project, snapshot_id=snapshot_id)


@router.post("/projects/{project_id}/cases/reevaluate")
def run_reevaluate_cases(
    project_id: int,
    payload: CaseReevaluateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按最新 spec 快照重评估受影响用例；只处理受影响用例，不重建用例。"""
    project = _project(db, project_id)
    if payload.dry_run:
        preview = preview_reevaluate(
            db,
            project,
            snapshot_id=payload.snapshot_id,
            case_ids=payload.case_ids,
        )
        return {"dry_run": True, **preview}
    return {
        "dry_run": False,
        **apply_reevaluate(
            db,
            project,
            snapshot_id=payload.snapshot_id,
            case_ids=payload.case_ids,
            actor=payload.actor,
            note=payload.note,
        ),
    }
