"""定时 digest / 周报：按项目配置的间隔投递摘要，失败由 webhook 队列重试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, Run, WebhookDelivery
from app.push import build_digest, has_notification_target, queue_event
from app.sla import sla_breaches


def _now() -> datetime:
    return datetime.now(timezone.utc)


def digest_settings(project: Project) -> dict[str, Any]:
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    automation = settings.get("automation") if isinstance(settings.get("automation"), dict) else {}
    interval = int(automation.get("digest_interval_seconds", 86400) or 86400)
    return {
        "enabled": bool(automation.get("digest_enabled", False)),
        "interval_seconds": max(3600, interval),
        "weekly": interval >= 7 * 86400,
    }


def build_scheduled_digest(db: Session, project: Project) -> dict[str, Any]:
    base = build_digest(db, project)
    since = _now() - timedelta(days=7)
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.project_id == project.id, Run.created_at >= since)
            .order_by(Run.id.desc())
            .limit(50)
        ).all()
    )
    breaches = sla_breaches(db, project)
    base.update(
        {
            "period": "weekly" if digest_settings(project)["weekly"] else "daily",
            "since": since.isoformat(),
            "runs_7d": len(runs),
            "recent_runs": [
                {
                    "id": run.id,
                    "status": run.status,
                    "trigger": run.trigger,
                    "pass_rate": (run.stats or {}).get("pass_rate") if isinstance(run.stats, dict) else None,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                }
                for run in runs[:10]
            ],
            "sla_breaches": breaches["count"],
            "sla_unassigned": breaches["unassigned"],
        }
    )
    return base


def queue_scheduled_digest(db: Session, project: Project) -> dict[str, Any]:
    config = digest_settings(project)
    if not config["enabled"]:
        return {"queued": False, "reason": "digest 未启用"}
    if not has_notification_target(project):
        return {"queued": False, "reason": "未配置 webhook_url 或 notification_email"}
    latest = db.scalar(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.project_id == project.id,
            WebhookDelivery.event.in_(["digest", "weekly_digest"]),
        )
        .order_by(WebhookDelivery.id.desc())
        .limit(1)
    )
    if latest is not None and latest.created_at is not None:
        created = latest.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created > _now() - timedelta(seconds=config["interval_seconds"]):
            return {"queued": False, "reason": "未到 digest 间隔", "last_delivery_id": latest.id}
    event = "weekly_digest" if config["weekly"] else "digest"
    delivery = queue_event(db, project, event, build_scheduled_digest(db, project))
    return {"queued": bool(delivery), "event": event, "delivery_id": delivery.id if delivery else None}
