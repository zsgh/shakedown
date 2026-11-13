"""Flaky 用例隔离：记录不稳定次数，达到阈值后移出执行集等待人工复核。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project, TestCase


def record_flaky_result(
    db: Session,
    project: Project,
    case: TestCase,
    *,
    rerun_status: str = "",
) -> dict[str, Any]:
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    automation = settings.get("automation") if isinstance(settings.get("automation"), dict) else {}
    enabled = bool(automation.get("flaky_quarantine", False))
    threshold = max(1, int(automation.get("flaky_threshold", 2)))
    spec = dict(case.spec or {})
    stats = spec.get("flaky_stats") if isinstance(spec.get("flaky_stats"), dict) else {}
    count = int(stats.get("count") or 0) + 1
    history = list(stats.get("history") or [])
    history.append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "rerun_status": rerun_status,
        }
    )
    spec["flaky_stats"] = {"count": count, "history": history[-10:]}
    tags = [str(tag) for tag in (case.tags or [])]
    if "flaky" not in tags:
        tags.append("flaky")
    quarantined = False
    if enabled and count >= threshold and case.status == "enabled":
        case.status = "needs_review"
        quarantined = True
        if "flaky-quarantined" not in tags:
            tags.append("flaky-quarantined")
        spec["flaky_quarantine"] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "count": count,
            "threshold": threshold,
            "reason": f"连续 {count} 次结果不一致，已移出执行集等待人工复核",
        }
        from app.exceptions import record_exception

        record_exception(
            db,
            project,
            kind="flaky_case",
            target_type="case",
            target_id=case.id,
            severity="medium",
            note=f"flaky 隔离：{case.title}（{count} 次不一致）",
            payload={"case_id": case.id, "count": count, "threshold": threshold},
        )
    case.spec = spec
    case.tags = tags
    db.flush()
    return {"count": count, "quarantined": quarantined, "threshold": threshold}
