"""用例的 API / 进程 / 构建产物 / 浏览器 / 权限验证。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseVerification, Finding, TestCase
from app.schemas import CaseVerificationCreate
from app.verification_evidence import default_expiry
VERIFICATION_MODES = {"api", "process", "artifact", "frontend", "privileged", "manual"}
PROCESS_WORDS = re.compile(r"Serve|监听|退出|信号|goroutine|errCh|关停|监控|守护")


def infer_verification_mode(file_path: str, title: str, case_type: str = "api") -> str:
    path = (file_path or "").replace("\\", "/")
    if case_type == "web" or path.startswith("web/"):
        return "frontend"
    if path.startswith("tools/"):
        return "artifact"
    if path.startswith("scripts/") or path.startswith("internal/privileged/"):
        return "privileged"
    if path.startswith("cmd/"):
        return "process"
    if path.startswith("internal/server/") and PROCESS_WORDS.search(title or ""):
        return "process"
    return "api"


def record_case_verification(
    session: Session,
    case: TestCase,
    payload: CaseVerificationCreate,
    *,
    actor: str = "manual",
    source: str = "manual",
) -> CaseVerification:
    mode = payload.mode or case.verification_mode or "api"
    if mode not in VERIFICATION_MODES:
        raise ValueError(f"不支持的验证方式：{mode}")
    # 开发 Agent 只能报告，不写 case / finding 的验证状态。
    # Shakedown 自己的执行结果才写 verified_fixed。
    status = "reported_passed" if payload.passed else "reported_failed"
    now = datetime.now(timezone.utc)
    evidence_level = "self_reported"
    # 没显式给有效期时给自报证据一个默认窗口，避免证据永久有效。
    expires_at = payload.expires_at
    if payload.passed and expires_at is None:
        expires_at = default_expiry(now)

    findings = list(session.scalars(select(Finding).where(Finding.case_id == case.id)).all())
    if not payload.passed and not findings:
        # 外部验证失败但还没有关联缺陷：生成一条待分诊线索，不能只留一条验证记录。
        finding = Finding(
            project_id=case.project_id,
            source="external",
            title=f"[外部验证失败] {case.title}"[:400],
            description=payload.note or f"{mode} 外部验证失败",
            severity="medium",
            evidence=[],
            case_id=case.id,
            module="",
            risk_type="external_verification",
            repro=payload.command or "",
            status="pending",
            triage_status="untriaged",
        )
        session.add(finding)
        session.flush()
        findings = [finding]

    finding_id = findings[0].id if findings else None
    record = CaseVerification(
        project_id=case.project_id,
        case_id=case.id,
        finding_id=finding_id,
        mode=mode,
        status=status,
        actor=actor,
        source=source,
        command=payload.command,
        result=payload.result,
        artifacts=payload.artifacts,
        note=payload.note,
        evidence_level=evidence_level,
        commit_sha=payload.commit_sha,
        artifact_hashes=payload.artifact_hashes,
        env_fingerprint=payload.env_fingerprint,
        expires_at=expires_at,
    )
    session.add(record)
    session.flush()
    # 报告单独挂在 verification.reported 下，不覆盖 Shakedown 自己的验证状态。
    case.verification = {
        **(case.verification or {}),
        "reported": {
            "passed": payload.passed,
            "mode": mode,
            "command": payload.command,
            "result": payload.result,
            "artifacts": payload.artifacts,
            "note": payload.note,
            "actor": actor,
            "source": source,
            "at": now.isoformat(),
            "evidence_level": evidence_level,
            "verification_id": record.id,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    }

    evidence_item = {
        "type": "external_verification",
        "mode": mode,
        "status": status,
        "command": payload.command,
        "result": payload.result,
        "note": payload.note,
        "case_id": case.id,
        "evidence_level": evidence_level,
        "verification_id": record.id,
        "commit_sha": payload.commit_sha,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    for finding in findings:
        evidence = list(finding.evidence or [])
        evidence.append(evidence_item)
        finding.evidence = evidence[-20:]
    return record


def case_verification_history(session: Session, case_id: int) -> list[CaseVerification]:
    return list(
        session.scalars(
            select(CaseVerification)
            .where(CaseVerification.case_id == case_id)
            .order_by(CaseVerification.created_at.desc(), CaseVerification.id.desc())
        ).all()
    )
