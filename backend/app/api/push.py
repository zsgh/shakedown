"""主动推送 API：查看投递记录、手动触发 digest / 重试。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import TaskKind
from app.errors import NotFoundError, ValidationError
from app.models import Project, WebhookDelivery
from app.push import (
    MAX_WEBHOOK_ATTEMPTS,
    has_notification_target,
    notification_email,
    queue_digest,
    webhook_url,
)
from app.worker.queue import enqueue

router = APIRouter(tags=["push"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.get("/projects/{project_id}/webhook-deliveries")
def list_deliveries(
    project_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    stmt = select(WebhookDelivery).where(WebhookDelivery.project_id == project.id)
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    rows = db.scalars(
        stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(limit)
    ).all()
    return {
        "webhook_configured": bool(webhook_url(project)),
        "email_configured": bool(notification_email(project)),
        "notification_configured": has_notification_target(project),
        "items": [
            {
                "id": row.id,
                "event": row.event,
                "url": row.url,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.post("/projects/{project_id}/push/digest")
def push_digest(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """立即生成并投递一次 digest（actionable + 契约缺口摘要）。"""
    project = _project(db, project_id)
    if not has_notification_target(project):
        raise ValidationError("项目未配置 settings.webhook_url 或 settings.notification_email")
    delivery = queue_digest(db, project)
    db.commit()
    return {"delivery_id": delivery.id if delivery else None, "status": "queued"}


@router.post("/projects/{project_id}/webhook-deliveries/{delivery_id}/retry")
def retry_delivery(project_id: int, delivery_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.project_id != project.id:
        raise NotFoundError(f"投递记录不存在：{delivery_id}")
    delivery.status = "pending"
    delivery.last_error = ""
    delivery.attempts = 0
    enqueue(
        db,
        TaskKind.DELIVER_WEBHOOK.value,
        {"delivery_id": delivery.id},
        project_id=project.id,
        priority=200,
        max_attempts=MAX_WEBHOOK_ATTEMPTS,
    )
    db.commit()
    return {"delivery_id": delivery.id, "status": "queued"}
