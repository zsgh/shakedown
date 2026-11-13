"""缺陷修复声明与验证。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Finding, FindingFix
from app.schemas import FindingFixCreate, FindingFixVerify
from app.verification_evidence import validate_verification_evidence

FIX_OPEN = "open"
FIX_IN_PROGRESS = "in_progress"
FIX_CLAIMED = "claimed_fixed"
FIX_VERIFIED = "verified_fixed"
FIX_REOPENED = "reopened"
FIX_NEEDS_HUMAN = "needs_human_verification"
FIX_NEEDS_EXTERNAL = "needs_external_verification"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim_payload(payload: FindingFixCreate, actor: str, source: str) -> dict[str, Any]:
    return {
        "commit_sha": payload.commit_sha,
        "pr_url": payload.pr_url,
        "files": payload.files,
        "note": payload.note,
        "test_command": payload.test_command,
        "test_result": payload.test_result,
        "actor": actor,
        "source": source,
        "claimed_at": _now().isoformat(),
    }


def claim_work(
    session: Session,
    finding: Finding,
    *,
    actor: str,
    note: str = "",
) -> FindingFix:
    """认领缺陷修复工作，避免多个编码 Agent 重复处理同一条。"""
    if finding.fix_status == FIX_VERIFIED:
        raise ValueError("该缺陷已验证修复，无需认领")
    if finding.fix_status == FIX_IN_PROGRESS and finding.assignee and finding.assignee != actor:
        raise ValueError(f"该缺陷已被 {finding.assignee} 认领")
    finding.fix_status = FIX_IN_PROGRESS
    finding.fix_verified_at = None
    finding.assignee = actor[:120]
    claim = dict(finding.fix_claim or {})
    claim["work"] = {
        "actor": actor,
        "claimed_at": _now().isoformat(),
        "note": note,
    }
    finding.fix_claim = claim
    record = FindingFix(
        project_id=finding.project_id,
        finding_id=finding.id,
        status=FIX_IN_PROGRESS,
        actor=actor,
        source="mcp",
        note=note,
    )
    session.add(record)
    session.flush()
    return record


def claim_fix(
    session: Session,
    finding: Finding,
    payload: FindingFixCreate,
    *,
    actor: str = "manual",
    source: str = "manual",
) -> FindingFix:
    if not any([payload.commit_sha, payload.pr_url, payload.test_result, payload.note]):
        raise ValueError("提交修复至少需要 commit、PR、测试结果或修复说明之一")
    claim = _claim_payload(payload, actor, source)
    finding.fix_status = FIX_CLAIMED
    finding.fix_verified_at = None
    finding.fix_claim = claim
    record = FindingFix(
        project_id=finding.project_id,
        finding_id=finding.id,
        status=FIX_CLAIMED,
        actor=actor,
        source=source,
        commit_sha=payload.commit_sha[:64],
        pr_url=payload.pr_url,
        files=payload.files,
        note=payload.note,
        test_command=payload.test_command,
        test_result=payload.test_result,
    )
    session.add(record)
    session.flush()
    return record


def verify_fix(
    session: Session,
    finding: Finding,
    payload: FindingFixVerify,
    *,
    actor: str = "manual",
    source: str = "manual",
) -> FindingFix:
    if finding.fix_status not in {FIX_CLAIMED, FIX_REOPENED, FIX_NEEDS_HUMAN, FIX_NEEDS_EXTERNAL}:
        raise ValueError("当前缺陷没有待验证的修复声明")

    evidence = None
    manual_override = False
    if payload.verified:
        if payload.verification_id is not None:
            evidence = validate_verification_evidence(session, finding, payload.verification_id)
        elif source in {"mcp", "external", "auto"}:
            raise ValueError(
                "外部/自动验证通过必须引用 verification_id；"
                "请先通过 report_case_verification 提交证据，或等待 Shakedown 独立重放。"
            )
        else:
            # 人工可以显式 override，但会在修复历史里留下标记。
            manual_override = True
        if evidence is not None and source in {"mcp", "external", "auto"} and evidence.evidence_level != "replayed":
            raise ValueError("外部/自动验证通过只能引用 Shakedown 独立重放证据")

    status = FIX_VERIFIED if payload.verified else FIX_REOPENED
    finding.fix_status = status
    finding.fix_verified_at = _now() if payload.verified else None
    evidence_level = evidence.evidence_level if evidence else ("manual" if manual_override else "self_reported")
    claim = dict(finding.fix_claim or {})
    claim["verification"] = {
        "verified": payload.verified,
        "note": payload.note,
        "actor": actor,
        "source": source,
        "evidence_level": evidence_level,
        "manual_override": manual_override,
        "verified_at": finding.fix_verified_at.isoformat() if finding.fix_verified_at else None,
        "verification_id": payload.verification_id,
    }
    finding.fix_claim = claim
    record = FindingFix(
        project_id=finding.project_id,
        finding_id=finding.id,
        status=status,
        actor=actor,
        source=source,
        note=payload.note,
        test_command=payload.test_command,
        test_result=payload.test_result,
        verification_id=payload.verification_id,
    )
    session.add(record)
    session.flush()
    return record


def mark_needs_human(
    session: Session,
    finding: Finding,
    *,
    note: str,
    actor: str = "auto",
    source: str = "auto",
    verification_id: int | None = None,
) -> FindingFix:
    finding.fix_status = FIX_NEEDS_HUMAN
    finding.fix_verified_at = None
    claim = dict(finding.fix_claim or {})
    claim["verification"] = {
        "verified": False,
        "needs_human": True,
        "note": note,
        "actor": actor,
        "source": source,
        "verification_id": verification_id,
        "verified_at": None,
    }
    finding.fix_claim = claim
    record = FindingFix(
        project_id=finding.project_id,
        finding_id=finding.id,
        status=FIX_NEEDS_HUMAN,
        actor=actor,
        source=source,
        note=note,
        verification_id=verification_id,
    )
    session.add(record)
    session.flush()
    return record


def mark_needs_external(
    session: Session,
    finding: Finding,
    *,
    note: str,
    mode: str,
    actor: str = "auto",
    source: str = "auto",
) -> FindingFix:
    finding.fix_status = FIX_NEEDS_EXTERNAL
    finding.fix_verified_at = None
    claim = dict(finding.fix_claim or {})
    claim["verification"] = {
        "verified": False,
        "needs_external": True,
        "mode": mode,
        "note": note,
        "actor": actor,
        "source": source,
        "verified_at": None,
    }
    finding.fix_claim = claim
    record = FindingFix(
        project_id=finding.project_id,
        finding_id=finding.id,
        status=FIX_NEEDS_EXTERNAL,
        actor=actor,
        source=source,
        note=note,
    )
    session.add(record)
    session.flush()
    return record


def fix_history(session: Session, finding_id: int) -> list[FindingFix]:
    return list(
        session.scalars(
            select(FindingFix)
            .where(FindingFix.finding_id == finding_id)
            .order_by(FindingFix.created_at.desc(), FindingFix.id.desc())
        ).all()
    )
