"""分诊后的自动决策：高置信度自动确认，低置信度进入例外队列。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.triage import classify_triage
from app.exceptions import record_exception
from app.models import Finding, Project


def apply_triage_policy(session: Session, project: Project) -> dict[str, int]:
    settings = project.settings_json or {}
    policy = settings.get("triage_policy") or {}
    if not policy.get("auto_confirm", False):
        return {
            "confirmed": 0,
            "exceptions": 0,
            "test_issue": 0,
            "environment": 0,
            "low_confidence": 0,
        }

    min_confidence = float(policy.get("min_confidence", 0.9))
    allowed_verdicts = set(policy.get("verdicts") or ["real_bug"])
    allowed_actions = set(policy.get("actions") or ["fix_code"])
    allowed_classes = set(policy.get("classifications") or ["product_defect"])
    findings = session.scalars(
        select(Finding).where(
            Finding.project_id == project.id,
            Finding.status == "pending",
            Finding.triage_status == "ai_triaged",
        )
    ).all()
    confirmed = 0
    exceptions = 0
    test_issue = 0
    environment = 0
    low_confidence = 0
    now = datetime.now(timezone.utc)
    for finding in findings:
        triage = finding.triage if isinstance(finding.triage, dict) else {}
        verdict = str(triage.get("verdict") or "")
        action = str(triage.get("recommended_action") or "")
        confidence = float(triage.get("confidence") or 0.0)
        classification = classify_triage(
            verdict=verdict,
            action=action,
            risk_type=str(finding.risk_type or ""),
            reason=str(triage.get("reason") or ""),
            classification=str(triage.get("classification") or ""),
        )
        if triage.get("classification") != classification:
            finding.triage = {**triage, "classification": classification}

        # 低置信度 / 需要验证：只进例外，不自动确认
        if confidence < min_confidence or verdict in {"needs_verification", ""}:
            low_confidence += 1
            exceptions += 1
            record_exception(
                session,
                project,
                kind="triage_low_confidence",
                target_type="finding",
                target_id=finding.id,
                severity=str(finding.severity or "medium"),
                note=f"分诊结论 {verdict or 'unknown'} / 置信度 {confidence:.2f}，需要人工复核",
                payload={
                    "verdict": verdict,
                    "action": action,
                    "confidence": confidence,
                    "classification": classification,
                    "reason": triage.get("reason") or "",
                },
            )
            continue

        if classification == "environment":
            environment += 1
            exceptions += 1
            record_exception(
                session,
                project,
                kind="environment_issue",
                target_type="finding",
                target_id=finding.id,
                severity=str(finding.severity or "high"),
                note=f"分诊判定为环境问题：{triage.get('reason') or '需要检查被测环境'}",
                payload={"verdict": verdict, "action": action, "confidence": confidence, "classification": classification},
            )
            finding.triage_status = "needs_review"
            finding.triage = {**triage, "classification": classification}
            continue

        if classification == "test_issue":
            test_issue += 1
            exceptions += 1
            record_exception(
                session,
                project,
                kind="test_issue",
                target_type="finding",
                target_id=finding.id,
                severity=str(finding.severity or "medium"),
                note=f"分诊判定为测试侧问题：{triage.get('reason') or '需要复核用例/断言'}",
                payload={"verdict": verdict, "action": action, "confidence": confidence, "classification": classification},
            )
            finding.triage_status = "needs_review"
            finding.triage = {**triage, "classification": classification}
            continue

        if (
            classification in allowed_classes
            and verdict in allowed_verdicts
            and action in allowed_actions
        ):
            finding.status = "confirmed"
            finding.confirmed_by = "auto"
            finding.confirmed_at = now
            finding.triage_status = "auto_confirmed"
            finding.triage = {**triage, "classification": classification, "auto_confirmed": True}
            confirmed += 1
            continue

        # 高置信度但分类不在允许自动确认范围：保守进例外
        exceptions += 1
        record_exception(
            session,
            project,
            kind="triage_policy_exception",
            target_type="finding",
            target_id=finding.id,
            severity=str(finding.severity or "medium"),
            note=f"分诊分类 {classification} 不在自动确认范围，需要人工处理",
            payload={"verdict": verdict, "action": action, "confidence": confidence, "classification": classification},
        )
    session.flush()
    return {
        "confirmed": confirmed,
        "exceptions": exceptions,
        "test_issue": test_issue,
        "environment": environment,
        "low_confidence": low_confidence,
    }
