"""缺陷处理 SLA：按严重级别计算超时未闭环项。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Finding, Project

DEFAULT_SLA_MINUTES = {
    "critical": 4 * 60,
    "high": 24 * 60,
    "medium": 3 * 24 * 60,
    "low": 7 * 24 * 60,
}
CLOSED_FINDING_STATUSES = {"false_positive", "accepted", "wont_fix"}


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def sla_breaches(db: Session, project: Project) -> dict[str, Any]:
    settings = project.settings_json or {}
    configured = settings.get("finding_sla_minutes")
    sla = dict(DEFAULT_SLA_MINUTES)
    if isinstance(configured, dict):
        for key, value in configured.items():
            try:
                sla[str(key)] = max(1, int(value))
            except (TypeError, ValueError):
                continue
    now = datetime.now(timezone.utc)
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.status.not_in(CLOSED_FINDING_STATUSES),
            Finding.fix_status != "verified_fixed",
        )
    ).all()
    rows: list[dict[str, Any]] = []
    for finding in findings:
        minutes = sla.get(str(finding.severity or "medium"), sla["medium"])
        created = _as_aware(finding.created_at) if finding.created_at else now
        age = max(0, int((now - created).total_seconds() // 60))
        if age <= minutes:
            continue
        rows.append(
            {
                "finding_id": finding.id,
                "title": finding.title,
                "severity": finding.severity,
                "status": finding.status,
                "fix_status": finding.fix_status,
                "assignee": finding.assignee or "",
                "age_minutes": age,
                "sla_minutes": minutes,
                "overdue_minutes": age - minutes,
            }
        )
    rows.sort(key=lambda item: (-item["overdue_minutes"], item["finding_id"]))
    return {
        "project_id": project.id,
        "sla_minutes": sla,
        "count": len(rows),
        "unassigned": sum(1 for item in rows if not item["assignee"]),
        "items": rows,
    }
