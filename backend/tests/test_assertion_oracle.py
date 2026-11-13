"""断言分层：无依据不生成断言、存量迁移、待确认问题与风险覆盖。"""
from __future__ import annotations

from app.executor.case_schema import (
    collect_assertions,
    collect_contract_questions,
    split_unbacked_assertions,
)
from app.models import Endpoint, HumanFeedback, TestCase as CaseModel
from app.policies import policy_assertion
from app.risk_coverage import RISK_CATEGORIES


def _case(project_id: int, title: str, *, spec: dict, dimension: str = "functional") -> CaseModel:
    return CaseModel(
        project_id=project_id,
        title=title,
        dimension=dimension,
        case_type="api",
        steps=spec.get("steps") or [],
        spec=spec,
        expected={"assertions": []},
        status="enabled",
    )


def test_split_unbacked_assertions_moves_guesses_to_questions():
    spec = {
        "steps": [
            {
                "action": "request",
                "method": "GET",
                "path": "/api/items",
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "status": "confirmed",
                        "evidence": {"source_type": "openapi", "ref": "GET /api/items"},
                    },
                    {
                        "kind": "json_path",
                        "target": "$.error.code",
                        "operator": "eq",
                        "expected": "TASK_NOT_FOUND",
                        "status": "pending",
                        "evidence": {"source_type": "none"},
                    },
                    {
                        "kind": "status_code",
                        "operator": "not_in",
                        "expected": [500, 501, 502, 503, 504],
                        "status": "confirmed",
                        "evidence": {"source_type": "policy", "ref": "POLICY-REL-001"},
                    },
                    {
                        "kind": "json_path",
                        "target": "$.note",
                        "operator": "exists",
                        "expected": True,
                        "status": "pending",
                        "evidence": {"source_type": "manual", "note": "作者：qa；理由：接口约定"},
                    },
                ],
            }
        ],
        "assertions": [],
    }

    moved = split_unbacked_assertions(spec)

    assert len(moved) == 1
    assert moved[0]["expected"] == "TASK_NOT_FOUND"
    targets = {item.get("target") for item in collect_assertions(spec)}
    assert "$.error.code" not in targets
    assert "$.note" in targets  # 带作者与理由的 manual 假设保留为 pending
    assert any(item["ref"].startswith("POLICY-") for item in (a["evidence"] for a in collect_assertions(spec)))
    questions = collect_contract_questions(spec)
    assert len(questions) == 1
    assert "$.error.code" in questions[0]["question"]
    assert "expected" not in questions[0]


def test_assertion_migration_moves_policy_and_questions(client, session, sample_project):
    policy_case = _case(
        sample_project.id,
        "错误响应健壮性",
        spec={
            "steps": [
                {
                    "action": "request",
                    "method": "GET",
                    "path": "/api/items/404",
                    "assert": [
                        {
                            "kind": "status_code",
                            "operator": "eq",
                            "expected": 500,
                            "status": "pending",
                            "description": "错误响应不得返回 5xx；文档未定义",
                            "evidence": {"source_type": "none"},
                        }
                    ],
                }
            ],
            "assertions": [],
        },
    )
    question_case = _case(
        sample_project.id,
        "跨项目读取任务详情",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/tasks/1"}],
            "assertions": [
                {
                    "kind": "status_code",
                    "operator": "eq",
                    "expected": 403,
                    "status": "pending",
                    "description": "跨项目读取任务详情应被拒绝；文档未定义",
                    "evidence": {"source_type": "manual"},
                }
            ],
        },
    )
    session.add_all([policy_case, question_case])
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/assertions/migrate").json()
    assert preview["affected_cases"] == 2
    assert preview["to_policy"] == 1
    assert preview["to_question"] == 1

    applied = client.post(
        f"/api/projects/{sample_project.id}/cases/assertions/migrate",
        json={"dry_run": False, "actor": "qa-bot", "note": "清理无依据断言"},
    ).json()
    assert applied["applied"] == 2
    assert applied["to_policy"] == 1
    assert applied["to_question"] == 1

    session.expire_all()
    policy_row = session.get(CaseModel, policy_case.id)
    assert collect_contract_questions(policy_row.spec) == []
    policy_assertions = collect_assertions(policy_row.spec)
    assert len(policy_assertions) == 1
    assert policy_assertions[0]["evidence"]["source_type"] == "policy"
    assert policy_assertions[0]["evidence"]["ref"].startswith("POLICY-REL-001")

    question_row = session.get(CaseModel, question_case.id)
    assert collect_assertions(question_row.spec) == []
    questions = collect_contract_questions(question_row.spec)
    assert len(questions) == 1
    assert questions[0]["owner_kind"] == "sut-contract"

    assert session.query(HumanFeedback).filter(HumanFeedback.action == "assertion_migration").count() == 2

    gaps = client.get(f"/api/projects/{sample_project.id}/case-gaps?kind=questions").json()
    ids = {item["case_id"] for item in gaps["items"]}
    assert question_case.id in ids
    row = next(item for item in gaps["items"] if item["case_id"] == question_case.id)
    assert row["pending_assertions"] == []
    assert row["question_count"] == 1
    assert row["owner_kind"] == "sut-contract"

    report = client.get(
        f"/api/projects/{sample_project.id}/case-gaps/export.md?owner_kind=sut-contract"
    ).text
    assert "跨项目读取任务详情应被拒绝" in report


def test_risk_coverage_is_separate_from_contract_coverage(client, session, sample_project):
    security_case = _case(
        sample_project.id,
        "[安全] 未鉴权访问用例列表",
        dimension="security",
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/cases"}], "assertions": []},
    )
    session.add(security_case)
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/coverage").json()
    assert "modules" in body and "coverage_rate" in body
    risk = body["risk"]
    assert risk["total"] == len(RISK_CATEGORIES)
    auth = next(row for row in risk["categories"] if row["key"] == "auth")
    assert auth["covered"] is True
    assert security_case.id in auth["examples"]


def _endpoint(project_id: int, method: str, path: str, *, params: dict | None = None, security: dict | None = None) -> Endpoint:
    return Endpoint(
        project_id=project_id,
        method=method,
        path=path,
        summary="",
        params=params or {},
        request_body={},
        responses={},
        security=security or {},
        examples={},
    )


def test_generic_probes_do_not_require_docs(client, session, sample_project):
    """文档没写 schema / 鉴权时，也能发起越权、注入、分页类 probe，只挂策略检查。"""
    with_param = _endpoint(
        sample_project.id, "GET", "/api/tasks/{task_id}", security={"BearerAuth": []}
    )
    bare_get = _endpoint(sample_project.id, "GET", "/api/tasks", params={"query": [{"name": "limit"}]})
    session.add_all([with_param, bare_get])
    session.commit()

    preview = client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": True}).json()
    assert preview["dry_run"] is True
    assert preview["planned"] == 5  # 路径参数接口 3 条（鉴权 / 注入 / 穿越）+ 查询接口 2 条（注入 / 分页）
    assert {item["tag"] for item in preview["cases"]} == {"missing_auth", "injection", "path_traversal", "pagination"}
    assert session.query(CaseModel).filter(CaseModel.generation == "probe").count() == 0

    created = client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": False}).json()
    assert created["created"] == 5

    probes = session.query(CaseModel).filter(CaseModel.generation == "probe").all()
    assert len(probes) == 5
    for case in probes:
        assert case.status == "pending"
        assert case.spec["probe"] is True
        assert "probe" in case.tags
        assertions = collect_assertions(case.spec)
        assert assertions
        assert all(a["evidence"]["source_type"] == "policy" for a in assertions)
        assert all(a["status"] == "confirmed" for a in assertions)

    again = client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": True}).json()
    assert again["planned"] == 0
    assert again["skipped"] == 5


def test_coverage_reports_oracle_mix(client, session, sample_project):
    contract_case = _case(
        sample_project.id,
        "契约断言用例",
        spec={
            "steps": [
                {
                    "action": "request",
                    "method": "GET",
                    "path": "/api/items",
                    "assert": [
                        {
                            "kind": "status_code",
                            "operator": "eq",
                            "expected": 200,
                            "status": "confirmed",
                            "evidence": {"source_type": "openapi", "ref": "GET /api/items"},
                        }
                    ],
                }
            ],
            "assertions": [],
        },
    )
    probe_case = _case(
        sample_project.id,
        "[Probe] GET /api/items · 注入型参数",
        spec={
            "probe": True,
            "steps": [
                {
                    "action": "request",
                    "method": "GET",
                    "path": "/api/items",
                    "assert": [
                        {
                            "kind": "status_code",
                            "operator": "not_in",
                            "expected": [500, 501, 502, 503, 504],
                            "status": "confirmed",
                            "evidence": {"source_type": "policy", "ref": "POLICY-REL-001@v1"},
                        }
                    ],
                }
            ],
            "assertions": [],
        },
    )
    probe_case.tags = ["probe", "injection"]
    session.add_all([contract_case, probe_case])
    session.commit()

    oracles = client.get(f"/api/projects/{sample_project.id}/coverage").json()["oracles"]
    assert oracles["assertions"] == 2
    assert oracles["by_source"] == {"openapi": 1, "policy": 1}
    assert oracles["oracle_mix"]["contract"] == 1
    assert oracles["oracle_mix"]["policy"] == 1
    assert oracles["probe_cases"] == 1
    assert oracles["unbacked_assertions"] == 0
    assert oracles["avg_assertions_per_case"] == 1.0


def test_boundary_and_fuzz_probes_cover_request_body(client, session, sample_project):
    """文档没写请求体约束时，也能塞空值 / 类型混淆 / Unicode 超长值观察健壮性。"""
    body_endpoint = Endpoint(
        project_id=sample_project.id,
        method="POST",
        path="/api/tasks",
        summary="",
        params={},
        request_body={
            "schema": {
                "properties": {
                    "title": {"type": "string"},
                    "count": {"type": "integer"},
                }
            }
        },
        responses={},
        security={},
        examples={},
    )
    session.add(body_endpoint)
    session.commit()

    preview = client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": True}).json()
    assert preview["planned"] == 3
    assert [item["tag"] for item in preview["cases"]] == ["injection", "boundary", "fuzz"]
    assert {item["dimension"] for item in preview["cases"]} == {"security", "boundary"}

    client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": False})
    by_tag = {}
    rows = session.query(CaseModel).filter(
        CaseModel.project_id == sample_project.id, CaseModel.generation == "probe"
    ).all()
    for case in rows:
        by_tag[case.tags[1]] = case
        assert {a["evidence"]["ref"] for a in collect_assertions(case.spec)} == {
            "POLICY-REL-001@v1",
            "POLICY-SEC-001@v1",
            "POLICY-SEC-002@v1",
        }
    boundary_body = by_tag["boundary"].spec["steps"][0]["body"]
    assert boundary_body == {"title": "", "count": "not-a-number"}
    fuzz_body = by_tag["fuzz"].spec["steps"][0]["body"]
    assert fuzz_body["title"].startswith("😀") and len(fuzz_body["title"]) > 400
    assert fuzz_body["count"] == "not-a-number"
    assert collect_assertions(by_tag["fuzz"].spec)
    assert all(a["evidence"]["source_type"] == "policy" for a in collect_assertions(by_tag["fuzz"].spec))


def test_missing_auth_probe_uses_auth_policy(client, session, sample_project):
    endpoint = Endpoint(
        project_id=sample_project.id,
        method="GET",
        path="/api/secrets",
        summary="",
        params={},
        request_body={},
        responses={},
        security={"BearerAuth": []},
        examples={},
    )
    session.add(endpoint)
    session.commit()

    client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": False})
    case = session.query(CaseModel).filter(
        CaseModel.project_id == sample_project.id, CaseModel.generation == "probe"
    ).one()
    refs = {a["evidence"]["ref"] for a in collect_assertions(case.spec)}
    assert "POLICY-AUTH-001@v1" in refs
    assert "POLICY-REL-001@v1" in refs
    assert case.tags == ["probe", "missing_auth"]


def test_auth_policy_covers_all_success_status_codes():
    """204 不能漏：匿名请求返回 204 也是成功响应，必须判失败。"""
    expected = policy_assertion("POLICY-AUTH-001")["expected"]
    assert 204 in expected
    for code in (200, 201, 202, 203, 204, 205, 206, 207, 208, 226):
        assert code in expected


def test_rel_policy_covers_all_server_error_status_codes():
    """5xx 策略不能只覆盖 500-504，505-511 也是服务端错误。"""
    expected = policy_assertion("POLICY-REL-001")["expected"]
    for code in (500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511):
        assert code in expected


def test_fuzz_probe_not_truncated_for_secured_body_endpoint(client, session, sample_project):
    """鉴权 + 路径参数 + 请求体的写接口必须同时拿到 boundary 与 fuzz。"""
    endpoint = Endpoint(
        project_id=sample_project.id,
        method="POST",
        path="/api/projects/{project_id}/tasks",
        summary="",
        params={},
        request_body={"schema": {"properties": {"title": {"type": "string"}}}},
        responses={},
        security={"BearerAuth": []},
        examples={},
    )
    session.add(endpoint)
    session.commit()

    preview = client.post(f"/api/projects/{sample_project.id}/probes/generate", json={"dry_run": True}).json()
    assert preview["planned"] == 5
    assert [item["tag"] for item in preview["cases"]] == [
        "missing_auth",
        "injection",
        "path_traversal",
        "boundary",
        "fuzz",
    ]


def test_probe_only_policy_is_not_offered_to_generation():
    from app.policies import policy_catalog

    ids = {item["id"] for item in policy_catalog()}
    assert "POLICY-AUTH-001" not in ids
    assert {"POLICY-REL-001", "POLICY-SEC-001", "POLICY-SEC-002"} <= ids


def test_observed_schema_drift_aggregates_by_endpoint_template(client, session, sample_project):
    """真实 URL 必须按 Endpoint 模板聚合，否则每个 id 各成一组，永远形成不了基线。"""
    from app.models import CaseResult, Run

    session.add(
        Endpoint(
            project_id=sample_project.id,
            method="GET",
            path="/api/tasks/{task_id}",
            summary="",
            params={},
            request_body={},
            responses={},
            security={},
            examples={},
        )
    )
    case = _case(sample_project.id, "响应结构观测", spec={"steps": [], "assertions": []})
    run = Run(project_id=sample_project.id, status="succeeded")
    session.add_all([case, run])
    session.commit()

    def _result(body: dict, task_id: int, *, status_code: int = 200) -> CaseResult:
        return CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "GET", "url": f"http://target.test/api/tasks/{task_id}"},
            response={"status_code": status_code, "body": body},
        )

    session.add_all(
        [
            _result({"id": 1, "title": "a"}, 81),
            _result({"id": 2, "title": "b"}, 199),
            _result({"id": "3", "title": "c", "extra": True}, 283),
        ]
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/observed/schema-drift").json()
    assert body["endpoints"] == 1
    assert body["compared"] == 1
    assert body["drift_count"] == 1
    row = body["drift"][0]
    assert row["endpoint"] == "GET /api/tasks/{task_id}"
    assert row["samples"] == 3
    assert row["added"] == ["extra"]
    assert row["type_changed"] == [{"field": "id", "from": "number", "to": "string"}]
    assert row["status_code"] == 200


def test_observed_schema_drift_compares_latest_sample_only(client, session, sample_project):
    """对比「最新一条 vs 上一条」，历史出现过的字段不应永久掩盖新增字段。"""
    from app.models import CaseResult, Run

    session.add(
        Endpoint(
            project_id=sample_project.id,
            method="GET",
            path="/api/tasks/{task_id}",
            summary="",
            params={},
            request_body={},
            responses={},
            security={},
            examples={},
        )
    )
    case = _case(sample_project.id, "最新样本观测", spec={"steps": [], "assertions": []})
    run = Run(project_id=sample_project.id, status="succeeded")
    session.add_all([case, run])
    session.commit()

    def _result(body: dict, task_id: int) -> CaseResult:
        return CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "GET", "url": f"http://target.test/api/tasks/{task_id}"},
            response={"status_code": 200, "body": body},
        )

    session.add_all(
        [
            _result({"id": 1, "legacy": True}, 1),
            _result({"id": 2, "title": "b"}, 2),
            _result({"id": 3}, 3),
        ]
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/observed/schema-drift").json()
    row = next(item for item in body["drift"] if item["endpoint"] == "GET /api/tasks/{task_id}")
    assert row["removed"] == ["title"]
    assert row["added"] == []  # legacy 不在上一条样本里，不应算新增


def test_observed_schema_drift_separates_status_codes(client, session, sample_project):
    """200 与 201 的响应结构不同是正常的，不能互相比较。"""
    from app.models import CaseResult, Run

    session.add(
        Endpoint(
            project_id=sample_project.id,
            method="POST",
            path="/api/tasks",
            summary="",
            params={},
            request_body={},
            responses={},
            security={},
            examples={},
        )
    )
    case = _case(sample_project.id, "创建任务观测", spec={"steps": [], "assertions": []})
    run = Run(project_id=sample_project.id, status="succeeded")
    session.add_all([case, run])
    session.commit()

    def _result(body: dict, status_code: int) -> CaseResult:
        return CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "POST", "url": "http://target.test/api/tasks"},
            response={"status_code": status_code, "body": body},
        )

    session.add_all([_result({"id": 1}, 201), _result({"id": 1, "title": "a"}, 200)])
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/observed/schema-drift").json()
    assert body["drift_count"] == 0
    assert body["compared"] == 0
    assert "暂无法对比" in body["message"]


def test_observed_schema_drift_needs_baseline(client, session, sample_project):
    from app.models import CaseResult, Run

    case = _case(sample_project.id, "单次观测", spec={"steps": [], "assertions": []})
    run = Run(project_id=sample_project.id, status="succeeded")
    session.add_all([case, run])
    session.commit()
    session.add(
        CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "GET", "url": "http://target.test/api/tasks/1"},
            response={"status_code": 200, "body": {"id": 1}},
        )
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/observed/schema-drift").json()
    assert body["drift_count"] == 0
    assert body["message"]


def _observed_case(session, project_id: int):
    from app.models import Run

    case = _case(project_id, "观测窗口", spec={"steps": [], "assertions": []})
    run = Run(project_id=project_id, status="succeeded")
    session.add_all([case, run])
    session.commit()
    return case, run


def test_observed_schema_drift_keeps_latest_results(session, sample_project):
    """窗口必须是最近 N 条，而不是最早 N 条。"""
    from app.models import CaseResult
    from app.observed_schema import observed_schema_drift

    session.add(
        Endpoint(
            project_id=sample_project.id,
            method="GET",
            path="/api/items/{item_id}",
            summary="",
            params={},
            request_body={},
            responses={},
            security={},
            examples={},
        )
    )
    case, run = _observed_case(session, sample_project.id)

    def _result(body: dict, item_id: int) -> CaseResult:
        return CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "GET", "url": f"http://target.test/api/items/{item_id}"},
            response={"status_code": 200, "body": body},
        )

    session.add_all([_result({"a": 1}, 1), _result({"a": 1, "b": 2}, 2), _result({"a": 1, "b": 2, "c": 3}, 3)])
    session.commit()

    body = observed_schema_drift(session, sample_project, max_results=2)
    row = next(item for item in body["drift"] if item["endpoint"] == "GET /api/items/{item_id}")
    assert row["samples"] == 2
    assert row["added"] == ["c"]  # 取最早两条会得到 "b"


def test_observed_schema_drift_reports_root_type_change(session, sample_project):
    from app.models import CaseResult
    from app.observed_schema import observed_schema_drift

    session.add(
        Endpoint(
            project_id=sample_project.id,
            method="GET",
            path="/api/items",
            summary="",
            params={},
            request_body={},
            responses={},
            security={},
            examples={},
        )
    )
    case, run = _observed_case(session, sample_project.id)

    def _result(body) -> CaseResult:
        return CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="passed",
            request={"method": "GET", "url": "http://target.test/api/items"},
            response={"status_code": 200, "body": body},
        )

    session.add_all([_result([{"id": 1}]), _result({"id": 1})])
    session.commit()

    body = observed_schema_drift(session, sample_project)
    row = next(item for item in body["drift"] if item["endpoint"] == "GET /api/items")
    assert row["root_changed"] == {"from": "array", "to": "object"}
