"""测试数据资产、fixture 生命周期、数据驱动执行与文件上传测试。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.executor.environment import Environment, cleanup_environment
from app.executor.runner import _env_for_run, _referenced_accounts
from app.models import TestCase as CaseModel
from app.schemas.project import TargetStart

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "executor" / "templates" / "runtime.py"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("shakedown_runtime_data_under_test", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()


def test_target_start_accepts_prepare_commands():
    value = TargetStart(
        type="command",
        allow_execute=True,
        start_command="echo start",
        migrate_command=["alembic", "upgrade", "head"],
        seed_command="python seed.py",
        cleanup_command="rm -rf tmp",
        wait_seconds=5,
    )
    dumped = value.model_dump()
    assert dumped["allow_execute"] is True
    assert dumped["migrate_command"] == ["alembic", "upgrade", "head"]
    assert dumped["seed_command"] == "python seed.py"


def test_cleanup_only_deletes_created_service(monkeypatch):
    calls = []

    def fake_delete(self, ref, volumes=False):
        calls.append((ref, volumes))
        return {}

    monkeypatch.setattr("app.executor.environment.BerthClient.delete_service", fake_delete)
    env = Environment(db_service="shared-db", db_service_created=False)
    assert cleanup_environment(None, env) == []
    assert calls == []

    env.db_service_created = True
    notes = cleanup_environment(None, env)
    assert calls == [("shared-db", True)]
    assert notes


def test_env_injects_only_referenced_accounts_and_read_dsn():
    specs = [
        {
            "steps": [
                {"action": "request", "auth": {"account": "viewer"}, "path": "/x"},
                {"action": "request", "headers": {"X": "{{account.admin.token}}"}},
            ]
        }
    ]
    names = _referenced_accounts(specs)
    assert names == {"viewer", "admin"}
    env = Environment(
        target_dsn="postgresql://write/db",
        read_dsn="postgresql://read/db",
        accounts={"admin": {"token": "a"}, "viewer": {"token": "v"}, "other": {"token": "o"}},
    )
    run = type("Run", (), {"id": 7, "project_id": 3})()
    values = _env_for_run(env, run, "/tmp/artifacts", account_names=names)
    assert values["SHAKEDOWN_READ_DSN"] == "postgresql://read/db"
    assert set(json.loads(values["SHAKEDOWN_ACCOUNTS"])) == {"admin", "viewer"}


def test_runtime_runs_fixture_and_cleanup_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    events: list[str] = []

    def fake_request(step, ctx):
        events.append(step.get("path", ""))
        ctx.requests.append({"method": step.get("method"), "url": step.get("path")})
        ctx.responses.append({"status_code": 500, "body": {}, "duration_ms": 1.0})
        return ctx.responses[-1]

    monkeypatch.setattr(runtime, "_perform_request", fake_request)
    case = {
        "id": "fixture-fail",
        "title": "fixture 失败清理",
        "fixtures": [
            {
                "kind": "api",
                "name": "seed",
                "steps": [{"action": "request", "method": "POST", "path": "/seed"}],
                "cleanup": [{"action": "request", "method": "DELETE", "path": "/seed/1"}],
            }
        ],
        "steps": [{"action": "request", "method": "GET", "path": "/main"}],
        "assertions": [
            {
                "kind": "status_code",
                "operator": "eq",
                "expected": 200,
                "status": "confirmed",
                "evidence": {"source_type": "policy", "ref": "P"},
            }
        ],
    }
    with pytest.raises(AssertionError):
        runtime.run_case(case)
    assert events == ["/seed", "/main", "/seed/1"]
    payload = json.loads((tmp_path / "results" / "case_fixture-fail.json").read_text(encoding="utf-8"))
    assert payload["status"] == "fail"
    assert payload["output"]["cleanup_errors"] == []


def test_runtime_fixture_setup_failure_is_error_and_skips_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    called = []
    monkeypatch.setattr(runtime, "_db_fetch", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(runtime, "_perform_request", lambda step, ctx: called.append(step))
    case = {
        "id": "fixture-error",
        "title": "fixture setup error",
        "fixtures": [{"kind": "sql", "name": "seed", "sql": "INSERT INTO x VALUES (1)"}],
        "steps": [{"action": "request", "method": "GET", "path": "/main"}],
    }
    with pytest.raises(RuntimeError):
        runtime.run_case(case)
    assert called == []
    payload = json.loads((tmp_path / "results" / "case_fixture-error.json").read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert payload["output"]["data_index"] is None


def test_runtime_iterates_dataset_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    seen: list[any] = []

    def fake_request(step, ctx):
        seen.append(ctx.render(step.get("body", {})).get("name"))
        ctx.requests.append({"method": "GET", "url": "/x"})
        ctx.responses.append({"status_code": 200, "body": {"ok": True}, "duration_ms": 1.0})
        return ctx.responses[-1]

    monkeypatch.setattr(runtime, "_perform_request", fake_request)
    case = {
        "id": "iterate",
        "title": "按数据集执行",
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/x",
                "body": {"name": "{{data.name}}"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "status": "confirmed",
                        "evidence": {"source_type": "policy", "ref": "P"},
                    }
                ],
            }
        ],
        "__data_rows": [{"name": "a"}, {"name": "b"}, {"name": "c"}],
    }
    runtime.run_case(case)
    assert seen == ["a", "b", "c"]
    payload = json.loads((tmp_path / "results" / "case_iterate.json").read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["output"]["data_rows"] == 3
    assert [item["index"] for item in payload["output"]["iterations"]] == [0, 1, 2]


def test_runtime_multipart_file_blob():
    ctx = runtime.CaseContext({"id": "1", "__file_blobs": {3: {"filename": "a.txt", "content_type": "text/plain", "content_b64": "aGVsbG8="}}})
    files, meta = runtime._request_files(
        {"files": {"upload": {"file_id": 3}}},
        ctx,
    )
    assert files["upload"][0] == "a.txt"
    assert files["upload"][1] == b"hello"
    assert meta[0]["size"] == 5


def test_dataset_api_and_binding(client, session, sample_project):
    response = client.post(
        f"/api/projects/{sample_project.id}/test-datasets",
        json={
            "name": "users",
            "count": 2,
            "fields": [
                {"name": "email", "type": "string", "format": "email", "unique": True},
                {"name": "amount", "type": "number", "minimum": 10, "maximum": 20},
            ],
            "schema": {"relations": []},
        },
    )
    assert response.status_code == 201, response.text
    dataset = response.json()
    assert dataset["row_count"] == 2
    assert "email" in dataset["pii_fields"]
    assert dataset["rows"][0]["data"]["email"]

    case = CaseModel(
        project_id=sample_project.id,
        title="data case",
        dimension="functional",
        case_type="api",
        spec={"title": "data case", "dimension": "functional", "type": "api", "steps": []},
        steps=[],
    )
    session.add(case)
    session.commit()
    session.refresh(case)
    bind = client.post(
        f"/api/test-cases/{case.id}/data-bindings",
        json={"dataset_id": dataset["id"], "mode": "iterate"},
    )
    assert bind.status_code == 201, bind.text
    assert bind.json()["dataset_row_count"] == 2

    health = client.get(f"/api/projects/{sample_project.id}/test-data/health").json()
    assert health["dataset_count"] == 1
    assert health["row_count"] == 2
    assert health["binding_count"] == 1

    listing = client.get(f"/api/projects/{sample_project.id}/test-datasets").json()
    assert listing["count"] == 1
    assert listing["items"][0]["name"] == "users"


def test_file_upload_and_health(client, sample_project):
    response = client.post(
        f"/api/projects/{sample_project.id}/test-files",
        files={"file": ("avatar.txt", b"hello", "text/plain")},
        data={"name": "avatar"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["name"] == "avatar"
    assert payload["size"] == 5
    assert len(payload["sha256"]) == 64
    listing = client.get(f"/api/projects/{sample_project.id}/test-files").json()
    assert listing["count"] == 1
    health = client.get(f"/api/projects/{sample_project.id}/test-data/health").json()
    assert health["file_count"] == 1


def test_account_preflight_skips_without_base_url(client, session, sample_project):
    from app.crypto import encrypt_json

    sample_project.base_url = ""
    sample_project.test_accounts_encrypted = encrypt_json({"default": {"username": "u", "password": "p"}})
    from app.db import SessionLocal

    with SessionLocal() as session:
        session.merge(sample_project)
        session.commit()
    body = client.post(f"/api/projects/{sample_project.id}/accounts/validate", json={}).json()
    assert body["total"] >= 1
    assert body["skipped"] >= 1


def test_prepare_data_runs_commands_with_dsn(session, sample_project, monkeypatch):
    from app.environment_manager import prepare_data

    sample_project.target_start = {
        "type": "external",
        "allow_execute": True,
        "migrate_command": ["echo", "migrate"],
        "seed_command": "echo seed",
    }
    sample_project.settings_json = {"automation": {"allow_target_commands": True, "command_execution": "host", "allow_host_commands": True}}
    calls = []

    def fake_run(project, args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "execution": "host", "sandboxed": False}

    monkeypatch.setattr("app.environment_manager.run_project_command", fake_run)
    result = prepare_data(sample_project, dsn="postgresql://read/db")
    assert result["prepared"] is True
    assert [step["name"] for step in result["steps"]] == ["migrate", "seed"]
    assert calls[0][1]["env"]["SHAKEDOWN_TARGET_DSN"] == "postgresql://read/db"
    assert calls[0][1]["env"]["DATABASE_URL"] == "postgresql://read/db"


def test_dataset_mask_pii_preview(client, sample_project):
    response = client.post(
        f"/api/projects/{sample_project.id}/test-datasets",
        json={"name": "pii", "count": 1, "fields": [{"name": "email", "type": "string", "format": "email"}]},
    )
    dataset_id = response.json()["id"]
    masked = client.get(f"/api/projects/{sample_project.id}/test-datasets/{dataset_id}?mask_pii=true").json()
    assert "***@example.com" in masked["rows"][0]["data"]["email"]
    raw = client.get(f"/api/projects/{sample_project.id}/test-datasets/{dataset_id}").json()
    assert "@example.com" in raw["rows"][0]["data"]["email"]
