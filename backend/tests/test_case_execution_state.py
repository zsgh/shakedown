"""用例执行状态筛选与批量重新评估测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models import CaseResult, KnowledgeSource, Run, Task
from app.models import TestCase as CaseModel
from app.worker.handlers import handle_reparse_source


def _case(project_id: int, title: str, *, status: str = "enabled", missing: str | None = None) -> CaseModel:
    case = CaseModel(
        project_id=project_id,
        title=title,
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/api/health"}],
        spec={
            "steps": [{"action": "request", "method": "GET", "path": "/api/health"}],
            "assertions": [],
        },
        status=status,
        missing_input=missing,
    )
    return case


def _result(session, project_id: int, case: CaseModel) -> CaseResult:
    run = Run(project_id=project_id, status="completed")
    session.add(run)
    session.flush()
    row = CaseResult(
        run_id=run.id,
        case_id=case.id,
        project_id=project_id,
        status="pass",
        request={},
        response={},
    )
    session.add(row)
    return row


def test_execution_state_filters(session, sample_project, client):
    executed = _case(sample_project.id, "执行过")
    enabled_no_result = _case(sample_project.id, "启用但未执行", missing="缺少响应 schema")
    pending = _case(sample_project.id, "待审核未启用", status="pending")
    missing = _case(sample_project.id, "缺说明", missing="缺少 response schema")
    session.add_all([executed, enabled_no_result, pending, missing])
    session.commit()
    _result(session, sample_project.id, executed)
    session.commit()

    body = client.get(
        f"/api/projects/{sample_project.id}/cases",
        params={"execution_state": "never_executed", "limit": 100},
    ).json()
    titles = {item["title"] for item in body}
    assert "启用但未执行" in titles
    assert "待审核未启用" in titles
    assert "执行过" not in titles
    assert all("never_executed" in item["execution_states"] for item in body)

    detail = client.get(f"/api/cases/{enabled_no_result.id}").json()
    assert "never_executed" in detail["execution_states"]
    assert "enabled_no_result" in detail["execution_states"]

    detail = client.get(f"/api/cases/{executed.id}").json()
    assert "executed" in detail["execution_states"]
    assert "never_executed" not in detail["execution_states"]

    detail = client.get(f"/api/cases/{pending.id}").json()
    assert "pending_not_enabled" in detail["execution_states"]

    body = client.get(
        f"/api/projects/{sample_project.id}/cases",
        params={"execution_state": "enabled_no_result", "limit": 100},
    ).json()
    assert {item["title"] for item in body} == {"启用但未执行", "缺说明"}

    body = client.get(
        f"/api/projects/{sample_project.id}/cases",
        params={"execution_state": "pending_not_enabled", "limit": 100},
    ).json()
    assert {item["title"] for item in body} == {"待审核未启用"}

    body = client.get(
        f"/api/projects/{sample_project.id}/case-gaps",
        params={"execution_state": "enabled_no_result", "limit": 100},
    ).json()
    assert {item["case_id"] for item in body["items"]} == {enabled_no_result.id, missing.id}
    assert all("enabled_no_result" in item["execution_states"] for item in body["items"])
    assert body["facets"]["by_execution_state"]["enabled_no_result"] >= 2


def test_stale_after_source_filter(session, sample_project, client):
    source = KnowledgeSource(
        project_id=sample_project.id,
        kind="markdown",
        name="文档",
        location="/tmp/doc.md",
        status="ready",
        parsed_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    case = _case(sample_project.id, "旧用例")
    session.add_all([source, case])
    session.commit()
    session.execute(
        update(CaseModel)
        .where(CaseModel.id == case.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(days=2))
    )
    session.commit()

    body = client.get(
        f"/api/projects/{sample_project.id}/cases",
        params={"execution_state": "stale_after_source", "limit": 100},
    ).json()
    assert [item["id"] for item in body] == [case.id]
    assert "stale_after_source" in body[0]["execution_states"]

    detail = client.get(f"/api/cases/{case.id}").json()
    assert "stale_after_source" in detail["execution_states"]


def test_batch_enrich_preview_and_enqueue(session, sample_project, client):
    first = _case(sample_project.id, "缺口一", missing="缺少响应结构")
    second = _case(sample_project.id, "缺口二", status="pending", missing="缺少鉴权规则")
    session.add_all([first, second])
    session.commit()

    preview = client.post(
        f"/api/projects/{sample_project.id}/cases/enrich",
        json={"dry_run": True, "filters": {"kind": "missing_input"}},
    ).json()
    assert preview["dry_run"] is True
    assert preview["count"] == 2

    applied = client.post(
        f"/api/projects/{sample_project.id}/cases/enrich",
        json={"dry_run": False, "filters": {"kind": "missing_input"}},
    ).json()
    assert applied["dry_run"] is False
    assert applied["enqueued"] == 2
    tasks = session.query(Task).filter(Task.kind == "enrich_case").all()
    assert {task.payload["case_id"] for task in tasks} == {first.id, second.id}

    again = client.post(
        f"/api/projects/{sample_project.id}/cases/enrich",
        json={"dry_run": False, "filters": {"kind": "missing_input"}},
    ).json()
    assert again["enqueued"] == 0
    assert again["skipped_active"] == 2

    too_many = client.post(
        f"/api/projects/{sample_project.id}/cases/enrich",
        json={"dry_run": True, "filters": {"kind": "missing_input"}, "limit": 201},
    )
    assert too_many.status_code == 422


def test_reparse_returns_reevaluate_candidates(session, sample_project, client, tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# 订单\n\n## 退款\n\n退款仅支持未支付订单。\n", encoding="utf-8")
    source = KnowledgeSource(
        project_id=sample_project.id,
        kind="markdown",
        name="订单文档",
        location=str(doc),
        status="pending",
    )
    session.add_all([source, _case(sample_project.id, "缺响应结构", missing="缺少响应结构")])
    session.commit()

    result = handle_reparse_source(session, {"source_id": source.id})
    assert result["version"]
    assert result["chunks"] >= 0
    assert result["reevaluate_candidates"] >= 1
    session.refresh(source)
    assert source.meta_json["reevaluate_candidates"] == result["reevaluate_candidates"]
    body = client.get(f"/api/sources/{source.id}").json()
    assert body["reevaluate_candidates"] == result["reevaluate_candidates"]
