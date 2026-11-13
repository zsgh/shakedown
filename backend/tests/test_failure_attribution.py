"""确定性失败归因与用例规格修复的单元测试。"""
from __future__ import annotations

from app.failure_attribution import (
    CATEGORY_DEFECT,
    CATEGORY_ENVIRONMENT,
    CATEGORY_EVIDENCE,
    CATEGORY_PLATFORM,
    CATEGORY_UNCONFIRMED,
    classify,
    classify_result,
    classify_static,
)
from app.models import CaseResult, TestCase as CaseModel
from app.spec_repair import repair_spec


def _case(spec: dict, **kwargs) -> CaseModel:
    steps = spec.get("steps") or []
    return CaseModel(
        project_id=1,
        feature_id=1,
        title=kwargs.pop("title", "测试用例"),
        dimension=kwargs.pop("dimension", "functional"),
        case_type="api",
        steps=steps,
        spec=spec,
        expected={"assertions": []},
        status=kwargs.pop("status", "pending"),
        tags=kwargs.pop("tags", []),
        missing_input=kwargs.pop("missing_input", None),
        **kwargs,
    )


def _result(assertions: list[dict], **kwargs) -> CaseResult:
    return CaseResult(
        project_id=1,
        run_id=1,
        case_id=1,
        status=kwargs.pop("status", "fail"),
        request=kwargs.pop("request", {}),
        response=kwargs.pop("response", {}),
        output={"assertions": assertions, "errors": kwargs.pop("errors", [])},
        logs=kwargs.pop("logs", ""),
    )


def _step(assertion: dict, action: str = "request") -> dict:
    return {"action": action, "assert": [assertion]}


def test_command_step_with_text_status_assertion_is_platform_gap():
    assertion = {
        "kind": "status_code",
        "operator": "contains",
        "expected": "ARCHIVE_NAME_OK",
        "actual": 0,
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    spec = {"steps": [_step(assertion, "command")]}
    verdict = classify(_case(spec), _result([assertion]))
    assert verdict["category"] == CATEGORY_PLATFORM
    assert verdict["reason_code"] == "command_assertion_kind"
    assert verdict["fixable"] is True


def test_header_location_assertion_is_platform_gap():
    assertion = {
        "kind": "text_contains",
        "target": "headers.location",
        "operator": "contains",
        "expected": "/admin/backup",
        "actual": "",
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    spec = {"steps": [_step(assertion)]}
    verdict = classify(_case(spec), _result([assertion]))
    assert verdict["category"] == CATEGORY_PLATFORM
    assert verdict["reason_code"] == "header_target_unsupported"


def test_jsonpath_filter_is_platform_gap():
    assertion = {
        "kind": "json_path",
        "target": "$[?(@.name=='bot')].id",
        "operator": "exists",
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    spec = {"steps": [_step(assertion)]}
    verdict = classify(_case(spec), _result([assertion]))
    assert verdict["category"] == CATEGORY_PLATFORM
    assert verdict["reason_code"] == "jsonpath_filter_unsupported"


def test_5xx_is_defect():
    assertion = {"kind": "status_code", "expected": 200, "actual": 500, "passed": False, "step_index": 0}
    spec = {"steps": [_step(assertion)]}
    verdict = classify_result(_case(spec), _result([assertion], response={"status_code": 500}))
    assert verdict is not None
    assert verdict["category"] == CATEGORY_DEFECT
    assert verdict["is_defect"] is True


def test_connection_refused_is_environment():
    verdict = classify_result(
        _case({"steps": [{"action": "request"}]}),
        _result([], status="error", errors=["httpx.ConnectError: [Errno 61] Connection refused"]),
    )
    assert verdict is not None
    assert verdict["category"] == CATEGORY_ENVIRONMENT
    assert verdict["reason_code"] == "connection_refused"


def test_disabled_case_with_missing_contract_is_evidence():
    case = _case(
        {"steps": [{"action": "request"}]},
        status="disabled",
        tags=["missing-input", "unsupported"],
        missing_input="缺少 backup.intervalHours 的取值下限与配置加载方式",
    )
    verdict = classify_static(case)
    assert verdict["category"] == CATEGORY_EVIDENCE
    assert verdict["reason_code"] == "missing_contract"
    assert "intervalHours" in verdict["reason"]


def test_unclassified_failure_stays_unconfirmed():
    assertion = {"kind": "status_code", "expected": 200, "actual": 404, "passed": False, "step_index": 0}
    spec = {"steps": [_step(assertion)]}
    verdict = classify(_case(spec), _result([assertion]))
    assert verdict["category"] == CATEGORY_UNCONFIRMED
    assert verdict["reason_code"] == "unclassified"


def test_repair_command_assertion_kind():
    spec = {
        "steps": [
            {
                "action": "command",
                "assert": [
                    {"kind": "status_code", "operator": "contains", "expected": "DONE"},
                    {"kind": "status_code", "operator": "eq", "expected": 0},
                    {"kind": "text_contains", "expected": "ok"},
                ],
            }
        ]
    }
    changes = repair_spec(spec)
    kinds = [item["kind"] for item in spec["steps"][0]["assert"]]
    assert kinds == ["stdout_contains", "exit_code", "stdout_contains"]
    assert len(changes) == 3
    assert spec["steps"][0]["assert"][0]["kind_before"] == "status_code"


def test_repair_request_step_command_assertion():
    spec = {
        "steps": [
            {
                "action": "request",
                "assert": [
                    {"kind": "exit_code", "expected": 200},
                    {"kind": "stdout_contains", "expected": "ok"},
                ],
            }
        ]
    }
    repair_spec(spec)
    kinds = [item["kind"] for item in spec["steps"][0]["assert"]]
    assert kinds == ["status_code", "text_contains"]


def test_wrong_output_stream_is_platform_gap():
    assertion = {
        "kind": "stdout_contains",
        "operator": "contains",
        "expected": "flag needs an argument",
        "actual": "RC=2",
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    spec = {"steps": [_step(assertion, "command")]}
    result = _result([assertion])
    result.output = {
        "assertions": [assertion],
        "commands": [
            {
                "step_index": 0,
                "stdout": "RC=2\n",
                "stderr": "flag needs an argument: -restore\n",
                "exit_code": 2,
            }
        ],
    }
    verdict = classify(_case(spec), result)
    assert verdict["category"] == CATEGORY_PLATFORM
    assert verdict["reason_code"] == "command_assertion_stream"
    assert verdict["fixable"] is True

    changes = repair_spec(spec, result.output["commands"])
    assert spec["steps"][0]["assert"][0]["kind"] == "stderr_contains"
    assert len(changes) == 1


def test_open_redirect_is_defect():
    assertion = {
        "kind": "text_contains",
        "target": "headers.location",
        "target_kind": "header",
        "operator": "not_contains",
        "expected": "//",
        "actual": "//evil.example/phish",
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    spec = {
        "steps": [
            {
                "action": "request",
                "method": "POST",
                "path": "/login",
                "assert": [assertion],
            }
        ]
    }
    result = _result(
        [assertion],
        response={"status_code": 303, "headers": {"Location": "//evil.example/phish"}, "body": ""},
    )
    verdict = classify(_case(spec, dimension="security"), result)
    assert verdict["category"] == CATEGORY_DEFECT
    assert verdict["reason_code"] == "open_redirect"
    assert verdict["auto_confirm"] is True


def test_policy_assertion_failure_is_defect():
    assertion = {
        "kind": "status_code",
        "expected": 401,
        "actual": 200,
        "passed": False,
        "pending": False,
        "step_index": 0,
        "detail": "实际状态码 200，期望 401",
        "evidence": {"source_type": "policy", "ref": "SEC-SESSION-REVOKE"},
    }
    spec = {"steps": [_step(assertion)]}
    verdict = classify(_case(spec, dimension="security"), _result([assertion]))
    assert verdict["category"] == CATEGORY_DEFECT
    assert verdict["reason_code"] == "policy_violation"
    assert verdict["auto_confirm"] is True


def test_status_in_list_is_not_platform_gap():
    assertion = {
        "kind": "status_code",
        "operator": "in",
        "expected": [423, 429],
        "actual": 200,
        "passed": False,
        "pending": False,
        "step_index": 0,
        "evidence": {"source_type": "policy", "ref": "SEC-RATE-LIMIT"},
    }
    spec = {"steps": [_step(assertion)]}
    verdict = classify(_case(spec), _result([assertion]))
    assert verdict["category"] == CATEGORY_DEFECT
    assert verdict["reason_code"] == "policy_violation"
