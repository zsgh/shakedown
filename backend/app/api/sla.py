"""SLA 超时检查 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError
from app.escalation import run_sla_escalation
from app.exceptions import list_exceptions
from app.models import Project
from app.sla import sla_breaches

router = APIRouter(tags=["automation"])


@router.get("/projects/{project_id}/sla/breaches")
def get_sla_breaches(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return sla_breaches(db, project)


@router.post("/projects/{project_id}/sla/escalate")
def escalate_sla_breaches(
    project_id: int,
    dry_run: bool = Query(default=True),
    min_severity: str | None = Query(default=None, pattern="^(low|medium|high|critical)$"),
    force: bool = Query(default=False, description="项目未开启 SLA 升级时仍手工执行"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """扫描 SLA 超时项并升级为例外 / 通知；默认 dry_run=true 只预览。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    result = run_sla_escalation(db, project, dry_run=dry_run, min_severity=min_severity, force=force)
    db.commit()
    return result


@router.get("/projects/{project_id}/sla/escalations")
def list_sla_escalations(
    project_id: int,
    status: str | None = Query(default="open"),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    rows = list_exceptions(db, project, status=status, kind="sla_breach", limit=limit)
    return {
        "project_id": project.id,
        "count": len(rows),
        "items": [
            {
                "id": row.id,
                "finding_id": row.target_id,
                "severity": row.severity,
                "status": row.status,
                "owner": row.owner,
                "note": row.note,
                "payload": row.payload or {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }
