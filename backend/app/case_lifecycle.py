"""用例生命周期：过期 / 重复用例的非破坏性退役。

退役只把用例置为 disabled 并写 tags/spec 元数据，不删除数据、不删执行历史。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, TestCase

RETIRED_TAG = "retired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_title(value: str) -> str:
    text = re.sub(r"^\[[^\]]+\]\s*", "", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).lower()
    return text


def _is_retired(case: TestCase) -> bool:
    return RETIRED_TAG in {str(tag) for tag in (case.tags or [])}


def collect_retirement_candidates(
    db: Session,
    project: Project,
    *,
    stale_days: int = 30,
    include_missing_input: bool = False,
    include_duplicates: bool = True,
    include_enabled: bool = False,
) -> list[dict[str, Any]]:
    """返回待退役用例；过期优先，重复只保留优先级最高/最旧的一条。"""
    now = _now()
    cutoff = now - timedelta(days=max(1, int(stale_days)))
    statuses = {"pending", "needs_review"}
    if include_enabled:
        statuses.add("enabled")
    cases = list(
        db.scalars(
            select(TestCase)
            .where(TestCase.project_id == project.id, TestCase.status.in_(statuses))
            .order_by(TestCase.priority, TestCase.id)
        ).all()
    )
    candidates: dict[int, dict[str, Any]] = {}
    for case in cases:
        if _is_retired(case):
            continue
        if case.missing_input and not include_missing_input:
            continue
        updated = case.updated_at
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated is not None and updated < cutoff:
            candidates[case.id] = {
                "case_id": case.id,
                "title": case.title,
                "status": case.status,
                "reason": "stale",
                "detail": f"超过 {stale_days} 天未更新",
            }

    if include_duplicates:
        groups: dict[tuple[Any, str], list[TestCase]] = {}
        for case in cases:
            if case.id in candidates or _is_retired(case):
                continue
            key = (case.feature_id, _normalize_title(case.title))
            if not key[1]:
                continue
            groups.setdefault(key, []).append(case)
        for group in groups.values():
            if len(group) < 2:
                continue
            # 保留优先级最高（数字最小）、再按 id 最早的一条
            keep = sorted(group, key=lambda item: (int(item.priority or 100), item.id))[0]
            for case in group:
                if case.id == keep.id:
                    continue
                candidates[case.id] = {
                    "case_id": case.id,
                    "title": case.title,
                    "status": case.status,
                    "reason": "duplicate",
                    "detail": f"与用例 #{keep.id} 重复，保留 #{keep.id}",
                }
    return sorted(candidates.values(), key=lambda item: (item["reason"], item["case_id"]))


def apply_case_retirement(
    db: Session,
    project: Project,
    *,
    candidates: list[dict[str, Any]],
    actor: str = "local",
) -> list[int]:
    """把候选用例置为 disabled 并打退役标记。"""
    retired: list[int] = []
    now = _now()
    for item in candidates:
        case = db.get(TestCase, int(item["case_id"]))
        if case is None or case.project_id != project.id:
            continue
        spec = dict(case.spec or {})
        spec["retirement"] = {
            "reason": item["reason"],
            "detail": item.get("detail") or "",
            "at": now.isoformat(),
            "actor": actor,
        }
        case.spec = spec
        tags = [str(tag) for tag in (case.tags or [])]
        if RETIRED_TAG not in tags:
            tags.append(RETIRED_TAG)
        case.tags = tags
        case.status = "disabled"
        retired.append(case.id)
    db.flush()
    return retired


def run_case_retirement(
    db: Session,
    project: Project,
    *,
    dry_run: bool = True,
    stale_days: int = 30,
    include_missing_input: bool = False,
    include_duplicates: bool = True,
    include_enabled: bool = False,
    actor: str = "local",
) -> dict[str, Any]:
    candidates = collect_retirement_candidates(
        db,
        project,
        stale_days=stale_days,
        include_missing_input=include_missing_input,
        include_duplicates=include_duplicates,
        include_enabled=include_enabled,
    )
    retired: list[int] = []
    if not dry_run:
        retired = apply_case_retirement(db, project, candidates=candidates, actor=actor)
    return {
        "dry_run": bool(dry_run),
        "count": len(candidates),
        "retired": retired,
        "cases": candidates[:200],
        "stale_days": max(1, int(stale_days)),
    }
