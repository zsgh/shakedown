"""缺陷修复声明 / 验证生命周期。"""
from __future__ import annotations

from app.models import Finding


def test_finding_fix_claim_verify_and_reopen(client, session, sample_project):
    finding = Finding(
        project_id=sample_project.id,
        source="whitebox",
        title="鉴权缺失",
        description="",
        severity="high",
        status="confirmed",
        module="internal",
        risk_type="auth",
        evidence=[],
        code_location={},
    )
    session.add(finding)
    session.commit()

    claimed = client.post(
        f"/api/findings/{finding.id}/fix",
        json={
            "commit_sha": "abc123",
            "pr_url": "https://example.com/pr/1",
            "files": ["internal/api.go"],
            "note": "补充服务端鉴权检查",
            "test_command": "go test ./...",
            "test_result": "PASS",
        },
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["finding"]["fix_status"] == "claimed_fixed"
    assert body["fix"]["status"] == "claimed_fixed"

    history = client.get(f"/api/findings/{finding.id}/fix-history").json()
    assert len(history) == 1
    assert history[0]["commit_sha"] == "abc123"

    verified = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": True, "note": "回归通过", "test_result": "PASS"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["finding"]["fix_status"] == "verified_fixed"
    assert verified.json()["finding"]["fix_verified_at"]

    again = client.post(
        f"/api/findings/{finding.id}/fix",
        json={"commit_sha": "def456", "note": "再次修复"},
    )
    assert again.status_code == 200

    reopened = client.post(
        f"/api/findings/{finding.id}/fix/verify",
        json={"verified": False, "note": "回归失败"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["finding"]["fix_status"] == "reopened"


def test_auto_verify_without_executable_case_goes_external(client, session, sample_project):
    from app.worker.handlers import handle_verify_fixes

    finding = Finding(
        project_id=sample_project.id,
        source="whitebox",
        title="没有可执行用例的修复",
        description="",
        severity="high",
        status="confirmed",
        module="web",
        risk_type="auth",
        evidence=[],
        code_location={},
        fix_status="claimed_fixed",
        fix_claim={"commit_sha": "abc"},
    )
    session.add(finding)
    session.commit()

    result = handle_verify_fixes(session, {"project_id": sample_project.id, "finding_ids": [finding.id]})
    session.refresh(finding)
    assert result["needs_external"] == 1
    assert finding.fix_status == "needs_external_verification"
    history = client.get(f"/api/findings/{finding.id}/fix-history").json()
    assert history[0]["status"] == "needs_external_verification"


def test_finding_fix_requires_evidence(client, session, sample_project):
    finding = Finding(
        project_id=sample_project.id,
        source="execution",
        title="空修复",
        description="",
        severity="low",
        status="confirmed",
        module="",
        risk_type="",
        evidence=[],
        code_location={},
    )
    session.add(finding)
    session.commit()
    response = client.post(f"/api/findings/{finding.id}/fix", json={})
    assert response.status_code == 400
    assert "至少需要" in response.text
