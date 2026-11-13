"""外部验证证据门禁：有效期、环境指纹与独立重放证据。

外部 Agent 只能提交报告，不能改验证状态：
- 自报通过只写 ``reported_passed``，不直接改 case / finding；
- 只有 Shakedown 独立重放/回归通过，才写 ``verified_fixed``；
- 人工复核如果引用外部证据，必须校验证据未过期、环境指纹一致、
  commit 与修复声明不矛盾；没有证据的人工复核视为显式 override。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseVerification, Finding, Project, SpecSnapshot, TestCase

EVIDENCE_TTL = timedelta(days=7)
PASSED = "passed_external"
FAILED = "failed_external"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def default_expiry(now: datetime | None = None) -> datetime:
    return (now or now_utc()) + EVIDENCE_TTL


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_env_fingerprint(session: Session, project: Project) -> dict[str, Any]:
    """当前项目可复核的环境指纹；外部证据只需携带其中一部分字段。"""
    snapshot = session.scalars(
        select(SpecSnapshot)
        .where(SpecSnapshot.project_id == project.id)
        .order_by(SpecSnapshot.id.desc())
        .limit(1)
    ).first()
    target_start = project.target_start if isinstance(project.target_start, dict) else {}
    isolation = project.isolation if isinstance(project.isolation, dict) else {}
    return {
        "project_id": project.id,
        "base_url": project.base_url or "",
        # 不落原始 command / env / DSN，避免把项目密钥带进验证记录。
        "target_type": str(target_start.get("type") or ""),
        "target_config_hash": _digest(target_start),
        "isolation_mode": str(isolation.get("mode") or ""),
        "isolation_hash": _digest(isolation),
        "spec_snapshot_id": snapshot.id if snapshot else None,
        "spec_hash": (snapshot.content_hash if snapshot else "") or "",
    }


def fingerprint_matches(stored: Any, current: dict[str, Any]) -> bool:
    """外部只上报自己关心的字段；已知字段必须一致，未知字段不参与判定。

    这样客户端可以多带 ``runner_version`` 之类的自描述字段，但至少要有一个
    Shakedown 能核对的字段（通常是 ``base_url`` / ``project_id``）。
    """
    if not isinstance(stored, dict) or not stored:
        return False
    known = [key for key in stored if key in current]
    if not known:
        return False
    return all(current[key] == stored[key] for key in known)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def validate_verification_evidence(
    session: Session,
    finding: Finding,
    verification_id: int,
) -> CaseVerification:
    """校验一条被引用的验证证据；不通过时抛 ValueError，调用方转 400。"""
    evidence = session.get(CaseVerification, verification_id)
    if evidence is None:
        raise ValueError(f"验证证据不存在：{verification_id}")
    if evidence.project_id != finding.project_id:
        raise ValueError("验证证据不属于当前项目")
    if finding.case_id and evidence.case_id != finding.case_id:
        raise ValueError("验证证据与当前缺陷关联的用例不一致")
    if evidence.status != PASSED:
        raise ValueError("只有 passed_external 证据可以用于通过验证")
    if evidence.expires_at is None:
        raise ValueError("验证证据没有有效期，不能用于通过验证")
    if _as_aware(evidence.expires_at) <= now_utc():
        raise ValueError("验证证据已过期，需要重新验证")

    project = session.get(Project, finding.project_id)
    if project is None:
        raise ValueError("缺陷所属项目不存在")
    if not fingerprint_matches(evidence.env_fingerprint, current_env_fingerprint(session, project)):
        raise ValueError("验证证据的环境指纹与当前项目不一致")

    claim = finding.fix_claim if isinstance(finding.fix_claim, dict) else {}
    claimed_commit = str(claim.get("commit_sha") or "")
    evidence_commit = str(evidence.commit_sha or "")
    if claimed_commit and evidence_commit and claimed_commit != evidence_commit:
        raise ValueError("验证证据的 commit 与修复声明不一致")
    return evidence


def record_replay_evidence(
    session: Session,
    case: TestCase,
    finding: Finding,
    *,
    passed: bool,
    result: str,
    command: str = "shakedown:replay",
) -> CaseVerification:
    """把 Shakedown 的独立重放结果写成可引用、可审计的证据记录。"""
    project = session.get(Project, case.project_id)
    claim = finding.fix_claim if isinstance(finding.fix_claim, dict) else {}
    evidence = CaseVerification(
        project_id=case.project_id,
        case_id=case.id,
        finding_id=finding.id,
        mode=case.verification_mode or "api",
        status=PASSED if passed else FAILED,
        actor="auto",
        source="auto",
        command=command,
        result=result,
        artifacts=[],
        note="Shakedown 独立重放",
        evidence_level="replayed",
        commit_sha=str(claim.get("commit_sha") or "")[:64],
        artifact_hashes={},
        env_fingerprint=current_env_fingerprint(session, project) if project else {},
        expires_at=default_expiry() if passed else None,
    )
    session.add(evidence)
    session.flush()
    # case 级验证状态只由 Shakedown 自己的重放结果写。
    case.verification_mode = evidence.mode
    case.verification_status = evidence.status
    case.verification = {
        "passed": passed,
        "mode": evidence.mode,
        "source": "shakedown",
        "evidence_level": "replayed",
        "at": now_utc().isoformat(),
        "verification_id": evidence.id,
        "result": result,
    }
    case.verified_at = now_utc() if passed else None
    case.verified_by = "shakedown" if passed else None
    return evidence
