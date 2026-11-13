"""Flaky 用例隔离。"""
from __future__ import annotations

from app.flaky import record_flaky_result
from app.models import AutomationException
from app.models import TestCase as CaseModel


def test_flaky_quarantine_after_threshold(session, sample_project):
    sample_project.settings_json = {"automation": {"flaky_quarantine": True, "flaky_threshold": 2}}
    case = CaseModel(
        project_id=sample_project.id,
        title="偶发失败",
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/api/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/health"}], "assertions": []},
        status="enabled",
        tags=["smoke"],
    )
    session.add(case)
    session.commit()

    first = record_flaky_result(session, sample_project, case, rerun_status="pass")
    session.commit()
    assert first["count"] == 1
    assert first["quarantined"] is False
    session.refresh(case)
    assert case.status == "enabled"
    assert "flaky" in case.tags

    second = record_flaky_result(session, sample_project, case, rerun_status="fail")
    session.commit()
    assert second["count"] == 2
    assert second["quarantined"] is True
    session.refresh(case)
    assert case.status == "needs_review"
    assert "flaky-quarantined" in case.tags
    assert case.spec["flaky_quarantine"]["count"] == 2
    assert session.query(AutomationException).filter_by(kind="flaky_case").count() == 1
