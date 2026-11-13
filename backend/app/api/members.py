"""项目角色矩阵 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ConflictError, NotFoundError
from app.models import Project, ProjectMember
from app.schemas import ProjectMemberCreate, ProjectMemberOut, ProjectMemberUpdate

router = APIRouter(tags=["projects"])
ROLES = {"owner", "maintainer", "viewer"}


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/members", response_model=list[ProjectMemberOut])
def list_members(project_id: int, db: Session = Depends(get_db)) -> list[Any]:
    _project(db, project_id)
    return list(
        db.scalars(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.role, ProjectMember.actor)
        ).all()
    )


@router.post("/projects/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
def create_member(
    project_id: int,
    payload: ProjectMemberCreate,
    db: Session = Depends(get_db),
) -> Any:
    project = _project(db, project_id)
    if payload.role not in ROLES:
        raise ConflictError(f"非法角色：{payload.role}")
    existing = db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.actor == payload.actor,
        )
    )
    if existing is not None:
        raise ConflictError(f"成员已存在：{payload.actor}")
    row = ProjectMember(
        project_id=project.id,
        actor=payload.actor.strip()[:120],
        role=payload.role,
        note=payload.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/members/{member_id}", response_model=ProjectMemberOut)
def update_member(
    member_id: int,
    payload: ProjectMemberUpdate,
    db: Session = Depends(get_db),
) -> Any:
    row = db.get(ProjectMember, member_id)
    if row is None:
        raise NotFoundError(f"成员不存在：{member_id}")
    if payload.role is not None:
        if payload.role not in ROLES:
            raise ConflictError(f"非法角色：{payload.role}")
        row.role = payload.role
    if payload.active is not None:
        row.active = payload.active
    if payload.note is not None:
        row.note = payload.note
    db.commit()
    db.refresh(row)
    return row


@router.delete("/members/{member_id}")
def delete_member(member_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(ProjectMember, member_id)
    if row is None:
        raise NotFoundError(f"成员不存在：{member_id}")
    db.delete(row)
    db.commit()
    return {"deleted": True, "member_id": member_id}
