"""白盒走查：建议用例关联、重复走查去重、最近执行回链。"""
from __future__ import annotations

from app.agent.agents import whitebox_review
from app.agent.providers import get_provider, reset_provider_cache
from app.db import session_scope
from app.models import CaseResult, Chunk, Finding, KnowledgeSource, Project, Run, TestCase as CaseModel


def test_whitebox_links_case_and_dedupes(tmp_path):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "app.py").write_text("def run(value):\n    return eval(value)\n", encoding="utf-8")

    reset_provider_cache()
    with session_scope() as session:
        project = Project(name="whitebox", base_url="http://127.0.0.1:1")
        session.add(project)
        session.flush()
        source = KnowledgeSource(
            project_id=project.id,
            kind="code",
            name="code",
            location=str(repo),
            status="ready",
        )
        session.add(source)
        session.flush()
        session.add(
            Chunk(
                project_id=project.id,
                source_id=source.id,
                content="def run(value):\n    return eval(value)\n",
                locator={"file_path": "web/app.py", "start_line": 1, "end_line": 2},
            )
        )
        session.flush()

        findings = whitebox_review(session, project, get_provider())
        assert findings
        finding = findings[0]
        assert finding.case_id is not None
        case = session.get(CaseModel, finding.case_id)
        assert case is not None
        assert case.title.startswith("[白盒建议]")
        assert case.missing_input

        first_finding_ids = {item.id for item in findings}
        first_case_count = session.query(CaseModel).filter_by(project_id=project.id).count()

        # 已完成模块应被跳过；取消时不应继续跑后续模块
        skipped = whitebox_review(
            session, project, get_provider(), completed_modules={"web"}
        )
        assert skipped == []
        cancelled = whitebox_review(
            session, project, get_provider(), should_cancel=lambda: True
        )
        assert cancelled == []

        # 第二次走查：同一位置复用 finding / 建议用例，不重复刷屏
        again = whitebox_review(session, project, get_provider())
        assert {item.id for item in again} == first_finding_ids
        assert session.query(CaseModel).filter_by(project_id=project.id).count() == first_case_count
        assert session.query(Finding).filter_by(project_id=project.id, source="whitebox").count() == len(findings)

        run = Run(project_id=project.id, trigger="manual", status="completed", stats={})
        session.add(run)
        session.flush()
        result = CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=project.id,
            status="fail",
            output={"errors": ["验证未通过"]},
        )
        session.add(result)
        session.flush()
        case_id, run_id = case.id, run.id

    # API：白盒建议用例的最近执行回链
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as client:
        payload = client.get(f"/api/cases/{case_id}/latest-result").json()
    assert payload["run"]["id"] == run_id
    assert payload["result"]["status"] == "fail"
