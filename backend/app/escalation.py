"""SLA 定时升级：把超时未闭环的 finding 变成可跟踪例外并推送通知。

设计约束：
- 同一 finding 只保留一个未关闭的 ``sla_breach`` 例外；
- 只在首次创建例外时推送 webhook，避免每小时刷屏；
- 只读 finding / SLA 数据，不修改 finding 状态或负责人。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exceptions import record_exception
from app.models import AutomationException, Project
from app.push import queue_event
from app.sla import sla_breaches

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
OPEN_EXCEPTION_STATUSES = {"open", "acked"}


def automation_settings(project: Project) -> dict[str, Any]:
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    automation = settings.get("automation") if isinstance(settings.get("automation"), dict) else {}
    return automation


def sla_escalation_settings(project: Project) -> dict[str, Any]:
    automation = automation_settings(project)
    minimum = str(automation.get("sla_escalation_min_severity") or "medium").lower()
    if minimum not in SEVERITY_RANK:
        minimum = "medium"
    return {
        "enabled": bool(automation.get("sla_escalation_enabled", False)),
        "min_severity": minimum,
        "include_unassigned": bool(automation.get("sla_escalation_include_unassigned", True)),
        "notify": bool(automation.get("sla_escalation_notify", True)),
    }


def _open_exception(db: Session, project: Project, finding_id: int) -> AutomationException | None:
    return db.scalar(
        select(AutomationException).where(
            AutomationException.project_id == project.id,
            AutomationException.kind == "sla_breach",
            AutomationException.target_type == "finding",
            AutomationException.target_id == finding_id,
            AutomationException.status.in_(OPEN_EXCEPTION_STATUSES),
        )
    )


def run_sla_escalation(
    db: Session,
    project: Project,
    *,
    dry_run: bool = True,
    min_severity: str | None = None,
    include_unassigned: bool | None = None,
    notify: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """扫描 SLA 超时项并升级；默认只预览。

    项目未开启 ``sla_escalation_enabled`` 时，``dry_run=True`` 仍可预览；
    ``dry_run=False`` 必须显式 ``force=True`` 才写库，避免手工 API 绕过 opt-in。
    """
    config = sla_escalation_settings(project)
    if not config["enabled"] and not dry_run and not force:
        return {
            "dry_run": False,
            "enabled": False,
            "count": 0,
            "created": 0,
            "min_severity": min_severity or config["min_severity"],
            "include_unassigned": config["include_unassigned"] if include_unassigned is None else include_unassigned,
            "notify": config["notify"] if notify is None else notify,
            "sla_minutes": sla_breaches(db, project)["sla_minutes"],
            "items": [],
            "reason": "项目未开启 settings.automation.sla_escalation_enabled；如需手工执行请传 force=true",
        }
    minimum = str(min_severity or config["min_severity"]).lower()
    if minimum not in SEVERITY_RANK:
        minimum = "medium"
    include_unassigned = config["include_unassigned"] if include_unassigned is None else include_unassigned
    notify = config["notify"] if notify is None else notify
    breaches = sla_breaches(db, project)
    threshold = SEVERITY_RANK[minimum]
    items: list[dict[str, Any]] = []
    created = 0
    for breach in breaches["items"]:
        severity = str(breach.get("severity") or "medium").lower()
        if SEVERITY_RANK.get(severity, 2) < threshold:
            continue
        if not include_unassigned and not breach.get("assignee"):
            continue
        existing = _open_exception(db, project, int(breach["finding_id"]))
        would_create = existing is None
        row = {
            **breach,
            "already_escalated": not would_create,
            "would_create": would_create,
        }
        items.append(row)
        if dry_run:
            created += int(would_create)
            continue
        exception = record_exception(
            db,
            project,
            kind="sla_breach",
            target_type="finding",
            target_id=int(breach["finding_id"]),
            severity=severity,
            note=(
                f"SLA 超时：{breach['title']}（{severity}，超时 "
                f"{breach['overdue_minutes']} 分钟，负责人 {breach.get('assignee') or '未指派'}）"
            ),
            payload=breach,
        )
        if would_create:
            created += 1
            if notify:
                queue_event(db, project, "sla_breach", {"exception_id": exception.id, **breach})
        else:
            exception.payload = {**(exception.payload or {}), **breach}
    db.flush()
    return {
        "dry_run": bool(dry_run),
        "enabled": bool(config["enabled"]),
        "count": len(items),
        "created": created,
        "min_severity": minimum,
        "include_unassigned": include_unassigned,
        "notify": notify,
        "sla_minutes": breaches["sla_minutes"],
        "items": items[:200],
    }
