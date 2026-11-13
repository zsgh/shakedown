"""定时 digest / 周报。"""
from __future__ import annotations

from app.digest import build_scheduled_digest, queue_scheduled_digest
from app.models import WebhookDelivery


def test_scheduled_digest_is_deduped_by_interval(session, sample_project):
    sample_project.settings_json = {
        "automation": {"digest_enabled": True, "digest_interval_seconds": 86400},
        "webhook_url": "http://example.invalid/hook",
    }
    session.commit()

    first = queue_scheduled_digest(session, sample_project)
    session.commit()
    assert first["queued"] is True
    assert first["event"] == "digest"
    delivery = session.query(WebhookDelivery).one()
    assert delivery.event == "digest"
    assert delivery.payload["data"]["period"] == "daily"

    second = queue_scheduled_digest(session, sample_project)
    assert second["queued"] is False
    assert session.query(WebhookDelivery).count() == 1

    digest = build_scheduled_digest(session, sample_project)
    assert "runs_7d" in digest
    assert "sla_breaches" in digest


def test_weekly_digest_event(session, sample_project):
    sample_project.settings_json = {
        "automation": {"digest_enabled": True, "digest_interval_seconds": 7 * 86400},
        "webhook_url": "http://example.invalid/hook",
    }
    session.commit()
    result = queue_scheduled_digest(session, sample_project)
    session.commit()
    assert result["queued"] is True
    assert result["event"] == "weekly_digest"


def test_digest_supports_email_only_target(session, sample_project):
    """只配置 notification_email 时，digest 也必须能入队（mailto 投递）。"""
    sample_project.settings_json = {
        "automation": {"digest_enabled": True, "digest_interval_seconds": 86400},
        "notification_email": "qa@example.com",
    }
    session.commit()

    result = queue_scheduled_digest(session, sample_project)
    session.commit()
    assert result["queued"] is True
    delivery = (
        session.query(WebhookDelivery)
        .filter_by(project_id=sample_project.id, event="digest")
        .one()
    )
    assert delivery.url == "mailto:qa@example.com"


def test_manual_digest_supports_email_only_target(client, session, sample_project):
    sample_project.settings_json = {"notification_email": "qa@example.com"}
    session.commit()

    body = client.post(f"/api/projects/{sample_project.id}/push/digest").json()
    assert body["status"] == "queued"
    delivery = (
        session.query(WebhookDelivery)
        .filter_by(project_id=sample_project.id, event="digest")
        .one()
    )
    assert delivery.url == "mailto:qa@example.com"

    listed = client.get(f"/api/projects/{sample_project.id}/webhook-deliveries").json()
    assert listed["email_configured"] is True
    assert listed["notification_configured"] is True


def test_digest_still_requires_a_target(session, sample_project):
    sample_project.settings_json = {"automation": {"digest_enabled": True}}
    session.commit()
    result = queue_scheduled_digest(session, sample_project)
    assert result["queued"] is False
    assert "notification_email" in result["reason"]
