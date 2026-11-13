"""Run artifact 保留策略：按时间 / 最近 N 次清理 ``artifacts/run-*``。"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, Run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_dir(root: Path, run_id: int) -> Path:
    return root / f"run-{run_id}"


def _dir_stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_file():
            files += 1
            size += item.stat().st_size
    return files, size


def prune_artifacts(
    db: Session,
    project: Project,
    *,
    artifacts_root: str | Path,
    retention_days: int = 30,
    keep_runs: int = 5,
    dry_run: bool = True,
) -> dict[str, Any]:
    """删除过期 run 的 artifacts 目录；默认 dry-run。"""
    root = Path(artifacts_root)
    cutoff = _now() - timedelta(days=max(1, int(retention_days)))
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.project_id == project.id, Run.status.in_(["completed", "failed", "cancelled"]))
            .order_by(Run.created_at.desc(), Run.id.desc())
        ).all()
    )
    keep_ids = {run.id for run in runs[: max(0, int(keep_runs))]}
    candidates: list[dict[str, Any]] = []
    for run in runs:
        if run.id in keep_ids:
            continue
        created = run.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is not None and created >= cutoff:
            continue
        path = _run_dir(root, run.id)
        files, size = _dir_stats(path)
        candidates.append(
            {
                "run_id": run.id,
                "status": run.status,
                "created_at": created.isoformat() if created else None,
                "path": str(path),
                "exists": path.exists(),
                "files": files,
                "bytes": size,
            }
        )
    if not dry_run:
        for item in candidates:
            path = Path(item["path"])
            if path.exists() and root in path.resolve().parents:
                shutil.rmtree(path, ignore_errors=True)
    return {
        "dry_run": bool(dry_run),
        "count": len(candidates),
        "files": sum(item["files"] for item in candidates),
        "bytes": sum(item["bytes"] for item in candidates),
        "kept_recent": len(keep_ids),
        "items": candidates[:200],
        "retention_days": max(1, int(retention_days)),
    }
