"""项目级主动推送：webhook 事件 + 定期 digest。

约束：
- 只推送 Shakedown 自己产出的数据，不写回被测系统；
- 投递失败进入重试，不阻塞执行 / 分析主流程。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import TaskKind
from app.gaps import collect_case_gaps, gap_facets, gap_summary
from app.models import Finding, Project, WebhookDelivery
from app.worker.queue import enqueue

MAX_WEBHOOK_ATTEMPTS = 5


def webhook_url(project: Project) -> str:
    settings = project.settings_json or {}
    url = str(settings.get("webhook_url") or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def notification_email(project: Project) -> str:
    settings = project.settings_json or {}
    return str(settings.get("notification_email") or "").strip()


def notification_targets(project: Project) -> list[str]:
    """项目配置的通知目标：webhook + 邮件。两者独立，任一可用即可投递。"""
    targets: list[str] = []
    if url := webhook_url(project):
        targets.append(url)
    if email := notification_email(project):
        targets.append(f"mailto:{email}")
    return targets


def has_notification_target(project: Project) -> bool:
    return bool(notification_targets(project))


def queue_event(
    db: Session,
    project: Project,
    event: str,
    data: dict[str, Any],
) -> WebhookDelivery | None:
    """写入一条投递记录并排队；项目未配置 webhook / 邮件时直接跳过。"""
    targets = notification_targets(project)
    if not targets:
        return None
    payload = {
        "event": event,
        "project_id": project.id,
        "project_name": project.name,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    first: WebhookDelivery | None = None
    for target in dict.fromkeys(targets):
        delivery = WebhookDelivery(
            project_id=project.id,
            event=event,
            url=target,
            payload=payload,
            status="pending",
        )
        db.add(delivery)
        db.flush()
        enqueue(
            db,
            TaskKind.DELIVER_WEBHOOK.value,
            {"delivery_id": delivery.id},
            project_id=project.id,
            priority=200,
            max_attempts=MAX_WEBHOOK_ATTEMPTS,
        )
        first = first or delivery
    return first


def deliver_webhook(db: Session, delivery_id: int) -> dict[str, Any]:
    """实际投递；失败时抛出异常，由任务队列负责重试。"""
    import httpx

    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        raise ValueError(f"投递记录不存在：{delivery_id}")
    if delivery.status == "sent":
        return {"delivery_id": delivery.id, "status": "sent", "skipped": True}
    delivery.attempts += 1
    try:
        if delivery.url.startswith("mailto:"):
            from app.notify import send_email

            send_email(
                delivery.url[len("mailto:") :],
                f"[Shakedown] {delivery.payload.get('project_name', '')} · {delivery.event}",
                json.dumps(delivery.payload, ensure_ascii=False, indent=2),
            )
        else:
            response = httpx.post(
                delivery.url,
                json=delivery.payload,
                timeout=5.0,
                headers={"X-Shakedown-Event": delivery.event},
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - 投递失败要原样记录并重试
        delivery.last_error = str(exc)[:2000]
        delivery.status = "failed" if delivery.attempts >= MAX_WEBHOOK_ATTEMPTS else "pending"
        db.commit()
        raise
    delivery.status = "sent"
    delivery.last_error = ""
    delivery.delivered_at = datetime.now(timezone.utc)
    db.commit()
    return {"delivery_id": delivery.id, "status": "sent", "attempts": delivery.attempts}


def build_digest(db: Session, project: Project) -> dict[str, Any]:
    """生成 actionable finding + 契约缺口摘要。"""
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.status == "confirmed",
        )
    ).all()
    actionable = [finding for finding in findings if finding.fix_status != "verified_fixed"]
    items = collect_case_gaps(db, project)
    summary = gap_summary(items)
    facets = gap_facets(items)
    sut_contract = [item for item in items if item.get("owner_kind") == "sut-contract"]
    return {
        "actionable_findings": len(actionable),
        "actionable_finding_ids": [finding.id for finding in actionable][:200],
        "verified_fixed": sum(1 for finding in findings if finding.fix_status == "verified_fixed"),
        "case_gaps": {
            "total": summary["cases"],
            "pending_assertions": summary["pending_assertions"],
            "sut_contract_cases": len(sut_contract),
            "by_owner_kind": facets["by_owner_kind"],
            "by_missing_input_kind": facets["by_missing_input_kind"],
        },
    }


def queue_digest(db: Session, project: Project) -> WebhookDelivery | None:
    return queue_event(db, project, "digest", build_digest(db, project))
