"""Artifact 保留策略：按时间清理，保留最近 N 次。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.artifact_retention import prune_artifacts
from app.models import Run


def _run(session, project_id: int, status: str = "completed") -> Run:
    run = Run(project_id=project_id, status=status, case_ids=[], stats={})
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def test_prune_artifacts_dry_run_and_apply(session, sample_project, tmp_path):
    old = _run(session, sample_project.id)
    new = _run(session, sample_project.id)
    session.execute(
        update(Run)
        .where(Run.id == old.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=90))
    )
    session.commit()
    old_dir = tmp_path / f"run-{old.id}"
    new_dir = tmp_path / f"run-{new.id}"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / "screenshot.png").write_bytes(b"old")
    (new_dir / "screenshot.png").write_bytes(b"new")

    preview = prune_artifacts(
        session, sample_project, artifacts_root=tmp_path, retention_days=30, keep_runs=0, dry_run=True
    )
    assert preview["count"] == 1
    assert preview["items"][0]["run_id"] == old.id
    assert old_dir.exists()

    applied = prune_artifacts(
        session, sample_project, artifacts_root=tmp_path, retention_days=30, keep_runs=0, dry_run=False
    )
    assert applied["count"] == 1
    assert not old_dir.exists()
    assert new_dir.exists()


def test_prune_artifacts_keeps_recent_runs_even_if_old(session, sample_project, tmp_path):
    old = _run(session, sample_project.id)
    session.execute(
        update(Run)
        .where(Run.id == old.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(days=90))
    )
    session.commit()
    old_dir = tmp_path / f"run-{old.id}"
    old_dir.mkdir()
    (old_dir / "a.txt").write_text("x", encoding="utf-8")

    result = prune_artifacts(
        session, sample_project, artifacts_root=tmp_path, retention_days=1, keep_runs=1, dry_run=False
    )
    assert result["count"] == 0
    assert old_dir.exists()
