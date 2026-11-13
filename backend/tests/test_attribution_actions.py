"""正式执行（非试运行）的归因动作测试。"""
from __future__ import annotations

from sqlalchemy import select

from app.models import AutomationException, CaseResult, Finding, Project, Run
from app.models import TestCase as CaseModel
from app.worker.handlers import analyze_run


def _run_case(session, project: Project, *, spec: dict, result_status: str = "fail") -> tuple[CaseModel, CaseResult]:
    case = CaseModel(
        project_id=project.id,
        title=spec.get("title", "用例"),
        dimension="functional",
        case_type="api",
        status="enabled",
        steps=spec.get("steps") or [],
        spec=spec,
        expected={"assertions": []},
    )
    session.add(case)
    session.flush()
    run = Run(project_id=project.id, trigger="manual", status="analyzing", stats={})
    session.add(run)
    session.flush()
    row = CaseResult(
        project_id=project.id,
        run_id=run.id,
        case_id=case.id,
        status=result_status,
        output={"assertions": [], "errors": []},
    )
    session.add(row)
    session.flush()
    return case, row


def test_5xx_is_attributed_to_defect_and_auto_confirmed(session):
    project = Project(name="defect-project", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    assertion = {
        "kind": "status_code",
        "operator": "eq",
        "expected": 200,
        "actual": 500,
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    case, row = _run_case(
        session,
        project,
        spec={
            "title": "创建项目返回 200",
            "steps": [{"action": "request", "method": "POST", "path": "/api/projects"}],
        },
    )
    row.request = {"method": "POST", "url": "http://example.test/api/projects"}
    row.response = {"status_code": 500, "body": {"error": "boom"}}
    row.output = {"assertions": [assertion], "errors": ["断言失败：实际状态码 500"]}
    session.flush()
    run = session.get(Run, row.run_id)

    result = analyze_run(session, project, run)
    session.flush()

    assert row.attribution["category"] == "defect"
    assert row.attribution["reason_code"] == "server_error"
    finding = session.scalar(select(Finding).where(Finding.case_id == case.id))
    assert finding is not None
    assert finding.status == "confirmed"
    assert result["findings_created"] == 1
    assert session.get(CaseModel, case.id).status == "enabled"


def test_platform_gap_repairs_spec_instead_of_blaming_case(session):
    project = Project(name="platform-project", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    assertion = {
        "kind": "status_code",
        "operator": "contains",
        "expected": "ARCHIVE_OK",
        "actual": 0,
        "passed": False,
        "pending": False,
        "step_index": 0,
    }
    case, row = _run_case(
        session,
        project,
        spec={
            "title": "备份产物落盘",
            "steps": [{"action": "command", "command": ["dm-git", "-backup"], "assert": [assertion]}],
        },
    )
    row.output = {"assertions": [assertion], "errors": ["断言失败：实际状态码 0，期望 ARCHIVE_OK"]}
    session.flush()
    run = session.get(Run, row.run_id)

    result = analyze_run(session, project, run)
    session.flush()

    assert row.attribution["category"] == "platform"
    assert row.attribution["reason_code"] == "command_assertion_kind"
    assert row.attribution.get("auto_repaired") is True
    assert case.spec["steps"][0]["assert"][0]["kind"] == "stdout_contains"
    assert result["auto_repaired"] == 1
    assert session.scalar(select(Finding).where(Finding.case_id == case.id)) is None
    assert case.status == "enabled"
    exception = session.scalar(
        select(AutomationException).where(
            AutomationException.kind == "platform_capability_gap",
            AutomationException.target_id == case.id,
        )
    )
    assert exception is not None
