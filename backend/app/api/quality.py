"""断言质量 / oracle mutation / oracle 趋势 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.assertion_quality import mutation_audit, oracle_trend, project_assertion_quality
from app.db import get_db
from app.errors import NotFoundError
from app.models import Project

router = APIRouter(tags=["quality"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/assertion-quality")
def get_assertion_quality(
    project_id: int,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return project_assertion_quality(db, _project(db, project_id), limit=limit)


@router.get("/projects/{project_id}/assertion-quality/mutation")
def get_assertion_mutation(
    project_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return mutation_audit(db, _project(db, project_id), limit=limit)


@router.get("/projects/{project_id}/oracle-trend")
def get_oracle_trend(
    project_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return oracle_trend(db, _project(db, project_id), limit=limit)
