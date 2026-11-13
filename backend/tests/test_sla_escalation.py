"""SLA 定时升级：例外去重 + 首次通知。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.escalation import run_sla_escalation
from app.models import AutomationException, Finding, WebhookDelivery


def _finding(project_id: int, *, severity: str = "critical") -> Finding:
    return Finding(
        project_id=project_id,
        title="支付回调偶发 500",
        description="",
        severity=severity,
        status="confirmed",
        fix_status="open",
        assignee="alice",
        created_at=datetime.now(timezone.utc) - timedelta(hours=8),
    )


def test_sla_escalation_is_opt_in_and_does_not_touch_finding(session, sample_project):
    sample_project.settings_json = {"automation": {"sla_escalation_enabled": True}}
    finding = _finding(sample_project.id)
    session.add(finding)
    session.commit()

    preview = run_sla_escalation(session, sample_project, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["count"] == 1
    assert preview["created"] == 1
    assert session.query(AutomationException).filter_by(project_id=sample_project.id).count() == 0
    assert session.query(WebhookDelivery).filter_by(project_id=sample_project.id).count() == 0

    applied = run_sla_escalation(session, sample_project, dry_run=False)
    session.commit()
    assert applied["created"] == 1
    assert session.query(AutomationException).filter_by(project_id=sample_project.id, kind="sla_breach").count() == 1
    assert session.query(WebhookDelivery).filter_by(project_id=sample_project.id).count() == 0  # 未配置 webhook 时不产生投递

    again = run_sla_escalation(session, sample_project, dry_run=False)
    session.commit()
    assert again["created"] == 0
    assert session.query(AutomationException).filter_by(project_id=sample_project.id, kind="sla_breach").count() == 1

    session.refresh(finding)
    assert finding.status == "confirmed"
    assert finding.assignee == "alice"
    assert finding.fix_status == "open"


def test_sla_escalation_notifies_once_when_webhook_configured(session, sample_project, client):
    sample_project.settings_json = {
        "automation": {"sla_escalation_enabled": True},
        "webhook_url": "http://example.invalid/hook",
    }
    session.add(_finding(sample_project.id, severity="critical"))
    session.commit()

    first = client.post(f"/api/projects/{sample_project.id}/sla/escalate?dry_run=false").json()
    assert first["created"] == 1
    deliveries = session.query(WebhookDelivery).filter_by(project_id=sample_project.id, event="sla_breach").all()
    assert len(deliveries) == 1

    second = client.post(f"/api/projects/{sample_project.id}/sla/escalate?dry_run=false").json()
    assert second["created"] == 0
    assert session.query(WebhookDelivery).filter_by(project_id=sample_project.id, event="sla_breach").count() == 1

    listed = client.get(f"/api/projects/{sample_project.id}/sla/escalations").json()
    assert listed["count"] == 1
    assert listed["items"][0]["finding_id"] is not None


def test_sla_escalation_respects_min_severity(session, sample_project):
    sample_project.settings_json = {
        "automation": {
            "sla_escalation_enabled": True,
            "sla_escalation_min_severity": "critical",
            "sla_escalation_notify": False,
        }
    }
    session.add(_finding(sample_project.id, severity="medium"))
    session.commit()

    result = run_sla_escalation(session, sample_project, dry_run=True)
    assert result["count"] == 0


def test_apply_is_noop_when_sla_escalation_disabled(session, sample_project):
    """手工调用 apply 不能绕过项目 opt-in。"""
    sample_project.settings_json = {"automation": {}}
    session.add(_finding(sample_project.id, severity="critical"))
    session.commit()

    preview = run_sla_escalation(session, sample_project, dry_run=True)
    assert preview["count"] == 1
    assert preview["created"] == 1

    blocked = run_sla_escalation(session, sample_project, dry_run=False)
    session.commit()
    assert blocked["created"] == 0
    assert blocked["enabled"] is False
    assert "sla_escalation_enabled" in blocked["reason"]
    assert session.query(AutomationException).filter_by(project_id=sample_project.id).count() == 0

    forced = run_sla_escalation(session, sample_project, dry_run=False, force=True)
    session.commit()
    assert forced["created"] == 1
    assert session.query(AutomationException).filter_by(project_id=sample_project.id, kind="sla_breach").count() == 1


def test_sla_escalate_api_requires_opt_in_or_force(client, session, sample_project):
    sample_project.settings_json = {"automation": {}}
    session.add(_finding(sample_project.id, severity="critical"))
    session.commit()

    blocked = client.post(f"/api/projects/{sample_project.id}/sla/escalate?dry_run=false").json()
    assert blocked["created"] == 0
    assert session.query(AutomationException).filter_by(project_id=sample_project.id).count() == 0

    forced = client.post(f"/api/projects/{sample_project.id}/sla/escalate?dry_run=false&force=true").json()
    assert forced["created"] == 1
