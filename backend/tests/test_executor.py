"""用例 schema、编译器、运行时断言引擎测试。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.executor.case_schema import collect_assertions, pending_assertions, validate_case_spec
from app.executor.compiler import collect_results, compile_cases
from app.executor.environment import Environment
from app.executor.normalizer import normalize_spec
from app.executor.runner import _env_for_run

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "executor" / "templates" / "runtime.py"


def _load_runtime():
    spec = importlib.util.spec_from_file_location("shakedown_runtime_under_test", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = _load_runtime()


VALID_CASE = {
    "title": "创建用户",
    "dimension": "functional",
    "type": "api",
    "steps": [
        {
            "action": "request",
            "method": "POST",
            "path": "/api/users",
            "body": {"name": "{{run_id}}"},
            "assert": [
                {
                    "kind": "status_code",
                    "operator": "eq",
                    "expected": 201,
                    "status": "confirmed",
                    "evidence": {"source_type": "openapi", "ref": "#/paths/~1api~1users/post"},
                }
            ],
        }
    ],
    "assertions": [],
}


def test_valid_case_passes_schema():
    assert validate_case_spec(VALID_CASE) == []


def test_missing_steps_is_invalid():
    broken = dict(VALID_CASE)
    broken.pop("steps")
    errors = validate_case_spec(broken)
    assert errors


def test_unknown_assertion_kind_is_invalid():
    broken = dict(VALID_CASE)
    broken["steps"] = [{"action": "request", "method": "GET", "path": "/x", "assert": [{"kind": "unknown_kind"}]}]
    assert validate_case_spec(broken)


def test_pending_assertions_detected():
    spec = dict(VALID_CASE)
    spec["assertions"] = [{"kind": "status_code", "status": "pending", "evidence": {"source_type": "none"}}]
    pending = pending_assertions(spec)
    assert len(pending) == 1


def test_compiler_generates_python_not_json():
    files = compile_cases([{"id": "7", **VALID_CASE}])
    assert "shakedown_runtime.py" in files
    content = files["test_shakedown_cases.py"]
    # JSON 的 true/false 在 Python 里非法 —— 编译产物必须是 Python 字面量
    assert "true" not in content.replace("True", "")
    assert "def test_shakedown_case_7():" in content
    compile(content, "test_shakedown_cases.py", "exec")  # 语法正确


def test_collect_results_parses_marker():
    payload = '[{"case_id": "1", "status": "pass"}]'
    stdout = f"some output\n<<<SHAKEDOWN_RESULT>>>{payload}\n"
    assert collect_results(stdout) == [{"case_id": "1", "status": "pass"}]
    assert collect_results("no marker") == []


def test_runtime_json_path():
    body = {"data": {"items": [{"id": 7}], "total": 1}}
    found, value = runtime._json_path(body, "$.data.items[0].id")
    assert found and value == 7
    found, value = runtime._json_path(body, "$.data.missing")
    assert not found and value is None
    found, value = runtime._json_path(body, "$")
    assert found and value == body


def test_runtime_json_path_wildcard():
    body = {"items": [{"id": 1}, {"id": 2}], "total": 2}
    found, value = runtime._json_path(body, "$.items[*].id")
    assert found and value == [1, 2]
    found, value = runtime._json_path(body, "$[*].id")
    assert found and value == []
    found, value = runtime._json_path({"rows": [1, 2]}, "$.rows[*]")
    assert found and value == [1, 2]


def test_runtime_compare_operators():
    assert runtime._compare("eq", 200, 200)
    assert runtime._compare("in", 400, [400, 422])
    assert runtime._compare("lt", 10, 20)
    assert runtime._compare("contains", "hello world", "world")
    assert not runtime._compare("eq", 200, 201)


def test_runtime_compare_coerces_numeric_ids():
    assert runtime._compare("eq", 182, "182")
    assert runtime._compare("eq", "182", 182)
    assert runtime._compare("ne", 182, "183")
    assert runtime._compare("in", 182, [181, "182"])


def test_runtime_exists_rejects_null_but_accepts_value():
    ctx = runtime.CaseContext({"id": "1"})
    assertion = {
        "kind": "json_path",
        "target": "$.task_id",
        "operator": "exists",
        "expected": True,
        "status": "confirmed",
        "evidence": {"source_type": "openapi", "ref": "x"},
    }
    null_response = {"status_code": 200, "body": {"task_id": None}}
    assert runtime._evaluate_assertion(assertion, ctx, null_response)["passed"] is False
    value_response = {"status_code": 200, "body": {"task_id": 42}}
    assert runtime._evaluate_assertion(assertion, ctx, value_response)["passed"] is True


def test_runtime_policy_assertion_reports_policy_id():
    from app.policies import policy_assertion

    ctx = runtime.CaseContext({"id": "1"})
    assertion = policy_assertion("POLICY-REL-001")
    outcome = runtime._evaluate_assertion(assertion, ctx, {"status_code": 500, "body": {}})
    assert outcome["passed"] is False
    assert outcome["pending"] is False
    assert outcome["detail"].startswith("[POLICY-REL-001@v1]")
    assert outcome["evidence"]["source_type"] == "policy"


def test_runtime_error_envelope_alias_for_detail():
    ctx = runtime.CaseContext({"id": "1"})
    assertion = {
        "kind": "json_path",
        "target": "$.detail",
        "operator": "exists",
        "expected": True,
        "status": "confirmed",
        "evidence": {"source_type": "openapi", "ref": "x"},
    }
    response = {
        "status_code": 422,
        "body": {"error": {"code": "validation_error", "message": "请求参数不合法"}},
    }
    outcome = runtime._evaluate_assertion(assertion, ctx, response)
    assert outcome["passed"] is True, outcome


def test_normalize_spec_coerces_schema_drift():
    schemas = {
        "POST /api/projects": {
            "body": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "priority": {"type": "integer"},
                    "isolation": {
                        "type": "object",
                        "properties": {"mode": {"type": "string"}},
                    },
                    "test_accounts": {"type": "object", "properties": {}},
                },
            },
            "params": {
                "sort": {"type": "string", "enum": ["id", "created"]},
                "limit": {"type": "integer"},
            },
            "responses": {},
        }
    }
    spec = {
        "title": "创建项目",
        "dimension": "functional",
        "type": "api",
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/api/projects",
                "query": {"sort": "created_at", "limit": "20"},
                "body": {
                    "name": "demo",
                    "priority": "P2",
                    "isolation": "shared",
                    "test_accounts": [{"username": "u", "password": "p"}],
                },
                "assert": [
                    {
                        "kind": "json_path",
                        "target": "$.isolation",
                        "operator": "eq",
                        "expected": "shared",
                    }
                ],
            }
        ],
    }
    normalize_spec(spec, schemas)
    step = spec["steps"][0]
    assert step["body"] == {
        "name": "demo",
        "isolation": {"mode": "shared"},
        "test_accounts": {},
    }
    assert step["query"] == {"limit": 20}
    assert step["assert"][0]["target"] == "$.isolation.mode"
    assert step["__request_schema"]["body"]["type"] == "object"


def test_normalize_spec_substitutes_semantic_placeholders():
    spec = {
        "title": "非法 ID",
        "dimension": "boundary",
        "type": "api",
        "steps": [
            {
                "action": "request",
                "method": "GET",
                "path": "/api/projects/{{invalid_project_id}}",
                "assert": [{"kind": "status_code", "expected": 422}],
            },
            {
                "action": "request",
                "method": "GET",
                "path": "/api/projects/{{nonexistent_project_id}}",
                "assert": [{"kind": "status_code", "expected": 404}],
            },
        ],
    }
    normalize_spec(spec, {})
    assert spec["steps"][0]["path"] == "/api/projects/not-a-number"
    assert spec["steps"][1]["path"] == "/api/projects/999999999"


def test_normalize_spec_keeps_negative_case_payload():
    schemas = {
        "POST /api/projects": {
            "body": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            "params": {},
            "responses": {},
        }
    }
    spec = {
        "title": "非法参数",
        "dimension": "boundary",
        "type": "api",
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/api/projects",
                "body": {"name": ""},
                "assert": [
                    {"kind": "status_code", "operator": "eq", "expected": 422}
                ],
            }
        ],
    }
    normalize_spec(spec, schemas)
    assert spec["steps"][0]["body"] == {"name": ""}


def test_runtime_render_placeholders():
    ctx = runtime.CaseContext({"id": "1"})
    ctx.variables["entity_id"] = 42
    ctx.variables["ids"] = [1, 2]
    rendered = ctx.render(
        {
            "path": "/api/users/{{entity_id}}",
            "n": "{{run_id}}",
            "ids": "{{ids}}",
            "id": "{{entity_id}}",
        }
    )
    assert rendered["path"] == "/api/users/42"
    assert rendered["n"] == ctx.variables["run_id"]
    assert rendered["ids"] == [1, 2]
    assert rendered["id"] == 42


def test_pending_assertion_does_not_fail_case(tmp_path, monkeypatch):
    """待确认断言只记录，不判失败。"""
    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    case = {
        "id": "99",
        "title": "待确认断言",
        "steps": [{"action": "request", "method": "GET", "path": "http://127.0.0.1:1/x"}],
        "assertions": [
            {
                "kind": "status_code",
                "operator": "eq",
                "expected": 200,
                "status": "pending",
                "evidence": {"source_type": "none"},
            }
        ],
    }
    with pytest.raises(Exception):
        runtime.run_case(case)  # 请求会失败（连接被拒）→ 抛异常
    result_files = list((tmp_path / "results").glob("*.json"))
    assert result_files
    import json

    payload = json.loads(result_files[0].read_text(encoding="utf-8"))
    # 连接失败属于执行异常；但待确认断言本身不应被记为「断言失败」
    assert payload["output"]["assertion_failed"] is False


def test_runtime_renders_assertion_expected_before_compare():
    """请求体里的 {{run_id}} 渲染后，断言 expected 也必须渲染，否则稳定误报。"""
    ctx = runtime.CaseContext({"id": "1"})
    ctx.variables["run_id"] = "15"
    assertion = {
        "kind": "json_path",
        "target": "$.name",
        "operator": "eq",
        "expected": "MYMAC_TEST_{{run_id}}",
        "status": "confirmed",
        "evidence": {"source_type": "openapi", "ref": "endpoint:71"},
        "description": "名称一致",
    }
    response = {"status_code": 200, "body": {"name": "MYMAC_TEST_15"}}
    outcome = runtime._evaluate_assertion(assertion, ctx, response)
    assert outcome["passed"] is True, outcome["detail"]
    assert outcome["actual"] == "MYMAC_TEST_15"
    assert outcome["description"] == "名称一致"


def test_runtime_result_includes_actual_and_pass_summary(tmp_path, monkeypatch):
    """执行结果的断言要带 actual / description，通过时日志里要有结论。"""
    import json

    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    def fake_request(step, ctx):
        ctx.requests.append({"method": "POST", "url": "http://example.test/x", "body": {}})
        ctx.responses.append(
            {"status_code": 200, "body": {"name": "MYMAC_APPLY_1"}, "duration_ms": 1.0}
        )
        return ctx.responses[-1]

    monkeypatch.setattr(runtime, "_perform_request", fake_request)
    case = {
        "id": "123",
        "title": "带实际值的断言",
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/x",
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "status": "confirmed",
                        "evidence": {"source_type": "openapi", "ref": "endpoint:1"},
                        "description": "创建成功",
                    },
                    {
                        "kind": "json_path",
                        "target": "$.name",
                        "operator": "eq",
                        "expected": "MYMAC_APPLY_1",
                        "status": "confirmed",
                        "evidence": {"source_type": "openapi", "ref": "endpoint:1"},
                        "description": "名称一致",
                    },
                ],
            }
        ],
    }
    runtime.run_case(case)
    payload = json.loads((tmp_path / "results" / "case_123.json").read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["output"]["assertions"][0]["actual"] == 200
    assert payload["output"]["assertions"][1]["actual"] == "MYMAC_APPLY_1"
    assert payload["output"]["assertions"][0]["step_index"] == 0
    assert payload["logs"].startswith("用例通过：2 条断言通过")


def test_runtime_timing_samples_use_p95(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    durations = iter([10.0, 20.0, 30.0, 40.0, 50.0])

    def fake_request(step, ctx):
        ctx.last_duration_ms = next(durations)
        ctx.requests.append({"method": "GET", "url": "http://example.test/api/x"})
        ctx.responses.append({"status_code": 200, "body": {}, "duration_ms": ctx.last_duration_ms})
        return ctx.responses[-1]

    monkeypatch.setattr(runtime, "_perform_request", fake_request)
    case = {
        "id": "perf-sample",
        "title": "P95 采样",
        "steps": [
            {
                "action": "request",
                "method": "GET",
                "path": "/api/x",
                "samples": 5,
                "assert": [
                    {
                        "kind": "timing",
                        "operator": "le",
                        "expected": 100,
                        "threshold_ms": 100,
                        "status": "confirmed",
                        "evidence": {"source_type": "manual", "ref": "manual"},
                    }
                ],
            }
        ],
    }
    runtime.run_case(case)
    payload = json.loads((tmp_path / "results" / "case_perf-sample.json").read_text(encoding="utf-8"))
    assertion = payload["output"]["assertions"][0]
    assert assertion["passed"] is True
    assert assertion["actual"] == 48.0
    assert assertion["metrics"]["samples"] == 5
    assert assertion["metrics"]["p95"] == 48.0


def test_runtime_top_level_request_points_to_failed_step_not_cleanup(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(runtime, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(runtime, "ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    def fake_request(step, ctx):
        request = {
            "method": step.get("method"),
            "url": "http://example.test" + str(step.get("path")),
            "body": step.get("body"),
        }
        response = {"status_code": 422, "body": {"error": {"message": "bad"}}, "duration_ms": 1.0}
        ctx.requests.append(request)
        ctx.responses.append(response)
        return response

    monkeypatch.setattr(runtime, "_perform_request", fake_request)
    case = {
        "id": "cleanup-repro",
        "title": "失败请求不能被 cleanup 覆盖",
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/api/things",
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 201,
                        "status": "confirmed",
                        "evidence": {"source_type": "openapi", "ref": "x"},
                    }
                ],
            }
        ],
        "cleanup": [{"action": "request", "method": "DELETE", "path": "/api/things/1"}],
    }
    with pytest.raises(AssertionError):
        runtime.run_case(case)
    payload = json.loads((tmp_path / "results" / "case_cleanup-repro.json").read_text(encoding="utf-8"))
    assert payload["request"]["method"] == "POST"
    assert payload["request"]["url"].endswith("/api/things")
    assert payload["output"]["failed_step_index"] == 0


def test_runtime_blocks_findings_mutation_on_default_project():
    ctx = runtime.CaseContext({"id": "1"})
    ctx.variables["project_id"] = "11"
    ctx.default_variables.add("project_id")
    with pytest.raises(RuntimeError, match="默认项目"):
        runtime._perform_request(
            {
                "action": "request",
                "method": "POST",
                "path": "/api/projects/{{project_id}}/findings/batch-status",
                "body": {"finding_ids": [1], "status": "confirmed"},
            },
            ctx,
        )


def test_runtime_rejects_unresolved_path_placeholder():
    """上游 save 失败时，不应把 {{created_name}} 原样发给被测系统。"""
    ctx = runtime.CaseContext({"id": "1"})
    with pytest.raises(RuntimeError, match="created_name"):
        runtime._perform_request(
            {"action": "request", "method": "GET", "path": "/api/env/vars/{{created_name}}"},
            ctx,
        )


def test_collect_assertions_merges_steps_and_dedupes_top_level():
    spec = {
        "steps": [
            {"action": "request", "assert": [{"kind": "status_code", "operator": "eq", "expected": 200}]},
            {
                "action": "request",
                "assert": [{"kind": "json_path", "target": "$.id", "operator": "eq", "expected": 7}],
            },
        ],
        "assertions": [
            # 与步骤 0 重复：应被去掉
            {"kind": "status_code", "operator": "eq", "expected": 200, "description": "另一个描述"},
            # 步骤内没有：保留为用例级断言
            {"kind": "json_path", "target": "$.ok", "operator": "eq", "expected": True},
        ],
    }
    assertions = collect_assertions(spec)
    assert len(assertions) == 3
    assert assertions[0]["step_index"] == 0
    assert assertions[1]["step_index"] == 1
    assert assertions[2]["target"] == "$.ok"
    assert "step_index" not in assertions[2]


def test_rerun_attempt_uses_distinct_run_token():
    env = Environment(base_url="http://127.0.0.1:8888", accounts={})
    run = SimpleNamespace(id=15)
    first = _env_for_run(env, run, "/tmp/artifacts", attempt=0)
    retry = _env_for_run(env, run, "/tmp/artifacts", attempt=1)
    assert first["SHAKEDOWN_RUN_ID"] == "15"
    assert retry["SHAKEDOWN_RUN_ID"] == "15-r1"
    assert first["SHAKEDOWN_RUN_ID"] != retry["SHAKEDOWN_RUN_ID"]


def test_bounded_request_caps_event_stream(monkeypatch):
    import httpx

    def handler(request):  # noqa: ARG001
        def chunks():
            for _ in range(10000):
                yield b"data: tick\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    monkeypatch.setattr(runtime, "STREAM_MAX_BYTES", 32)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = runtime._bounded_request(
            client,
            "GET",
            "http://sut/stream",
            headers={},
            params=None,
            json_body=None,
            timeout=1,
        )
    assert response.status_code == 200
    assert "data: tick" in response.text
    assert len(response.text) <= 64


def test_bounded_request_reads_normal_json():
    import httpx

    def handler(request):  # noqa: ARG001
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = runtime._bounded_request(
            client,
            "GET",
            "http://sut/api",
            headers={},
            params=None,
            json_body=None,
            timeout=1,
        )
    assert response.json() == {"ok": True}


def test_strip_nuls_handles_binary_remnants():
    from app.executor.runner import _strip_nuls

    payload = {"body": "abc\x00def", "nested": [{"x": "1\x002"}]}
    cleaned = _strip_nuls(payload)
    assert "\x00" not in str(cleaned)
    assert cleaned["body"] == "abcdef"
    assert cleaned["nested"][0]["x"] == "12"


def test_local_sandbox_cancel_kills_pytest():
    from app.sandbox.base import SandboxRequest
    from app.sandbox.local import LocalSandbox

    files = {
        "test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(30)\n",
    }
    request = SandboxRequest(
        files=files,
        timeout=60,
        task_id="cancel-test",
        should_cancel=lambda: True,
    )
    result = LocalSandbox().run(request)
    assert result.ok is False
    assert "取消" in result.error
    assert result.raw.get("cancelled") is True
