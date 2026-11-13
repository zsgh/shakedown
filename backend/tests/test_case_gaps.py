"""契约缺口聚合：待确认断言 / 无步骤 / 待补全说明。"""
from __future__ import annotations

from app.models import Feature, TestCase as CaseModel


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


def test_case_gaps_aggregate_and_filter(client, session, sample_project):
    feature = Feature(project_id=sample_project.id, name="项目概览")
    session.add(feature)
    session.flush()

    pending_case = _case(
        sample_project.id,
        "[正常流] 概览返回 200",
        feature_id=feature.id,
        missing_input="OpenAPI 未定义 200 响应体结构",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/projects/1/overview"}],
            "assertions": [],
        },
    )
    # 步骤内联断言：一条已确认、一条待确认
    pending_case.spec["steps"][0]["assert"] = [
        {
            "kind": "status_code",
            "operator": "eq",
            "expected": 200,
            "status": "confirmed",
            "evidence": {"source_type": "openapi", "ref": "openapi:1"},
        },
        {
            "kind": "text_contains",
            "operator": "contains",
            "expected": "项目名",
            "status": "pending",
            "evidence": {"source_type": "openapi", "ref": "openapi:1", "quote": "200:[]"},
            "description": "概览包含项目名（推测）",
        },
    ]
    no_steps = _case(sample_project.id, "缺少 SLA 的性能用例", missing_input="未提供 SLA", spec={})
    clean = _case(
        sample_project.id,
        "已确认的正常用例",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/health"}],
            "assertions": [
                {
                    "kind": "status_code",
                    "operator": "eq",
                    "expected": 200,
                    "status": "confirmed",
                    "evidence": {"source_type": "openapi", "ref": "openapi:2"},
                }
            ],
        },
    )
    disabled = _case(
        sample_project.id,
        "停用且待确认",
        status="disabled",
        spec={"steps": [], "assertions": [{"kind": "text_contains", "status": "pending"}]},
    )
    session.add_all([pending_case, no_steps, clean, disabled])
    session.commit()

    response = client.get(f"/api/projects/{sample_project.id}/case-gaps")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {
        "cases": 2,
        "pending_assertions": 1,
        "contract_questions": 0,
        "cases_with_questions": 0,
        "cases_with_pending": 1,
        "cases_without_steps": 1,
        "cases_with_missing_input": 2,
        "by_status": {"open": 2},
    }
    titles = {item["title"] for item in body["items"]}
    assert titles == {"[正常流] 概览返回 200", "缺少 SLA 的性能用例"}
    by_title = {item["title"]: item for item in body["items"]}
    assert by_title["[正常流] 概览返回 200"]["pending_assertions"][0]["endpoint"] == "GET /api/projects/1/overview"
    assert by_title["[正常流] 概览返回 200"]["feature_name"] == "项目概览"
    assert by_title["缺少 SLA 的性能用例"]["no_steps"] is True

    only_pending = client.get(f"/api/projects/{sample_project.id}/case-gaps?kind=pending").json()
    assert [item["title"] for item in only_pending["items"]] == ["[正常流] 概览返回 200"]

    only_no_steps = client.get(f"/api/projects/{sample_project.id}/case-gaps?kind=no_steps").json()
    assert [item["title"] for item in only_no_steps["items"]] == ["缺少 SLA 的性能用例"]


def test_case_gap_disposition_filters_closed(client, session, sample_project):
    case = _case(
        sample_project.id,
        "待确认断言",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/health"}],
            "assertions": [{"kind": "status_code", "status": "pending"}],
        },
    )
    session.add(case)
    session.commit()

    response = client.patch(
        f"/api/projects/{sample_project.id}/case-gaps/{case.id}",
        json={"status": "accepted", "owner": "team-a", "note": "已知风险，下季度处理"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"

    default = client.get(f"/api/projects/{sample_project.id}/case-gaps").json()
    assert default["items"] == []
    assert default["summary"]["by_status"] == {"accepted": 1}

    included = client.get(f"/api/projects/{sample_project.id}/case-gaps?include_closed=true").json()
    assert included["items"][0]["gap_status"] == "accepted"
    assert included["items"][0]["gap_owner"] == "team-a"


def test_case_gaps_export_markdown(client, session, sample_project):
    case = _case(
        sample_project.id,
        "概览响应断言待确认",
        missing_input="OpenAPI 未定义响应体",
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/projects/1/overview"}],
            "assertions": [
                {
                    "kind": "text_contains",
                    "expected": "项目名",
                    "status": "pending",
                    "description": "概览包含项目名（推测）",
                    "evidence": {"source_type": "openapi", "ref": "openapi:607", "quote": "200:[]"},
                }
            ],
        },
    )
    session.add(case)
    session.commit()

    response = client.get(f"/api/projects/{sample_project.id}/case-gaps/export.md")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    text = response.text
    assert "契约缺口报告" in text
    assert "概览响应断言待确认" in text
    assert "待确认断言" in text
    assert "openapi:607" in text
