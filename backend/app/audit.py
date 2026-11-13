"""项目审计视图：把人工反馈、例外、webhook、Agent trace 统一成时间线。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentTrace, AutomationException, HumanFeedback, Project, WebhookDelivery


def list_audit(
    db: Session,
    project: Project,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    if not kind or kind == "feedback":
        for row in db.scalars(
            select(HumanFeedback)
            .where(HumanFeedback.project_id == project.id)
            .order_by(HumanFeedback.created_at.desc())
            .limit(limit)
        ).all():
            rows.append(
                {
                    "kind": "feedback",
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "actor": row.author,
                    "action": row.action,
                    "target": f"{row.target_type}:{row.target_id}",
                    "detail": row.note,
                    "payload": {"before": row.before, "after": row.after},
                }
            )

    if not kind or kind == "exception":
        for row in db.scalars(
            select(AutomationException)
            .where(AutomationException.project_id == project.id)
            .order_by(AutomationException.created_at.desc())
            .limit(limit)
        ).all():
            rows.append(
                {
                    "kind": "exception",
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "actor": row.owner or "auto",
                    "action": row.status,
                    "target": f"{row.target_type}:{row.target_id}" if row.target_id else row.kind,
                    "detail": row.note,
                    "payload": row.payload,
                }
            )

    if not kind or kind == "webhook":
        for row in db.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.project_id == project.id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        ).all():
            rows.append(
                {
                    "kind": "webhook",
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "actor": "system",
                    "action": row.status,
                    "target": row.event,
                    "detail": row.last_error,
                    "payload": {"url": row.url, "attempts": row.attempts},
                }
            )

    if not kind or kind == "trace":
        for row in db.scalars(
            select(AgentTrace)
            .where(AgentTrace.project_id == project.id)
            .order_by(AgentTrace.created_at.desc())
            .limit(limit)
        ).all():
            rows.append(
                {
                    "kind": "trace",
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "actor": row.model or "agent",
                    "action": row.kind,
                    "target": row.step,
                    "detail": row.error or "",
                    "payload": {
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "duration_ms": row.duration_ms,
                    },
                }
            )

    rows.sort(key=lambda item: item.get("at") or "", reverse=True)
    return rows[: max(1, min(limit, 500))]
