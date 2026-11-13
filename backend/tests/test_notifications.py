"""邮件通知通道。"""
from __future__ import annotations

from app.models import WebhookDelivery
from app.push import deliver_webhook, queue_event


def test_email_channel_queues_and_delivers(session, sample_project, monkeypatch):
    sample_project.settings_json = {"notification_email": "qa@example.com"}
    session.commit()
    called: list[tuple] = []

    def fake_send(to, subject, text, **kwargs):
        called.append((to, subject, text))
        return {"sent": True}

    monkeypatch.setattr("app.notify.send_email", fake_send)
    delivery = queue_event(session, sample_project, "digest", {"count": 1})
    session.commit()
    assert delivery is not None
    assert delivery.url == "mailto:qa@example.com"
    assert session.query(WebhookDelivery).filter_by(project_id=sample_project.id).count() == 1

    result = deliver_webhook(session, delivery.id)
    assert result["status"] == "sent"
    assert called and called[0][0] == "qa@example.com"
    assert "digest" in called[0][1]


def test_webhook_and_email_fan_out(session, sample_project):
    sample_project.settings_json = {
        "webhook_url": "http://example.invalid/hook",
        "notification_email": "qa@example.com",
    }
    session.commit()
    queue_event(session, sample_project, "sla_breach", {"finding_id": 1})
    session.commit()
    rows = session.query(WebhookDelivery).filter_by(project_id=sample_project.id).all()
    assert {row.url for row in rows} == {"http://example.invalid/hook", "mailto:qa@example.com"}
