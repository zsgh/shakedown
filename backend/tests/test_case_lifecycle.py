"""过期 / 重复用例的非破坏性退役。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.case_lifecycle import RETIRED_TAG, collect_retirement_candidates, run_case_retirement
from app.models import Feature
from app.models import TestCase as CaseModel


def _case(project_id: int, feature_id: int, title: str, *, status: str = "pending", priority: int = 100) -> CaseModel:
    return CaseModel(
        project_id=project_id,
        feature_id=feature_id,
        title=title,
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/api/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/health"}], "assertions": []},
        status=status,
        priority=priority,
    )


def test_retire_stale_and_duplicate_pending_cases(session, sample_project, client):
    feature = Feature(project_id=sample_project.id, name="健康检查", module="health")
    session.add(feature)
    session.flush()
    stale = _case(sample_project.id, feature.id, "[正常流] 旧健康检查")
    duplicate_keep = _case(sample_project.id, feature.id, "[正常流] 健康检查", priority=10)
    duplicate = _case(sample_project.id, feature.id, "[正常流] 健康检查", priority=100)
    enabled = _case(sample_project.id, feature.id, "[正常流] 已启用健康检查", status="enabled")
    session.add_all([stale, duplicate_keep, duplicate, enabled])
    session.commit()
    session.execute(
        update(CaseModel)
        .where(CaseModel.id == stale.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(days=90))
    )
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/retire?stale_days=30").json()
    ids = {item["case_id"] for item in preview["cases"]}
    assert stale.id in ids
    assert duplicate.id in ids
    assert duplicate_keep.id not in ids
    assert enabled.id not in ids

    applied = client.post(
        f"/api/projects/{sample_project.id}/cases/retire?dry_run=false&stale_days=30"
    ).json()
    assert applied["dry_run"] is False
    assert set(applied["retired"]) == {stale.id, duplicate.id}
    for case in (stale, duplicate):
        session.refresh(case)
        assert case.status == "disabled"
        assert RETIRED_TAG in case.tags
        assert case.spec["retirement"]["reason"] in {"stale", "duplicate"}
    session.refresh(duplicate_keep)
    assert duplicate_keep.status == "pending"


def test_retirement_can_explicitly_include_enabled(session, sample_project):
    feature = Feature(project_id=sample_project.id, name="健康检查", module="health")
    session.add(feature)
    session.flush()
    enabled = _case(sample_project.id, feature.id, "[正常流] 旧健康检查", status="enabled")
    session.add(enabled)
    session.commit()
    session.execute(
        update(CaseModel)
        .where(CaseModel.id == enabled.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(days=90))
    )
    session.commit()

    default = collect_retirement_candidates(session, sample_project, stale_days=30)
    assert all(item["case_id"] != enabled.id for item in default)
    explicit = collect_retirement_candidates(
        session, sample_project, stale_days=30, include_enabled=True
    )
    assert any(item["case_id"] == enabled.id for item in explicit)


def test_retirement_dry_run_does_not_write(session, sample_project):
    feature = Feature(project_id=sample_project.id, name="健康检查", module="health")
    session.add(feature)
    session.flush()
    case = _case(sample_project.id, feature.id, "[正常流] 旧健康检查")
    session.add(case)
    session.commit()
    session.execute(
        update(CaseModel)
        .where(CaseModel.id == case.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(days=90))
    )
    session.commit()

    result = run_case_retirement(session, sample_project, dry_run=True, stale_days=30)
    session.commit()
    assert result["count"] == 1
    session.refresh(case)
    assert case.status == "pending"
    assert RETIRED_TAG not in (case.tags or [])
