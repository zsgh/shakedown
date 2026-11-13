"""攻击用例包 API：扫描、幂等生成、可选验证。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError
from app.models import Project
from app.security.generator import generate_cases, scan_project

router = APIRouter(tags=["security"])


class AttackPackRequest(BaseModel):
    pattern_ids: list[str] | None = Field(default=None, description="为空时跑全部模式")
    include_findings: bool = True


class AttackPackGenerateRequest(AttackPackRequest):
    model_config = {"populate_by_name": True}

    dry_run: bool = False
    validate_cases: bool = Field(default=False, alias="validate", description="生成后立即排队试运行")


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/attack-pack")
def get_attack_pack(
    project_id: int,
    findings: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    return scan_project(db, project, include_findings=findings)


@router.post("/projects/{project_id}/attack-pack/scan")
def scan_attack_pack(
    project_id: int,
    payload: AttackPackRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    return scan_project(db, project, include_findings=payload.include_findings, pattern_ids=payload.pattern_ids)


@router.post("/projects/{project_id}/attack-pack/generate")
def generate_attack_pack(
    project_id: int,
    payload: AttackPackGenerateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    result = generate_cases(
        db,
        project,
        pattern_ids=payload.pattern_ids,
        dry_run=payload.dry_run,
        validate=payload.validate_cases,
    )
    if not payload.dry_run:
        db.commit()
    return result


__all__ = ["router"]
