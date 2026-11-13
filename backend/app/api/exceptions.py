"""无人值守例外队列 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError
from app.exceptions import list_exceptions, update_exception
from app.models import Project
from app.schemas import AutomationExceptionOut, AutomationExceptionUpdate

router = APIRouter(tags=["automation"])


@router.get("/projects/{project_id}/exceptions", response_model=list[AutomationExceptionOut])
def get_exceptions(
    project_id: int,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return list_exceptions(db, project, status=status, kind=kind, limit=limit)


@router.patch("/exceptions/{exception_id}", response_model=AutomationExceptionOut)
def patch_exception(
    exception_id: int,
    payload: AutomationExceptionUpdate,
    db: Session = Depends(get_db),
) -> Any:
    try:
        row = update_exception(
            db,
            exception_id,
            status=payload.status,
            owner=payload.owner,
            note=payload.note,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    db.commit()
    db.refresh(row)
    return row
