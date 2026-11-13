"""自动分诊策略、例外队列与 SLA 测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models import AutomationException, Finding
from app.triage_policy import apply_triage_policy


def _finding(
    project_id: int,
    *,
    severity="high",
    verdict="real_bug",
    confidence=0.95,
    classification: str | None = None,
) -> Finding:
    return Finding(
        project_id=project_id,
        source="whitebox",
        title="示例缺陷",
        description="",
        severity=severity,
        status="pending",
        module="api",
        risk_type="auth",
        evidence=[{"type": "code", "ref": "api.py:1"}],
        code_location={},
        triage_status="ai_triaged",
        triage={
            "verdict": verdict,
            "confidence": confidence,
            "recommended_action": "fix_code",
            "reason": "测试",
            **({"classification": classification} if classification else {}),
        },
    )


def test_high_confidence_triage_auto_confirms(session, sample_project):
    sample_project.settings_json = {
        "triage_policy": {"auto_confirm": True, "min_confidence": 0.9}
    }
    finding = _finding(sample_project.id)
    session.add_all([sample_project, finding])
    session.commit()

    result = apply_triage_policy(session, sample_project)
    session.commit()
    session.refresh(finding)
    assert result["confirmed"] == 1
    assert finding.status == "confirmed"
    assert finding.confirmed_by == "auto"
    assert finding.triage["auto_confirmed"] is True


def test_low_confidence_triage_creates_exception(session, sample_project):
    sample_project.settings_json = {
        "triage_policy": {"auto_confirm": True, "min_confidence": 0.9}
    }
    finding = _finding(sample_project.id, verdict="needs_verification", confidence=0.4)
    session.add_all([sample_project, finding])
    session.commit()

    result = apply_triage_policy(session, sample_project)
    session.commit()
    assert result["exceptions"] == 1
    row = session.query(AutomationException).filter_by(target_id=finding.id).one()
    assert row.kind == "triage_low_confidence"
    assert row.status == "open"


def test_exception_api_list_and_ack(client, session, sample_project):
    finding = _finding(sample_project.id, verdict="needs_verification", confidence=0.2)
    session.add_all([sample_project, finding])
    session.commit()
    sample_project.settings_json = {"triage_policy": {"auto_confirm": True, "min_confidence": 0.9}}
    apply_triage_policy(session, sample_project)
    session.commit()

    rows = client.get(f"/api/projects/{sample_project.id}/exceptions").json()
    assert len(rows) == 1
    response = client.patch(
        f"/api/exceptions/{rows[0]['id']}",
        json={"status": "acked", "owner": "qa", "note": "复核中"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acked"
    assert response.json()["owner"] == "qa"


def test_sla_breaches(client, session, sample_project):
    finding = _finding(sample_project.id, severity="critical")
    session.add_all([sample_project, finding])
    session.commit()
    session.execute(
        update(Finding)
        .where(Finding.id == finding.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=10))
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/sla/breaches").json()
    assert body["count"] == 1
    assert body["items"][0]["finding_id"] == finding.id
    assert body["items"][0]["overdue_minutes"] > 0


def test_high_confidence_environment_goes_to_environment_exception(session, sample_project):
    sample_project.settings_json = {
        "triage_policy": {"auto_confirm": True, "min_confidence": 0.9}
    }
    finding = _finding(sample_project.id, classification="environment")
    session.add_all([sample_project, finding])
    session.commit()

    result = apply_triage_policy(session, sample_project)
    session.commit()
    session.refresh(finding)
    assert result["confirmed"] == 0
    assert result["environment"] == 1
    assert finding.status == "pending"
    assert finding.triage_status == "needs_review"
    row = session.query(AutomationException).filter_by(target_id=finding.id).one()
    assert row.kind == "environment_issue"


def test_high_confidence_test_issue_does_not_auto_confirm(session, sample_project):
    sample_project.settings_json = {
        "triage_policy": {"auto_confirm": True, "min_confidence": 0.9}
    }
    finding = _finding(sample_project.id, classification="test_issue")
    session.add_all([sample_project, finding])
    session.commit()

    result = apply_triage_policy(session, sample_project)
    session.commit()
    session.refresh(finding)
    assert result["test_issue"] == 1
    assert finding.status == "pending"
    row = session.query(AutomationException).filter_by(target_id=finding.id).one()
    assert row.kind == "test_issue"


def test_classification_can_block_auto_confirm(session, sample_project):
    sample_project.settings_json = {
        "triage_policy": {
            "auto_confirm": True,
            "min_confidence": 0.9,
            "classifications": ["product_defect"],
        }
    }
    finding = _finding(sample_project.id, classification="environment")
    session.add_all([sample_project, finding])
    session.commit()

    result = apply_triage_policy(session, sample_project)
    session.commit()
    assert result["confirmed"] == 0
    assert result["exceptions"] == 1
