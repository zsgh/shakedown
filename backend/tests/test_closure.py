"""关闭闭环：处置状态、外部验证失败入缺陷、人工归因联动 finding。"""
from __future__ import annotations

from sqlalchemy import select

from app.models import CaseResult, Finding, Run, TestCase as CaseModel


def _case(project_id: int, title: str = "示例用例") -> CaseModel:
    return CaseModel(
        project_id=project_id,
        title=title,
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/health"}], "assertions": []},
        status="enabled",
    )


def test_finding_disposition_accept_and_wontfix(client, session, sample_project):
    finding = Finding(
        project_id=sample_project.id,
        source="execution",
        title="待处置缺陷",
        description="",
        severity="medium",
        status="confirmed",
        evidence=[],
        code_location={},
        module="",
        risk_type="",
        repro="",
    )
    session.add(finding)
    session.commit()

    response = client.patch(
        f"/api/findings/{finding.id}",
        json={"status": "accepted", "assignee": "team-a", "note": "已知风险，下季度处理"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["assignee"] == "team-a"
    assert body["resolution"]["note"] == "已知风险，下季度处理"

    session.refresh(finding)
    assert finding.status == "accepted"
    assert finding.assignee == "team-a"

    # 已接受风险的缺陷不能再出现在 actionable 修复清单里
    export = client.get(f"/api/projects/{sample_project.id}/findings/export.md")
    assert "待处置缺陷" not in export.text

    response = client.patch(f"/api/findings/{finding.id}", json={"status": "wont_fix", "note": "不修"})
    assert response.status_code == 200
    assert response.json()["status"] == "wont_fix"

    response = client.patch(f"/api/findings/{finding.id}", json={"status": "pending"})
    assert response.status_code == 200
    assert response.json()["resolution"] == {}


def test_external_verification_failure_creates_finding(client, session, sample_project):
    case = _case(sample_project.id, "外部进程验证失败")
    case.verification_mode = "process"
    session.add(case)
    session.commit()

    response = client.post(
        f"/api/cases/{case.id}/verification",
        json={
            "passed": False,
            "mode": "process",
            "command": "go test ./...",
            "result": "FAIL",
            "note": "进程启动失败",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verification"]["status"] == "reported_failed"
    assert body["verification"]["finding_id"]

    finding = session.scalar(
        select(Finding).where(Finding.case_id == case.id, Finding.source == "external")
    )
    assert finding is not None
    assert finding.status == "pending"
    assert "外部验证失败" in finding.title


def test_claim_finding_work(client, session, sample_project):
    finding = Finding(
        project_id=sample_project.id,
        source="execution",
        title="待认领缺陷",
        description="",
        severity="medium",
        status="confirmed",
        evidence=[],
        code_location={},
        module="",
        risk_type="",
        repro="",
    )
    session.add(finding)
    session.commit()

    first = client.post(
        f"/api/findings/{finding.id}/claim",
        json={"assignee": "agent-a", "note": "开始修"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["finding"]["fix_status"] == "in_progress"
    assert first.json()["finding"]["assignee"] == "agent-a"

    second = client.post(
        f"/api/findings/{finding.id}/claim",
        json={"assignee": "agent-b", "note": "抢单"},
    )
    assert second.status_code == 409

    again = client.post(
        f"/api/findings/{finding.id}/claim",
        json={"assignee": "agent-a"},
    )
    assert again.status_code == 200


def test_sync_external_issue_route(client, sample_project):
    response = client.post(f"/api/projects/{sample_project.id}/findings/integrations/sync")
    assert response.status_code == 200, response.text
    assert response.json() == {"synced": 0, "failed": 0, "items": []}


def test_manual_attribution_reconciles_finding(client, session, sample_project):
    case = _case(sample_project.id, "归因联动用例")
    session.add(case)
    session.flush()
    run = Run(project_id=sample_project.id, trigger="manual", status="completed", stats={})
    session.add(run)
    session.flush()
    result = CaseResult(
        run_id=run.id,
        case_id=case.id,
        project_id=sample_project.id,
        status="fail",
        seq=0,
        attribution={"category": "defect", "reason": "原归因"},
    )
    session.add(result)
    session.flush()
    finding = Finding(
        project_id=sample_project.id,
        source="execution",
        title="原缺陷",
        description="",
        severity="high",
        status="pending",
        evidence=[],
        code_location={},
        module="",
        risk_type="",
        repro="",
        result_id=result.id,
        case_id=case.id,
    )
    session.add(finding)
    session.commit()

    response = client.post(
        f"/api/results/{result.id}/attribution",
        json={"category": "case", "reason": "断言写错了", "note": "人工复核"},
    )
    assert response.status_code == 200, response.text
    session.refresh(finding)
    assert finding.status == "false_positive"
    assert finding.resolution["actor"] == "manual:attribution"

    # 反向：人工改判为缺陷且没有 finding 时，应补一条可跟踪缺陷
    result2 = CaseResult(
        run_id=run.id,
        case_id=case.id,
        project_id=sample_project.id,
        status="fail",
        seq=1,
        attribution={"category": "unknown"},
    )
    session.add(result2)
    session.commit()
    response = client.post(
        f"/api/results/{result2.id}/attribution",
        json={"category": "defect", "reason": "确认是产品缺陷"},
    )
    assert response.status_code == 200, response.text
    created = session.scalar(
        select(Finding).where(Finding.result_id == result2.id, Finding.source == "execution")
    )
    assert created is not None
    assert created.status == "confirmed"
