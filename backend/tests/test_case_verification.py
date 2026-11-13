"""用例外部验证与自动验证路由。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import CaseResult, CaseVerification, Finding, Project, Run
from app.models import TestCase as CaseModel
from app.verification_evidence import current_env_fingerprint


def _case_with_finding(session, project_id, mode="artifact"):
    case = CaseModel(
        project_id=project_id,
        title="构建产物验证",
        dimension="functional",
        case_type="api",
        spec={"title": "构建产物验证", "steps": [], "assertions": []},
        status="pending",
        verification_mode=mode,
    )
    session.add(case)
    session.flush()
    finding = Finding(
        project_id=project_id,
        source="whitebox",
        title="构建产物缺陷",
        description="",
        severity="high",
        status="confirmed",
        module="tools",
        risk_type="artifact",
        evidence=[],
        code_location={"file_path": "tools/genicon/main.go"},
        case_id=case.id,
        fix_status="claimed_fixed",
        fix_claim={"commit_sha": "abc"},
    )
    session.add(finding)
    session.commit()
    return case, finding


def _evidence(session, project_id, case, finding, **overrides):
    project = session.get(Project, project_id)
    now = datetime.now(timezone.utc)
    data = {
        "project_id": project_id,
        "case_id": case.id,
        "finding_id": finding.id,
        "mode": "artifact",
        "status": "passed_external",
        "actor": "mcp:dev-agent",
        "source": "mcp",
        "command": "verify",
        "result": "ok",
        "artifacts": [],
        "note": "",
        "evidence_level": "self_reported",
        "commit_sha": "abc",
        "artifact_hashes": {},
        "env_fingerprint": current_env_fingerprint(session, project),
        "expires_at": now + timedelta(days=1),
    }
    data.update(overrides)
    evidence = CaseVerification(**data)
    session.add(evidence)
    session.commit()
    session.refresh(evidence)
    return evidence


def test_external_case_verification_records_evidence_without_verifying(client, session, sample_project):
    """开发 Agent 只能提交报告；case / finding 的验证状态都不能由它改。"""
    case, finding = _case_with_finding(session, sample_project.id)

    response = client.post(
        f"/api/cases/{case.id}/verification",
        json={
            "passed": True,
            "mode": "artifact",
            "command": "go run ./tools/genicon && identify apple-touch-icon.png",
            "result": "180x180，内容与整图缩放一致",
            "artifacts": ["apple-touch-icon.png"],
            "note": "构建产物检查通过",
            "actor": "mcp:dev-agent",
            "commit_sha": "deadbeef",
            "artifact_hashes": {"apple-touch-icon.png": "sha256:abc"},
            "env_fingerprint": {"base_url": "https://example.test"},
            "expires_at": "2026-12-31T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["case"]["verification_status"] == "unverified"
    assert body["case"]["verification"]["reported"]["passed"] is True
    assert body["verification"]["status"] == "reported_passed"
    assert body["verification"]["mode"] == "artifact"
    assert body["verification"]["evidence_level"] == "self_reported"
    assert body["verification"]["commit_sha"] == "deadbeef"
    assert body["verification"]["artifact_hashes"] == {"apple-touch-icon.png": "sha256:abc"}
    assert body["verification"]["env_fingerprint"] == {"base_url": "https://example.test"}
    assert body["verification"]["expires_at"].startswith("2026-12-31")

    session.refresh(finding)
    assert finding.fix_status == "claimed_fixed"
    assert finding.evidence[-1]["status"] == "reported_passed"
    assert finding.evidence[-1]["evidence_level"] == "self_reported"
    assert finding.evidence[-1]["verification_id"] == body["verification"]["id"]
    history = client.get(f"/api/findings/{finding.id}/fix-history").json()
    assert history == []


def test_passed_evidence_gets_default_expiry(client, session, sample_project):
    case, _ = _case_with_finding(session, sample_project.id)
    response = client.post(
        f"/api/cases/{case.id}/verification",
        json={"passed": True, "mode": "artifact", "result": "ok"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["verification"]["expires_at"]


def test_expired_evidence_cannot_verify_finding(client, session, sample_project):
    case, finding = _case_with_finding(session, sample_project.id)
    evidence = _evidence(
        session,
        sample_project.id,
        case,
        finding,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    response = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": True, "verification_id": evidence.id},
    )
    assert response.status_code == 400
    assert "过期" in response.text
    session.refresh(finding)
    assert finding.fix_status == "claimed_fixed"


def test_fingerprint_mismatch_cannot_verify_finding(client, session, sample_project):
    case, finding = _case_with_finding(session, sample_project.id)
    evidence = _evidence(
        session,
        sample_project.id,
        case,
        finding,
        env_fingerprint={"base_url": "http://another-target.test"},
    )

    response = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": True, "verification_id": evidence.id},
    )
    assert response.status_code == 400
    assert "环境指纹" in response.text
    session.refresh(finding)
    assert finding.fix_status == "claimed_fixed"


def test_commit_mismatch_cannot_verify_finding(client, session, sample_project):
    case, finding = _case_with_finding(session, sample_project.id)
    evidence = _evidence(session, sample_project.id, case, finding, commit_sha="different-commit")

    response = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": True, "verification_id": evidence.id},
    )
    assert response.status_code == 400
    assert "commit" in response.text
    session.refresh(finding)
    assert finding.fix_status == "claimed_fixed"


def test_valid_evidence_can_verify_finding(client, session, sample_project):
    case, finding = _case_with_finding(session, sample_project.id)
    project = session.get(Project, sample_project.id)
    fingerprint = current_env_fingerprint(session, project)
    # 客户端可以多带自描述字段，但已知字段必须一致。
    fingerprint["runner_version"] = "test-runner"
    evidence = _evidence(session, sample_project.id, case, finding, env_fingerprint=fingerprint)

    response = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": True, "verification_id": evidence.id, "note": "证据有效"},
    )
    assert response.status_code == 200, response.text
    session.refresh(finding)
    assert finding.fix_status == "verified_fixed"
    history = client.get(f"/api/findings/{finding.id}/fix-history").json()
    assert history[0]["verification_id"] == evidence.id


def test_mcp_verification_requires_evidence(session, sample_project):
    from app.fixes import verify_fix
    from app.schemas import FindingFixVerify

    case, finding = _case_with_finding(session, sample_project.id)
    with pytest.raises(ValueError, match="verification_id"):
        verify_fix(session, finding, FindingFixVerify(verified=True), source="mcp")
    session.refresh(finding)
    assert finding.fix_status == "claimed_fixed"


def test_external_report_without_shakedown_replay_does_not_verify(session, sample_project):
    from app.worker.handlers import handle_verify_fixes

    case, finding = _case_with_finding(session, sample_project.id, mode="artifact")
    case.verification_status = "passed_external"
    case.verification = {"passed": True, "actor": "mcp:dev-agent", "result": "ok"}
    session.commit()

    result = handle_verify_fixes(
        session,
        {"project_id": sample_project.id, "finding_ids": [finding.id]},
    )
    assert result["verified"] == 0
    assert result["needs_external"] == 1
    session.refresh(finding)
    assert finding.fix_status == "needs_external_verification"


def test_replay_result_overrides_self_reported_pass(session, sample_project):
    """自报通过仍会被独立重放校验；重放失败则重新打开。"""
    from app.worker.handlers import handle_verify_fixes

    steps = [{"action": "request", "method": "GET", "path": "/api/health"}]
    case = CaseModel(
        project_id=sample_project.id,
        title="健康检查重放",
        dimension="functional",
        case_type="api",
        steps=steps,
        spec={"title": "健康检查重放", "steps": steps, "assertions": []},
        status="enabled",
        verification_mode="api",
        verification_status="passed_external",
        verification={"passed": True, "actor": "mcp:dev-agent", "result": "ok"},
    )
    session.add(case)
    session.flush()
    finding = Finding(
        project_id=sample_project.id,
        source="whitebox",
        title="健康检查缺陷",
        description="",
        severity="high",
        status="confirmed",
        module="api",
        risk_type="availability",
        evidence=[],
        case_id=case.id,
        fix_status="claimed_fixed",
        fix_claim={"commit_sha": "abc"},
    )
    session.add(finding)
    session.commit()

    result = handle_verify_fixes(
        session,
        {"project_id": sample_project.id, "finding_ids": [finding.id]},
    )
    assert result["verified"] == 0
    assert result["reopened"] == 1
    session.refresh(finding)
    assert finding.fix_status == "reopened"
    history = session.query(Finding).filter(Finding.id == finding.id).one()
    assert history.fix_claim["verification"]["verified"] is False


def test_auto_verify_non_api_case_goes_external(session, sample_project):
    from app.worker.handlers import handle_verify_fixes

    case, finding = _case_with_finding(session, sample_project.id, mode="artifact")
    result = handle_verify_fixes(
        session,
        {"project_id": sample_project.id, "finding_ids": [finding.id]},
    )
    session.refresh(finding)
    assert result["needs_external"] == 1
    assert finding.fix_status == "needs_external_verification"


def test_replay_pass_records_replayed_evidence(client, session, sample_project, monkeypatch):
    """独立重放通过要写 replayed 证据，finding 的验证历史引用该证据。"""
    from app.worker import handlers

    steps = [{"action": "request", "method": "GET", "path": "/api/health"}]
    case = CaseModel(
        project_id=sample_project.id,
        title="健康检查重放通过",
        dimension="functional",
        case_type="api",
        steps=steps,
        spec={"title": "健康检查重放通过", "steps": steps, "assertions": []},
        status="enabled",
        verification_mode="api",
    )
    session.add(case)
    session.flush()
    finding = Finding(
        project_id=sample_project.id,
        source="whitebox",
        title="健康检查缺陷",
        description="",
        severity="high",
        status="confirmed",
        module="api",
        risk_type="availability",
        evidence=[],
        case_id=case.id,
        fix_status="claimed_fixed",
        fix_claim={"commit_sha": "abc"},
    )
    session.add(finding)
    session.commit()

    def fake_execute(session_arg, payload):
        run = session_arg.get(Run, payload["run_id"])
        session_arg.add(
            CaseResult(
                run_id=run.id,
                case_id=case.id,
                project_id=sample_project.id,
                status="pass",
                request={},
                response={},
            )
        )
        session_arg.commit()

    monkeypatch.setattr(handlers, "handle_execute_run", fake_execute)
    result = handlers.handle_verify_fixes(
        session,
        {"project_id": sample_project.id, "finding_ids": [finding.id]},
    )

    assert result["verified"] == 1
    session.refresh(finding)
    assert finding.fix_status == "verified_fixed"
    session.refresh(case)
    assert case.verification_status == "passed_external"
    assert case.verified_by == "shakedown"
    evidence = session.query(CaseVerification).filter(
        CaseVerification.case_id == case.id,
        CaseVerification.evidence_level == "replayed",
    ).one()
    assert evidence.status == "passed_external"
    assert evidence.expires_at is not None
    assert evidence.env_fingerprint["base_url"] == sample_project.base_url
    history = client.get(f"/api/findings/{finding.id}/fix-history").json()
    assert history[0]["verification_id"] == evidence.id
