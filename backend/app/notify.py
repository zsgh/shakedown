"""通知通道：邮件（SMTP）。Webhook / Slack 由 push.deliver_webhook 处理。"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from app.config import Settings, get_settings


def send_email(
    to: str,
    subject: str,
    text: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cfg = settings or get_settings()
    if not cfg.smtp_host or not cfg.smtp_from:
        raise ValueError("SMTP 未配置（smtp_host / smtp_from）")
    message = EmailMessage()
    message["From"] = cfg.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(cfg.smtp_host, int(cfg.smtp_port or 25), timeout=10) as client:
        if cfg.smtp_tls:
            client.starttls()
        if cfg.smtp_user:
            client.login(cfg.smtp_user, cfg.smtp_password)
        client.send_message(message)
    return {"to": to, "subject": subject, "sent": True}
