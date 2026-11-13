"""测试设计按用例 ID / 缺陷 ID 搜索。"""
from __future__ import annotations

from app.models import CaseResult, Finding, Run
from app.models import TestCase as CaseModel


def test_case_page_searches_case_and_finding_ids(client, session, sample_project):
    case = CaseModel(
        project_id=sample_project.id,
        title="设置页异常处理",
        dimension="functional",
        case_type="api",
        spec={"title": "设置页异常处理", "steps": [], "assertions": []},
        status="pending",
    )
    session.add(case)
    session.flush()
    finding = Finding(
        project_id=sample_project.id,
        source="whitebox",
        title="设置页异常被吞",
        description="",
        severity="medium",
        status="confirmed",
        module="web",
        risk_type="exception",
        evidence=[],
        code_location={},
        case_id=case.id,
    )
    session.add(finding)
    session.commit()

    by_case = client.get(f"/api/projects/{sample_project.id}/cases/page?q=%23{case.id}").json()
    assert by_case["total"] == 1
    assert by_case["items"][0]["id"] == case.id
    assert by_case["items"][0]["finding_ids"] == [finding.id]

    by_finding = client.get(f"/api/projects/{sample_project.id}/cases/page?q=%23{finding.id}").json()
    assert by_finding["total"] == 1
    assert by_finding["items"][0]["id"] == case.id


def test_case_page_filters_never_executed(client, session, sample_project):
    cases = [
        CaseModel(
            project_id=sample_project.id,
            title=f"execution-filter-{index}",
            dimension="functional",
            case_type="api",
            spec={"title": f"execution-filter-{index}", "steps": [], "assertions": []},
            status="enabled",
        )
        for index in range(2)
    ]
    session.add_all(cases)
    session.flush()
    run = Run(project_id=sample_project.id, trigger="manual", status="completed", stats={})
    session.add(run)
    session.flush()
    session.add(
        CaseResult(
            run_id=run.id,
            case_id=cases[0].id,
            project_id=sample_project.id,
            status="pass",
            seq=0,
        )
    )
    session.commit()

    never = client.get(
        f"/api/projects/{sample_project.id}/cases/page?status=enabled&execution_status=never"
    ).json()
    assert [item["id"] for item in never["items"]] == [cases[1].id]

    executed = client.get(
        f"/api/projects/{sample_project.id}/cases/page?status=enabled&execution_status=executed"
    ).json()
    assert [item["id"] for item in executed["items"]] == [cases[0].id]


def test_case_page_filters_multiple_features(client, session, sample_project):
    from app.models import Feature

    features = [
        Feature(project_id=sample_project.id, name=f"feature-{index}", module="m", description="")
        for index in range(2)
    ]
    session.add_all(features)
    session.flush()
    cases = [
        CaseModel(
            project_id=sample_project.id,
            feature_id=feature.id,
            title=f"multi-feature-{index}",
            dimension="functional",
            case_type="api",
            spec={"title": f"multi-feature-{index}", "steps": [], "assertions": []},
            status="enabled",
        )
        for index, feature in enumerate(features)
    ]
    session.add_all(cases)
    session.commit()

    payload = client.get(
        f"/api/projects/{sample_project.id}/cases/page?feature_ids={features[0].id},{features[1].id}"
    ).json()
    assert {item["id"] for item in payload["items"]} == {case.id for case in cases}
