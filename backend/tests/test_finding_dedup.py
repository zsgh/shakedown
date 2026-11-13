"""缺陷聚类与人工合并。"""
from __future__ import annotations

from app.models import CaseResult, Finding, Run
from app.models import TestCase as CaseModel


def _finding(project_id: int, title: str, *, result_id: int | None = None, cluster_key: str | None = None) -> Finding:
    return Finding(
        project_id=project_id,
        source="execution",
        title=title,
        description="",
        severity="high",
        status="pending",
        module="api",
        risk_type="logic",
        result_id=result_id,
        cluster_key=cluster_key,
        evidence=[],
        code_location={},
    )


def test_clusters_link_cross_run(session, sample_project, client):
    run1 = Run(project_id=sample_project.id, status="completed", case_ids=[])
    run2 = Run(project_id=sample_project.id, status="completed", case_ids=[])
    session.add_all([run1, run2])
    session.flush()
    case = CaseModel(
        project_id=sample_project.id,
        title="订单接口",
        dimension="functional",
        case_type="api",
        spec={"steps": [], "assertions": []},
        steps=[],
        status="enabled",
    )
    session.add(case)
    session.flush()
    result1 = CaseResult(run_id=run1.id, case_id=case.id, project_id=sample_project.id, status="fail", seq=0)
    result2 = CaseResult(run_id=run2.id, case_id=case.id, project_id=sample_project.id, status="fail", seq=0)
    session.add_all([result1, result2])
    session.flush()
    session.add_all(
        [
            _finding(sample_project.id, "订单金额错误 A", result_id=result1.id, cluster_key="order:amount"),
            _finding(sample_project.id, "订单金额错误 B", result_id=result2.id, cluster_key="order:amount"),
        ]
    )
    session.commit()

    body = client.get(f"/api/projects/{sample_project.id}/findings/clusters").json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["count"] == 2
    assert item["cross_run"] is True
    assert {run1.id, run2.id}.issubset(set(item["run_ids"]))
    assert item["case_ids"] == [case.id]


def test_manual_merge_sets_duplicate_and_audit(session, sample_project, client):
    root = _finding(sample_project.id, "根因")
    duplicate = _finding(sample_project.id, "重复")
    session.add_all([root, duplicate])
    session.commit()

    response = client.post(
        f"/api/findings/{root.id}/merge",
        json={"duplicate_id": duplicate.id, "note": "同一根因"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["duplicate_of"] == root.id
    session.refresh(duplicate)
    assert duplicate.duplicate_of == root.id
    assert duplicate.triage_status == "merged"
    assert duplicate.triage["classification"] == "test_issue"
    assert duplicate.resolution["merged_into"] == root.id
    assert duplicate.cluster_key == f"finding:{root.id}"


def test_cannot_merge_self_or_cross_project(session, sample_project, client):
    finding = _finding(sample_project.id, "self")
    session.add(finding)
    other_project = sample_project
    session.commit()
    assert client.post(
        f"/api/findings/{finding.id}/merge", json={"duplicate_id": finding.id}
    ).status_code == 409
    # 自己的 finding 合并到不存在的 ID
    assert client.post(
        f"/api/findings/{finding.id}/merge", json={"duplicate_id": 999999}
    ).status_code == 404
