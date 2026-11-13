"""环境生命周期、产物校验、测试数据与性能执行测试。"""
from __future__ import annotations

from types import SimpleNamespace

from app.environment_manager import start_environment
from app.performance import run_performance
from app.process_verifier import run_verification_command


def test_environment_multi_probe(session, sample_project, monkeypatch):
    from app.environment_check import check_target

    sample_project.settings_json = {
        "readiness_probes": [
            {"path": "/health", "expected_status": [200]},
            {"path": "/api/health", "expected_status": [200]},
        ]
    }
    monkeypatch.setattr("httpx.request", lambda method, url, **kwargs: SimpleNamespace(status_code=200))
    result = check_target(sample_project)
    assert result["ok"] is True
    assert [item["url"].split("127.0.0.1:9")[-1] for item in result["probes"]] == ["/health", "/api/health"]


def test_environment_start_requires_explicit_permission(session, sample_project, monkeypatch):
    sample_project.target_start = {"type": "command", "start_command": "echo start"}
    sample_project.settings_json = {}
    monkeypatch.setattr("app.environment_manager.check_target", lambda project: {"ok": True})
    result = start_environment(sample_project)
    assert result["started"] is False
    assert "未允许" in result["reason"]

    sample_project.target_start = {"type": "command", "start_command": "echo start", "allow_execute": True}
    sample_project.settings_json = {"automation": {"allow_target_commands": True}}
    called = {}

    def fake_run(args, **kwargs):
        called["args"] = args
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    # 新默认：没有沙箱隔离时拒绝执行项目命令。
    denied = start_environment(sample_project)
    assert denied["started"] is False
    assert "local 沙箱" in denied["command"]["error"]

    # 显式宿主执行才允许调用 subprocess，并标注 execution=host。
    sample_project.settings_json = {
        "automation": {
            "allow_target_commands": True,
            "command_execution": "host",
            "allow_host_commands": True,
        }
    }
    monkeypatch.setattr("app.command_runner.subprocess.run", fake_run)
    result = start_environment(sample_project)
    assert result["started"] is True
    assert called["args"] == ["echo", "start"]
    assert result["command"]["execution"] == "host"
    assert result["command"]["sandboxed"] is False


def test_artifact_verify(client, session, sample_project, tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "icon.png").write_bytes(b"fake-png")
    sample_project.settings_json = {"artifact_root": str(root)}
    session.add(sample_project)
    session.commit()

    body = client.post(
        f"/api/projects/{sample_project.id}/artifacts/verify",
        json={"paths": ["icon.png", "../secret.txt", "missing.png"]},
    ).json()
    assert body["configured"] is True
    items = {item["path"]: item for item in body["items"]}
    assert items["icon.png"]["exists"] is True
    assert len(items["icon.png"]["sha256"]) == 64
    assert items["../secret.txt"]["error"] == "路径越界"
    assert items["missing.png"]["exists"] is False


def test_generate_business_semantic_test_data(client, sample_project):
    body = client.post(
        f"/api/projects/{sample_project.id}/test-data/generate",
        json={
            "count": 3,
            "fields": [
                {"name": "email", "type": "string"},
                {"name": "amount", "type": "number", "minimum": 1, "maximum": 100},
                {"name": "status", "type": "string", "enum": ["new", "paid"]},
            ],
        },
    ).json()
    assert body["count"] == 3
    rows = body["rows"]
    assert "@example.com" in rows[0]["email"]
    assert 1 <= rows[0]["amount"] <= 100
    assert {row["status"] for row in rows} == {"new", "paid"}

    normal = client.post(
        f"/api/projects/{sample_project.id}/test-data/generate",
        json={
            "count": 5,
            "fields": [{"name": "amount", "type": "number", "minimum": 0, "maximum": 100}],
            "distribution": "normal",
            "seed": 7,
        },
    ).json()
    assert all(0 <= row["amount"] <= 100 for row in normal["rows"])


def test_whitelisted_verification_command(client, session, sample_project):
    sample_project.settings_json = {
        "automation": {"allow_local_sandbox_commands": True},
        "verification_commands": [
            {"name": "echo", "command": ["/bin/echo", "ok"], "mode": "process"}
        ],
    }
    session.add(sample_project)
    session.commit()
    listed = client.get(f"/api/projects/{sample_project.id}/verification-commands").json()
    assert listed["items"][0]["name"] == "echo"
    result = client.post(
        f"/api/projects/{sample_project.id}/verification-commands/echo/run",
        json={},
    ).json()
    assert result["passed"] is True
    assert "ok" in result["stdout"]
    assert result["execution"] == "local"
    assert result["sandboxed"] is False


def test_verification_command_is_denied_without_sandbox_opt_in(client, session, sample_project):
    sample_project.settings_json = {
        "verification_commands": [
            {"name": "echo", "command": ["/bin/echo", "should-not-run"], "mode": "process"}
        ]
    }
    session.add(sample_project)
    session.commit()
    result = client.post(
        f"/api/projects/{sample_project.id}/verification-commands/echo/run",
        json={},
    ).json()
    assert result["passed"] is False
    assert result["execution"] == "denied"
    assert "local 沙箱" in result["error"]


def test_verification_command_uses_berth_sandbox(session, sample_project, monkeypatch):
    from app.sandbox.base import SandboxResult

    calls = {}

    class FakeSandbox:
        name = "berth"

        def run_command(self, command, **kwargs):
            calls["command"] = command
            calls["kwargs"] = kwargs
            return SandboxResult(
                ok=True,
                exit_code=0,
                stdout="sandbox-ok",
                task_id="task-1",
                raw={"execution": "berth"},
            )

    monkeypatch.setattr("app.command_runner.get_sandbox", lambda: FakeSandbox())
    sample_project.settings_json = {
        "verification_commands": [
            {"name": "check", "command": ["/bin/echo", "ok"], "mode": "process"}
        ]
    }
    result = run_verification_command(session, sample_project, "check")
    assert result["passed"] is True
    assert result["execution"] == "berth"
    assert result["sandboxed"] is True
    assert calls["command"] == ["/bin/echo", "ok"]
    assert calls["kwargs"]["task_id"] == f"verify-{sample_project.id}-check"


def test_project_role_matrix(client, sample_project):
    created = client.post(
        f"/api/projects/{sample_project.id}/members",
        json={"actor": "qa-lead", "role": "owner", "note": "负责人"},
    )
    assert created.status_code == 201, created.text
    member = created.json()
    assert member["role"] == "owner"
    listed = client.get(f"/api/projects/{sample_project.id}/members").json()
    assert [item["actor"] for item in listed] == ["qa-lead"]
    updated = client.patch(f"/api/members/{member['id']}", json={"role": "viewer", "active": False}).json()
    assert updated["role"] == "viewer"
    assert updated["active"] is False


def test_account_matrix_does_not_leak_secrets(client, session, sample_project):
    from app.crypto import encrypt_json

    sample_project.test_accounts_encrypted = encrypt_json(
        {"admin": {"username": "admin", "password": "secret", "token": "abc"}}
    )
    session.add(sample_project)
    session.commit()
    body = client.get(f"/api/projects/{sample_project.id}/accounts").json()
    assert body["roles"] == ["admin"]
    assert body["accounts"][0]["fields"] == ["password", "token", "username"]
    assert "secret" not in str(body)
    assert "abc" not in str(body)


def test_a11y_rule_audit(client, session, sample_project):
    body = client.post(
        f"/api/projects/{sample_project.id}/ui/a11y/audit",
        json={
            "images_without_alt": 1,
            "buttons_without_label": 0,
            "inputs_without_label": 0,
            "links_without_text": 0,
            "duplicate_ids": [],
            "heading_jumps": 0,
            "has_main_landmark": True,
            "has_title": True,
            "html_lang": "zh-CN",
        },
    ).json()
    assert body["passed"] is False
    assert {item["id"] for item in body["violations"]} == {"image-alt"}


def test_visual_baseline_compare(client, session, sample_project):
    from app.models import CaseResult, Run
    from app.models import TestCase as CaseModel

    case = CaseModel(
        project_id=sample_project.id,
        title="视觉用例",
        dimension="functional",
        case_type="web",
        status="enabled",
        spec={"steps": []},
    )
    run = Run(project_id=sample_project.id, status="completed")
    session.add_all([case, run])
    session.flush()
    result = CaseResult(
        run_id=run.id,
        case_id=case.id,
        project_id=sample_project.id,
        status="pass",
        output={"screenshot_sha256": "a" * 64, "screenshot": "artifacts/a.png"},
    )
    session.add(result)
    session.commit()

    baseline = client.post(
        f"/api/projects/{sample_project.id}/visual/baselines",
        json={"case_id": case.id},
    )
    assert baseline.status_code == 201, baseline.text
    same = client.post(
        f"/api/projects/{sample_project.id}/visual/compare",
        json={"case_id": case.id},
    ).json()
    assert same["status"] == "same"

    session.add(
        CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="pass",
            output={"screenshot_sha256": "b" * 64, "screenshot": "artifacts/b.png"},
        )
    )
    session.commit()
    changed = client.post(
        f"/api/projects/{sample_project.id}/visual/compare",
        json={"case_id": case.id},
    ).json()
    assert changed["status"] == "changed"


def test_audit_timeline(client, session, sample_project):
    from app.models import AutomationException

    session.add(
        AutomationException(
            project_id=sample_project.id,
            kind="test_exception",
            target_type="run",
            target_id=1,
            severity="medium",
            status="open",
            note="audit test",
        )
    )
    session.commit()
    body = client.get(f"/api/projects/{sample_project.id}/audit").json()
    assert body["items"]
    assert any(item["kind"] == "exception" for item in body["items"])


def test_performance_api_persists_history(client, sample_project, monkeypatch):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    body = client.post(
        f"/api/projects/{sample_project.id}/performance/run",
        json={"method": "GET", "path": "/api/health", "iterations": 3},
    ).json()
    assert body["id"]
    history = client.get(f"/api/projects/{sample_project.id}/performance/history").json()
    assert history["count"] == 1
    assert len(history["trend"]["p95_ms"]) == 1


def test_performance_runner_metrics(monkeypatch, sample_project):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    result = run_performance(
        sample_project,
        method="GET",
        path="/api/health",
        concurrency=2,
        iterations=5,
        timeout=1,
    )
    assert result["iterations"] == 5
    assert result["success_rate"] == 1.0
    assert result["latency_ms"]["p95"] >= 0
    assert result["slo"]["configured"] is False

    sample_project.settings_json = {
        "performance_slo": {"p95_ms": 1, "error_rate": 0, "min_rps": 999999}
    }
    result = run_performance(sample_project, method="GET", path="/api/health", iterations=3)
    assert result["slo"]["configured"] is True
    assert result["slo"]["passed"] is False
    assert result["slo"]["violations"]
