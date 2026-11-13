"""批量重新评估用例：把“缺口很多但只能一条条点”变成可预览、可批量的动作。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.enums import TaskStatus
from app.gaps import collect_case_gaps, filter_gaps
from app.models import Project, Task
from app.worker.queue import enqueue


def _matched_gap_cases(
    db: Any,
    project: Project,
    *,
    filters: dict[str, Any],
    case_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    items = collect_case_gaps(db, project, include_disabled=bool(filters.get("include_disabled")))
    matched = filter_gaps(
        items,
        str(filters.get("kind") or "all"),
        include_closed=bool(filters.get("include_closed")),
        dimension=filters.get("dimension"),
        feature_id=filters.get("feature_id"),
        evidence_source=filters.get("evidence_source"),
        owner=filters.get("owner"),
        missing_input_kind=filters.get("missing_input_kind"),
        owner_kind=filters.get("owner_kind"),
        gap_status=filters.get("gap_status"),
        execution_state=filters.get("execution_state"),
    )
    if case_ids is not None:
        wanted = set(case_ids)
        matched = [item for item in matched if item["case_id"] in wanted]
    return matched


def preview_case_enrichment(
    db: Any,
    project: Project,
    *,
    filters: dict[str, Any],
    case_ids: list[int] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    matched = _matched_gap_cases(db, project, filters=filters, case_ids=case_ids)
    rows = [
        {
            "case_id": item["case_id"],
            "title": item["title"],
            "status": item["status"],
            "missing_input": item.get("missing_input"),
            "missing_input_kind": item.get("missing_input_kind"),
            "execution_states": item.get("execution_states") or [],
            "owner_kind": item.get("owner_kind"),
            "no_steps": item.get("no_steps"),
        }
        for item in matched[: max(1, min(limit, 200))]
    ]
    return {
        "count": len(matched),
        "returned": len(rows),
        "cases": rows,
        "filters": filters,
    }


def enqueue_case_ids(
    db: Any,
    project: Project,
    case_ids: list[int],
    *,
    actor: str = "auto",
    note: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """按用例 ID 直接入队，供变更影响面自动触发使用。"""
    active = db.scalars(
        select(Task).where(
            Task.project_id == project.id,
            Task.kind == "enrich_case",
            Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
        )
    ).all()
    active_case_ids = {
        int((task.payload or {}).get("case_id"))
        for task in active
        if (task.payload or {}).get("case_id") is not None
    }
    task_ids: list[int] = []
    skipped: list[int] = []
    for case_id in list(dict.fromkeys(case_ids))[: max(1, min(limit, 500))]:
        if case_id in active_case_ids:
            skipped.append(case_id)
            continue
        task = enqueue(
            db,
            "enrich_case",
            {
                "project_id": project.id,
                "case_id": case_id,
                "actor": actor,
                "note": note or "变更影响自动重新评估",
            },
            project_id=project.id,
        )
        task_ids.append(task.id)
    return {
        "enqueued": len(task_ids),
        "skipped_active": len(skipped),
        "skipped_case_ids": skipped,
        "task_ids": task_ids,
        "case_ids": [case_id for case_id in case_ids if case_id not in skipped],
    }


def enqueue_case_enrichment(
    db: Any,
    project: Project,
    *,
    filters: dict[str, Any],
    case_ids: list[int] | None = None,
    limit: int = 200,
    actor: str = "local",
    note: str = "",
) -> dict[str, Any]:
    preview = preview_case_enrichment(
        db, project, filters=filters, case_ids=case_ids, limit=limit
    )
    result = enqueue_case_ids(
        db,
        project,
        [row["case_id"] for row in preview["cases"]],
        actor=actor,
        note=note,
        limit=limit,
    )
    return {
        "count": preview["count"],
        **result,
    }


def count_reenrich_candidates(db: Any, project: Project) -> int:
    """reparse 后提示用：有多少条缺说明 / 缺步骤的用例值得重新评估。

    这里按需求统计「当前项目」的全部用例，因此包含 disabled 用例；
    是否实际批量入队仍由 enrich 的 include_disabled 过滤决定。
    """
    items = collect_case_gaps(db, project, include_disabled=True)
    return sum(1 for item in items if item.get("missing_input") or item.get("no_steps"))
