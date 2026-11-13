"""Run 创建与运行策略：手动、调度器、MCP 共用同一套选例逻辑。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import CaseStatus, RunStatus
from app.models import Project, Run, TestCase
from app.worker.queue import enqueue


def _policy_value(policy: dict[str, Any] | None, key: str, default: Any) -> Any:
    if not isinstance(policy, dict):
        return default
    value = policy.get(key, default)
    return default if value is None else value


def select_cases_for_policy(
    db: Session,
    project: Project,
    policy: dict[str, Any] | None = None,
    *,
    case_ids: list[int] | None = None,
    dimensions: list[str] | None = None,
    allow_needs_review: bool = False,
) -> list[TestCase]:
    """按运行策略选择可执行用例；默认只选 enabled，``allow_needs_review`` 可用于变更回归。"""
    policy = policy or {}
    statuses = [CaseStatus.ENABLED.value]
    if allow_needs_review:
        statuses.append(CaseStatus.NEEDS_REVIEW.value)
    stmt = select(TestCase).where(
        TestCase.project_id == project.id,
        TestCase.status.in_(statuses),
        func.jsonb_array_length(TestCase.steps) > 0,
    )
    cases = list(db.scalars(stmt.order_by(TestCase.priority, TestCase.id)).all())

    wanted_dimensions = dimensions if dimensions is not None else _policy_value(policy, "dimensions", [])
    if wanted_dimensions:
        wanted = set(str(item) for item in wanted_dimensions)
        cases = [case for case in cases if case.dimension in wanted]

    if case_ids is not None:
        wanted_ids = set(case_ids)
        cases = [case for case in cases if case.id in wanted_ids]

    mode = str(_policy_value(policy, "mode", "full") or "full")
    if mode == "smoke":
        smoke_tags = set(str(item) for item in _policy_value(policy, "smoke_tags", ["smoke"]))
        smoke_priority = int(_policy_value(policy, "smoke_priority", 50))
        smoke = [
            case
            for case in cases
            if smoke_tags.intersection(str(tag) for tag in (case.tags or []))
            or int(case.priority or 100) <= smoke_priority
        ]
        cases = smoke or cases[: int(_policy_value(policy, "smoke_fallback_limit", 20))]
    elif mode == "impacted":
        cases = [
            case
            for case in cases
            if bool((case.spec or {}).get("change_impact"))
            or bool((case.spec or {}).get("change_candidates"))
        ]

    tags = set(str(item) for item in _policy_value(policy, "tags", []))
    if tags:
        cases = [case for case in cases if tags.intersection(str(tag) for tag in (case.tags or []))]

    priority_max = _policy_value(policy, "priority_max", None)
    if priority_max is not None:
        cases = [case for case in cases if int(case.priority or 100) <= int(priority_max)]

    max_cases = int(_policy_value(policy, "max_cases", 200))
    return cases[: max(0, max_cases)]


def create_run_for_project(
    db: Session,
    project: Project,
    *,
    trigger: str = "manual",
    policy: dict[str, Any] | None = None,
    case_ids: list[int] | None = None,
    dimensions: list[str] | None = None,
    note: str = "",
    skip_empty: bool = False,
    allow_needs_review: bool = False,
) -> Run | None:
    """创建 Run 并入队；``skip_empty`` 为真时，没有可执行用例返回 None。"""
    cases = select_cases_for_policy(
        db,
        project,
        policy,
        case_ids=case_ids,
        dimensions=dimensions,
        allow_needs_review=allow_needs_review,
    )
    if not cases and skip_empty:
        return None
    if allow_needs_review:
        policy = {**(policy or {}), "allow_needs_review": True}
    run = Run(
        project_id=project.id,
        trigger=trigger,
        status=RunStatus.QUEUED.value if cases else RunStatus.COMPLETED.value,
        case_ids=[case.id for case in cases],
        env_snapshot={"base_url": project.base_url, "policy": policy or {}},
        stats={"total": len(cases), "policy": policy or {}, "note": note},
    )
    db.add(run)
    db.flush()
    if cases:
        task = enqueue(db, "execute_run", {"run_id": run.id}, project_id=project.id, run_id=run.id)
        run.task_id = task.id
    else:
        run.finished_at = datetime.now(timezone.utc)
    return run
