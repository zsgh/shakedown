"""测试资产闭环：批量处置、SLA 重算、spec 重评估、断言复核、误报复核、主动推送。"""
from __future__ import annotations

from app.executor.case_schema import collect_assertions
from app.models import (
    CaseGapAudit,
    Endpoint,
    Feature,
    Finding,
    KnowledgeSource,
    SpecSnapshot,
    TestCase as CaseModel,
)
from app.push import deliver_webhook


def _case(project_id: int, title: str, *, status: str = "enabled", **kwargs) -> CaseModel:
    spec = kwargs.pop("spec", {"steps": [], "assertions": []})
    return CaseModel(
        project_id=project_id,
        title=title,
        dimension=kwargs.pop("dimension", "functional"),
        case_type="api",
        steps=spec.get("steps") or [],
        spec=spec,
        expected={"assertions": []},
        status=status,
        **kwargs,
    )


def test_batch_gap_disposition_with_audit_and_owner_split(client, session, sample_project):
    sla_case = _case(sample_project.id, "性能用例缺少 SLA", missing_input="没有 SLA / 性能阈值", dimension="performance")
    schema_case = _case(
        sample_project.id,
        "响应 schema 未定义",
        missing_input="OpenAPI 未定义响应 schema",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/items"}],
            "assertions": [{"kind": "status_code", "status": "pending"}],
        },
    )
    env_case = _case(sample_project.id, "环境账号缺失", missing_input="测试环境账号未提供")
    session.add_all([sla_case, schema_case, env_case])
    session.commit()

    facets = client.get(f"/api/projects/{sample_project.id}/case-gaps").json()["facets"]
    assert facets["by_missing_input_kind"]["sla"] == 1
    assert facets["by_owner_kind"]["sut-contract"] == 1
    assert facets["by_owner_kind"]["environment"] == 1

    preview = client.post(
        f"/api/projects/{sample_project.id}/case-gaps/batch",
        json={"status": "accepted", "filters": {"missing_input_kind": "sla"}},
    ).json()
    assert preview["dry_run"] is True
    assert preview["matched"] == 1

    applied = client.post(
        f"/api/projects/{sample_project.id}/case-gaps/batch",
        json={
            "status": "accepted",
            "owner": "qa-team",
            "note": "SLA 待产品确认",
            "dry_run": False,
            "filters": {"missing_input_kind": "sla"},
        },
    ).json()
    assert applied == {"dry_run": False, "matched": 1, "updated": 1, "case_ids": [sla_case.id]}

    default = client.get(f"/api/projects/{sample_project.id}/case-gaps").json()
    assert sla_case.id not in {item["case_id"] for item in default["items"]}
    included = client.get(f"/api/projects/{sample_project.id}/case-gaps?include_closed=true").json()
    row = next(item for item in included["items"] if item["case_id"] == sla_case.id)
    assert row["gap_status"] == "accepted"
    assert row["gap_owner"] == "qa-team"

    audits = client.get(f"/api/projects/{sample_project.id}/case-gaps/{sla_case.id}/audits").json()
    assert audits["items"][0]["actor"] == "local"
    assert audits["items"][0]["to_status"] == "accepted"
    assert audits["items"][0]["note"] == "SLA 待产品确认"
    assert session.query(CaseGapAudit).filter(CaseGapAudit.case_id == sla_case.id).count() == 1

    sut_report = client.get(
        f"/api/projects/{sample_project.id}/case-gaps/export.md?owner_kind=sut-contract&include_closed=true"
    ).text
    assert "响应 schema 未定义" in sut_report
    assert "性能用例缺少 SLA" not in sut_report
    assert "环境账号缺失" not in sut_report


def test_missing_input_kind_ignores_sla_for_non_performance(client, session, sample_project):
    """非性能维度即使顺带提到 SLA，也按主因归类，避免形成无法处置的死分类。"""
    schema_case = _case(
        sample_project.id,
        "创建项目 name 超长",
        missing_input="缺少 name 的 maxLength 定义，无法给出确定的超长阈值断言；无 SLA 故未生成性能基线",
    )
    param_case = _case(
        sample_project.id,
        "任务列表 limit 语义",
        missing_input="spec 未定义 query.limit 的默认值与截断语义；无 SLA 故未生成性能基线",
    )
    perf_case = _case(
        sample_project.id,
        "登录接口性能",
        dimension="performance",
        missing_input="没有 SLA，无法生成 timing 断言",
    )
    session.add_all([schema_case, param_case, perf_case])
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/case-gaps").json()
    rows = {item["case_id"]: item for item in body["items"]}
    assert rows[schema_case.id]["missing_input_kind"] == "response_schema"
    assert rows[param_case.id]["missing_input_kind"] == "api_param"
    assert rows[schema_case.id]["owner_kind"] == "sut-contract"
    assert rows[param_case.id]["owner_kind"] == "sut-contract"
    assert rows[perf_case.id]["missing_input_kind"] == "sla"

    non_perf_sla = [
        item for item in body["items"] if item["missing_input_kind"] == "sla" and item["dimension"] != "performance"
    ]
    assert non_perf_sla == []


def test_no_steps_auth_gap_reports_as_sut_contract(client, session, sample_project):
    """无步骤 + 鉴权契约缺失：测试侧和被测方都有责任，但主归属应给契约缺口。"""
    auth_case = _case(
        sample_project.id,
        "[安全] 任务详情获取 - 鉴权/越权访问覆盖缺失",
        dimension="security",
        missing_input="OpenAPI 未定义鉴权方案（securitySchemes），无法断言未鉴权访问的期望状态",
    )
    plain_case = _case(sample_project.id, "占位用例", missing_input="尚未补充执行步骤")
    session.add_all([auth_case, plain_case])
    session.commit()

    items = client.get(f"/api/projects/{sample_project.id}/case-gaps").json()["items"]
    rows = {item["case_id"]: item for item in items}
    assert rows[auth_case.id]["no_steps"] is True
    assert rows[auth_case.id]["owner_kind"] == "sut-contract"
    assert set(rows[auth_case.id]["owner_kinds"]) == {"sut-contract", "test-side"}
    # 与契约无关的无步骤用例保持测试侧归属。
    assert rows[plain_case.id]["owner_kind"] == "test-side"

    sut_report = client.get(
        f"/api/projects/{sample_project.id}/case-gaps/export.md?owner_kind=sut-contract"
    ).text
    assert auth_case.title in sut_report
    assert plain_case.title not in sut_report


def test_sla_recompute_preview_and_apply(client, session, sample_project):
    sample_project.settings_json = {"performance_sla_ms": 100}
    case = _case(
        sample_project.id,
        "登录接口性能",
        dimension="performance",
        missing_input="没有 SLA，无法生成 timing 断言",
        spec={
            "steps": [
                {
                    "action": "request",
                    "method": "POST",
                    "path": "/api/login",
                    "assert": [
                        {
                            "kind": "timing",
                            "target": "response_time_ms",
                            "operator": "lt",
                            "expected": 500,
                            "status": "confirmed",
                        }
                    ],
                }
            ]
        },
    )
    session.add(case)
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/sla-recompute").json()
    assert preview["project_sla_ms"] == 100
    assert preview["count"] == 1
    assert preview["cases"][0]["old_assertions"][0]["expected"] == 500

    applied = client.post(
        f"/api/projects/{sample_project.id}/cases/sla-recompute",
        json={"dry_run": False},
    ).json()
    assert applied["applied"] == 1

    session.refresh(case)
    assert case.missing_input is None
    assertions = collect_assertions(case.spec)
    assert assertions[0]["expected"] == 100
    assert assertions[0]["evidence"]["source_type"] == "project_config"


def test_sla_recompute_without_project_setting_is_noop(client, session, sample_project):
    case = _case(sample_project.id, "旧性能用例", dimension="performance", missing_input="未提供 SLA", spec={})
    session.add(case)
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/sla-recompute").json()
    assert preview["project_sla_ms"] is None
    assert preview["count"] == 1
    response = client.post(
        f"/api/projects/{sample_project.id}/cases/sla-recompute",
        json={"dry_run": False},
    )
    assert response.status_code == 404
    session.refresh(case)
    assert case.missing_input == "未提供 SLA"


def test_spec_reevaluate_only_touches_affected_cases(client, session, sample_project):
    source = KnowledgeSource(project_id=sample_project.id, kind="openapi", name="spec", location="/tmp/openapi.json")
    session.add(source)
    session.flush()
    endpoint = Endpoint(
        project_id=sample_project.id,
        method="GET",
        path="/api/items",
        responses={"422": {"schema": {"type": "object", "properties": {"detail": {"type": "string"}}}}},
    )
    session.add(endpoint)
    session.commit()
    snapshot = SpecSnapshot(
        project_id=sample_project.id,
        source_id=source.id,
        version="v2",
        diff={"added": [], "removed": [], "changed": [{"method": "GET", "path": "/api/items"}]},
    )
    affected = _case(
        sample_project.id,
        "items 参数校验",
        status="needs_review",
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
                            "expected": 422,
                            "status": "pending",
                            "evidence": {"source_type": "none"},
                        }
                    ],
                }
            ]
        },
    )
    untouched = _case(
        sample_project.id,
        "无关用例",
        status="needs_review",
        spec={
            "steps": [
                {
                    "action": "request",
                    "method": "GET",
                    "path": "/api/other",
                    "assert": [{"kind": "status_code", "expected": 200, "status": "pending"}],
                }
            ]
        },
    )
    session.add_all([affected, untouched, snapshot])
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/reevaluate?snapshot_id={snapshot.id}").json()
    assert preview["count"] == 1
    assert preview["cases"][0]["case_id"] == affected.id

    result = client.post(
        f"/api/projects/{sample_project.id}/cases/reevaluate",
        json={"snapshot_id": snapshot.id, "dry_run": False},
    ).json()
    assert result["processed"] == 1
    assert result["confirmed_assertions"] == 1
    assert result["unlocked_cases"] == [affected.id]

    session.refresh(affected)
    assert collect_assertions(affected.spec)[0]["status"] == "confirmed"
    assert affected.status == "enabled"
    session.refresh(untouched)
    assert collect_assertions(untouched.spec)[0]["status"] == "pending"


def test_pending_assertion_groups_and_batch_confirm(client, session, sample_project):
    feature = Feature(project_id=sample_project.id, name="订单")
    session.add(feature)
    session.flush()
    case = _case(
        sample_project.id,
        "订单详情",
        feature_id=feature.id,
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/orders/1"}],
            "assertions": [
                {
                    "kind": "status_code",
                    "expected": 200,
                    "status": "pending",
                    "evidence": {"source_type": "openapi", "ref": "openapi:orders"},
                },
                {
                    "kind": "text_contains",
                    "expected": "订单号",
                    "status": "pending",
                    "evidence": {"source_type": "none"},
                },
            ],
        },
    )
    session.add(case)
    session.commit()

    groups = client.get(
        f"/api/projects/{sample_project.id}/case-gaps/assertions/groups?group_by=endpoint"
    ).json()
    assert groups["total"] == 2
    assert groups["confirmable"] == 1
    assert groups["groups"][0]["key"] == "GET /api/orders/1"

    result = client.post(
        f"/api/projects/{sample_project.id}/case-gaps/assertions/confirm",
        json={"selections": [{"case_id": case.id, "index": 0}, {"case_id": case.id, "index": 1}]},
    ).json()
    assert result["confirmed_count"] == 1
    assert len(result["skipped"]) == 1
    session.refresh(case)
    statuses = [raw["status"] for raw in collect_assertions(case.spec)]
    assert statuses == ["confirmed", "pending"]

    risk = client.post(
        f"/api/projects/{sample_project.id}/case-gaps/assertions/accept-risk",
        json={"case_ids": [case.id], "note": "无契约依据，接受风险"},
    ).json()
    assert risk["updated"] == 1
    closed = client.get(f"/api/projects/{sample_project.id}/case-gaps?include_closed=true").json()
    row = next(item for item in closed["items"] if item["case_id"] == case.id)
    assert row["gap_status"] == "accepted"


def test_false_positive_recheck_candidates(client, session, sample_project):
    source = KnowledgeSource(project_id=sample_project.id, kind="openapi", name="spec", location="/tmp/openapi.json")
    session.add(source)
    session.flush()
    case = _case(
        sample_project.id,
        "误报用例",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/orders"}],
            "assertions": [],
        },
    )
    session.add(case)
    session.flush()
    session.add(
        Finding(
            project_id=sample_project.id,
            title="历史误报",
            status="false_positive",
            case_id=case.id,
            assignee="dev-a",
            resolution={"note": "旧 spec 未定义 200"},
        )
    )
    session.add(
        SpecSnapshot(
            project_id=sample_project.id,
            source_id=source.id,
            version="v3",
            diff={"added": [], "removed": [], "changed": [{"method": "GET", "path": "/api/orders"}]},
        )
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/findings/recheck-candidates").json()
    assert body["count"] == 1
    assert body["items"][0]["resolution_note"] == "旧 spec 未定义 200"
    assert body["items"][0]["resolution_missing"] is False


def test_webhook_digest_and_delivery(client, session, sample_project, monkeypatch):
    sample_project.settings_json = {"webhook_url": "http://example.invalid/hook"}
    session.add(
        Finding(
            project_id=sample_project.id,
            title="待修复缺陷",
            status="confirmed",
            fix_status="open",
        )
    )
    session.add(_case(sample_project.id, "缺口用例", missing_input="OpenAPI 未定义响应 schema"))
    session.commit()

    queued = client.post(f"/api/projects/{sample_project.id}/push/digest")
    assert queued.status_code == 200, queued.text
    delivery_id = queued.json()["delivery_id"]

    listing = client.get(f"/api/projects/{sample_project.id}/webhook-deliveries").json()
    assert listing["webhook_configured"] is True
    assert listing["items"][0]["event"] == "digest"
    assert listing["items"][0]["status"] == "pending"

    class _Response:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: _Response())
    result = deliver_webhook(session, delivery_id)
    assert result["status"] == "sent"
    sent = client.get(f"/api/projects/{sample_project.id}/webhook-deliveries").json()["items"][0]
    assert sent["status"] == "sent"
    assert sent["attempts"] == 1
