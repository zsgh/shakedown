"""无人值守触发与健康摘要测试。"""
from __future__ import annotations

import hashlib
import hmac
import json

from app.models import Run, Task
from app.models import TestCase as CaseModel


def _case(project_id: int) -> CaseModel:
    return CaseModel(
        project_id=project_id,
        title="自动化用例",
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/api/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/health"}], "assertions": []},
        status="enabled",
    )


def test_automation_trigger_creates_run(client, session, sample_project, monkeypatch):
    session.add(_case(sample_project.id))
    session.commit()
    monkeypatch.setattr("app.automation.check_target", lambda project: {"ok": True})
    response = client.post(
        f"/api/projects/{sample_project.id}/automation/trigger",
        json={"trigger": "deploy", "policy": {"mode": "full"}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    run = session.get(Run, body["run_id"])
    assert run.trigger == "deploy"
    assert run.case_ids
    assert session.query(Task).filter(Task.kind == "execute_run", Task.run_id == run.id).count() == 1


def test_automation_trigger_token(client, session, sample_project, monkeypatch):
    sample_project.settings_json = {"automation_trigger_token": "secret"}
    session.add(sample_project)
    session.commit()
    monkeypatch.setattr("app.automation.check_target", lambda project: {"ok": True})
    denied = client.post(f"/api/projects/{sample_project.id}/automation/trigger", json={})
    assert denied.status_code == 403
    allowed = client.post(
        f"/api/projects/{sample_project.id}/automation/trigger",
        json={},
        headers={"X-Shakedown-Trigger-Token": "secret"},
    )
    assert allowed.status_code == 200


def test_github_webhook_verifies_and_triggers(client, session, sample_project, monkeypatch):
    session.add(_case(sample_project.id))
    sample_project.settings_json = {
        "github_webhook_secret": "secret",
        "github_trigger_policy": {"mode": "full"},
    }
    session.add(sample_project)
    session.commit()
    monkeypatch.setattr("app.automation.check_target", lambda project: {"ok": True})
    payload = {"action": "opened"}
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        f"/api/projects/{sample_project.id}/automation/github",
        content=raw,
        headers={
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["triggered"] is True
    run = session.get(Run, response.json()["run_id"])
    assert run.trigger.startswith("github:")


def test_gitlab_webhook_checks_token_and_triggers(client, session, sample_project, monkeypatch):
    session.add(_case(sample_project.id))
    sample_project.settings_json = {"gitlab_webhook_secret": "secret", "gitlab_trigger_policy": {"mode": "full"}}
    session.add(sample_project)
    session.commit()
    monkeypatch.setattr("app.automation.check_target", lambda project: {"ok": True})
    denied = client.post(
        f"/api/projects/{sample_project.id}/automation/gitlab",
        json={"object_attributes": {"action": "open"}},
        headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert denied.status_code == 403
    allowed = client.post(
        f"/api/projects/{sample_project.id}/automation/gitlab",
        json={"object_attributes": {"action": "open"}},
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["triggered"] is True
    assert session.get(Run, allowed.json()["run_id"]).trigger == "gitlab:mr"


def test_run_quota_blocks_trigger(client, session, sample_project):
    sample_project.settings_json = {"automation": {"max_runs_per_day": 1}}
    session.add(sample_project)
    session.add(Run(project_id=sample_project.id, status="completed", trigger="manual"))
    session.commit()
    body = client.post(
        f"/api/projects/{sample_project.id}/automation/trigger",
        json={"trigger": "deploy"},
    ).json()
    assert body["created"] is False
    assert body["status"] == "quota_exceeded"


def test_ci_gate_blocks_failed_run(client, session, sample_project):
    from app.models import CaseResult, Finding
    from app.models import TestCase as CaseModel

    case = CaseModel(
        project_id=sample_project.id,
        title="失败用例",
        dimension="functional",
        case_type="api",
        status="enabled",
        steps=[],
        spec={},
    )
    run = Run(project_id=sample_project.id, status="completed")
    session.add_all([case, run])
    session.flush()
    result = CaseResult(
        run_id=run.id,
        case_id=case.id,
        project_id=sample_project.id,
        status="fail",
        request={},
        response={},
    )
    session.add(result)
    session.flush()
    session.add(
        Finding(
            project_id=sample_project.id,
            source="execution",
            title="严重缺陷",
            severity="critical",
            status="confirmed",
            result_id=result.id,
            case_id=case.id,
            fix_status="open",
        )
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/automation/gate?run_id={run.id}").json()
    assert body["passed"] is False
    kinds = {item["kind"] for item in body["blockers"]}
    assert {"failed_cases", "confirmed_findings"} <= kinds


def test_ci_gate_scopes_slo_to_run_window(client, session, sample_project):
    """旧 run 的 gate 不能被之后新 run 产生的 SLO 失败误阻断。"""
    from datetime import datetime, timedelta, timezone

    from app.models import PerformanceRun, Run

    base = datetime.now(timezone.utc)
    old_run = Run(project_id=sample_project.id, status="completed", created_at=base)
    new_run = Run(project_id=sample_project.id, status="completed", created_at=base + timedelta(minutes=10))
    session.add_all([old_run, new_run])
    session.flush()
    in_window = PerformanceRun(
        project_id=sample_project.id,
        path="/api/x",
        slo_passed=False,
        created_at=base + timedelta(minutes=5),
    )
    after_window = PerformanceRun(
        project_id=sample_project.id,
        path="/api/x",
        slo_passed=False,
        created_at=base + timedelta(minutes=20),
    )
    session.add_all([in_window, after_window])
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/automation/gate?run_id={old_run.id}").json()
    slo_blockers = [item for item in body["blockers"] if item["kind"] == "slo"]
    assert len(slo_blockers) == 1
    assert slo_blockers[0]["performance_ids"] == [in_window.id]
    assert body["slo_window"]["end"] is not None

    # 最新 run 没有下一个 run，窗口向后开放；此时应看到它之后的失败。
    latest = client.get(f"/api/projects/{sample_project.id}/automation/gate?run_id={new_run.id}").json()
    latest_slo = [item for item in latest["blockers"] if item["kind"] == "slo"]
    assert latest_slo and latest_slo[0]["performance_ids"] == [after_window.id]


def test_automation_health(client, session, sample_project):
    session.add(_case(sample_project.id))
    session.commit()
    body = client.get(f"/api/projects/{sample_project.id}/automation/health").json()
    assert body["project_id"] == sample_project.id
    assert body["execution_states"]["never_executed"] == 1
    assert "queue" in body
    assert "sla" in body
