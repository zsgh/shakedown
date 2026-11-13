"""运行对比 + Spec 快照对比。"""
from __future__ import annotations

from app.db import session_scope
from app.models import CaseResult, Finding, KnowledgeSource, Project, Run, SpecSnapshot, TestCase as CaseModel


def test_run_compare_classifies_changes(client):
    response = client.post("/api/projects", json={"name": "compare", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]

    with session_scope() as session:
        cases = [
            CaseModel(project_id=project_id, title="一直通过", dimension="functional", case_type="api", status="enabled", spec={}),
            CaseModel(project_id=project_id, title="这次坏了", dimension="functional", case_type="api", status="enabled", spec={}),
            CaseModel(project_id=project_id, title="这次修好", dimension="boundary", case_type="api", status="enabled", spec={}),
            CaseModel(project_id=project_id, title="一直失败", dimension="security", case_type="api", status="enabled", spec={}),
        ]
        session.add_all(cases)
        session.flush()
        base = Run(project_id=project_id, trigger="manual", status="completed", stats={"total": 4})
        session.add(base)
        session.flush()
        target = Run(project_id=project_id, trigger="manual", status="completed", stats={"total": 4})
        session.add(target)
        session.flush()

        base_status = ["pass", "pass", "fail", "fail"]
        target_status = ["pass", "fail", "pass", "fail"]
        for case, status in zip(cases, base_status):
            session.add(CaseResult(run_id=base.id, case_id=case.id, project_id=project_id, status=status, seq=0))
        for index, (case, status) in enumerate(zip(cases, target_status)):
            session.add(CaseResult(run_id=target.id, case_id=case.id, project_id=project_id, status=status, seq=index))
        base_id, target_id = base.id, target.id

    response = client.get(f"/api/projects/{project_id}/runs/compare?base={base_id}&target={target_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["broken"] == 1
    assert payload["summary"]["fixed"] == 1
    assert payload["summary"]["still_failing"] == 1
    assert payload["summary"]["still_passing"] == 1
    changes = {item["title"]: item["change"] for item in payload["cases"]}
    assert changes["这次坏了"] == "broken"
    assert changes["这次修好"] == "fixed"


def test_spec_snapshot_compare(client):
    response = client.post("/api/projects", json={"name": "spec-diff", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]

    with session_scope() as session:
        source = KnowledgeSource(
            project_id=project_id,
            kind="openapi",
            name="test spec",
            location="inline",
            status="ready",
        )
        session.add(source)
        session.flush()
        old = SpecSnapshot(
            project_id=project_id,
            source_id=source.id,
            version="old",
            spec={
                "endpoints": [
                    {"method": "GET", "path": "/api/old", "params": {}, "request_body": {}, "responses": {}},
                    {"method": "GET", "path": "/api/keep", "params": {}, "request_body": {}, "responses": {}},
                ]
            },
            diff={},
        )
        new = SpecSnapshot(
            project_id=project_id,
            source_id=source.id,
            version="new",
            spec={
                "endpoints": [
                    {"method": "GET", "path": "/api/keep", "params": {}, "request_body": {}, "responses": {}},
                    {"method": "POST", "path": "/api/new", "params": {}, "request_body": {}, "responses": {}},
                ]
            },
            diff={},
        )
        session.add_all([old, new])
        session.flush()
        old_id, new_id = old.id, new.id

    response = client.get(
        f"/api/projects/{project_id}/spec-snapshots/compare?base_id={old_id}&target_id={new_id}"
    )
    assert response.status_code == 200
    diff = response.json()["diff"]
    assert diff["added"][0]["path"] == "/api/new"
    assert diff["removed"][0]["path"] == "/api/old"

    snapshots = client.get(f"/api/projects/{project_id}/spec-snapshots").json()
    assert len(snapshots) == 2


def test_create_finding_auto_confirms_high_confidence_defect():
    from app.worker.handlers import _create_finding

    with session_scope() as session:
        project = Project(name="auto-confirm", base_url="http://127.0.0.1:1")
        session.add(project)
        session.flush()
        case = CaseModel(
            project_id=project.id,
            title="空名称",
            dimension="boundary",
            case_type="api",
            status="enabled",
            spec={},
        )
        session.add(case)
        session.flush()
        run = Run(project_id=project.id, trigger="manual", status="completed", stats={})
        session.add(run)
        session.flush()
        row = CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=project.id,
            status="fail",
            request={"method": "POST", "url": "http://example.test/api/projects/1/features"},
        )
        session.add(row)
        session.flush()
        finding, created = _create_finding(
            session,
            project,
            run,
            case,
            row,
            {"reason": "接受了空名称", "evidence": [], "auto_confirm": True},
        )
        session.flush()
        assert created is True
        assert finding.status == "confirmed"
        assert finding.confirmed_by == "auto:attribution"
        assert finding.confirmed_at is not None


def test_deterministic_attribution_overrides_conservative_model():
    from app.agent.agents import attribute_failure

    class ConservativeProvider:
        def structured(self, *args, **kwargs):
            return {
                "category": "case",
                "reason": "OpenAPI 没有写 minLength",
                "confidence": 0.3,
                "evidence": [],
                "is_defect": False,
            }

    with session_scope() as session:
        project = Project(name="attribution", base_url="http://127.0.0.1:1")
        session.add(project)
        session.flush()
        case = CaseModel(
            project_id=project.id,
            title="创建功能点 - 空名称返回 422",
            dimension="boundary",
            case_type="api",
            status="enabled",
            spec={
                "title": "创建功能点 - 空名称返回 422",
                "steps": [
                    {
                        "action": "request",
                        "method": "POST",
                        "path": "/api/projects/1/features",
                        "body": {"name": ""},
                    }
                ],
            },
        )
        session.add(case)
        session.flush()
        run = Run(project_id=project.id, trigger="manual", status="completed", stats={})
        session.add(run)
        session.flush()
        row = CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=project.id,
            status="fail",
            request={"method": "POST", "url": "http://example.test/api/projects/1/features", "body": {"name": ""}},
            response={"status_code": 201, "body": {"id": 1, "name": ""}},
            output={
                "requests": [
                    {
                        "method": "POST",
                        "url": "http://example.test/api/projects/1/features",
                        "body": {"name": ""},
                    }
                ],
                "responses": [{"status_code": 201, "body": {"id": 1, "name": ""}}],
                "assertions": [
                    {
                        "kind": "status_code",
                        "actual": 201,
                        "expected": 422,
                        "passed": False,
                        "pending": False,
                        "step_index": 0,
                    }
                ],
            },
        )
        session.add(row)
        session.flush()
        data = attribute_failure(session, project, ConservativeProvider(), case, row)
        assert data["category"] == "defect"
        assert data["is_defect"] is True
        assert data["auto_confirm"] is True
        assert data["deterministic_signals"][0]["type"] == "common_sense_empty_identifier"


def test_actionable_includes_confirmed_execution_finding_without_triage():
    from types import SimpleNamespace
    from app.api.findings import _matches_verdict

    finding = SimpleNamespace(
        source="execution",
        status="confirmed",
        triage_status="untriaged",
        triage={},
    )
    assert _matches_verdict(finding, "actionable") is True
    pending = SimpleNamespace(source="execution", status="pending", triage_status="untriaged", triage={})
    assert _matches_verdict(pending, "actionable") is False


def test_execution_finding_is_deduplicated():
    """同一用例连续失败，保留一条未关闭缺陷并更新证据，而不是刷屏。"""
    from app.worker.handlers import _create_finding

    with session_scope() as session:
        project = Project(name="dedupe", base_url="http://127.0.0.1:1")
        session.add(project)
        session.flush()
        case = CaseModel(
            project_id=project.id,
            title="重复失败",
            dimension="functional",
            case_type="api",
            status="enabled",
            spec={},
        )
        session.add(case)
        session.flush()
        run = Run(project_id=project.id, trigger="manual", status="completed", stats={})
        session.add(run)
        session.flush()
        row = CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=project.id,
            status="fail",
            request={"method": "GET", "url": "http://example.test/x"},
            output={"assertions": [{"kind": "status_code", "passed": False}]},
        )
        session.add(row)
        session.flush()

        finding, created = _create_finding(
            session, project, run, case, row, {"reason": "实际与预期不一致", "evidence": []}
        )
        session.flush()
        assert created is True

        second, created_again = _create_finding(
            session, project, run, case, row, {"reason": "第二次失败", "evidence": []}
        )
        session.flush()
        assert created_again is False
        assert second.id == finding.id
        assert session.query(Finding).filter_by(project_id=project.id).count() == 1
