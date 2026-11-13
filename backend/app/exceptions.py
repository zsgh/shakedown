"""无人值守例外队列：自动动作做不了/不敢做时，统一落一条可跟踪事项。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AutomationException, Project

OPEN_STATUSES = {"open", "acked"}
EXCEPTION_STATUSES = {"open", "acked", "resolved", "ignored"}


def record_exception(
    db: Session,
    project: Project,
    *,
    kind: str,
    target_type: str = "",
    target_id: int | None = None,
    severity: str = "medium",
    note: str = "",
    payload: dict[str, Any] | None = None,
) -> AutomationException:
    """同一 kind+target 只保留一个未关闭例外，避免每个 run 刷一条。"""
    existing = None
    if target_type and target_id is not None:
        existing = db.scalar(
            select(AutomationException).where(
                AutomationException.project_id == project.id,
                AutomationException.kind == kind,
                AutomationException.target_type == target_type,
                AutomationException.target_id == target_id,
                AutomationException.status.in_(OPEN_STATUSES),
            )
        )
    if existing is not None:
        existing.note = note or existing.note
        existing.payload = payload or existing.payload
        existing.severity = severity or existing.severity
        db.flush()
        return existing
    row = AutomationException(
        project_id=project.id,
        kind=kind,
        target_type=target_type,
        target_id=target_id,
        severity=severity or "medium",
        note=note or "",
        payload=payload or {},
    )
    db.add(row)
    db.flush()
    return row


def list_exceptions(
    db: Session,
    project: Project,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[AutomationException]:
    stmt = select(AutomationException).where(AutomationException.project_id == project.id)
    if status:
        stmt = stmt.where(AutomationException.status == status)
    if kind:
        stmt = stmt.where(AutomationException.kind == kind)
    return list(
        db.scalars(
            stmt.order_by(AutomationException.created_at.desc(), AutomationException.id.desc())
            .limit(max(1, min(limit, 500)))
        ).all()
    )


def update_exception(
    db: Session,
    exception_id: int,
    *,
    status: str | None = None,
    owner: str | None = None,
    note: str | None = None,
) -> AutomationException:
    row = db.get(AutomationException, exception_id)
    if row is None:
        raise ValueError(f"例外不存在：{exception_id}")
    if status is not None:
        if status not in EXCEPTION_STATUSES:
            raise ValueError(f"非法例外状态：{status}")
        row.status = status
    if owner is not None:
        row.owner = owner[:120]
    if note is not None:
        row.note = note
    db.flush()
    return row
