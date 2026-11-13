"""执行失败 / 取消的闭环回归。"""
from __future__ import annotations

from app.models import Project, Run, Task
from app.worker.queue import fail


def test_final_task_failure_marks_run_failed(session):
    project = Project(name="worker-fail", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    run = Run(project_id=project.id, trigger="manual", status="running", case_ids=[])
    session.add(run)
    session.flush()
    task = Task(
        project_id=project.id,
        run_id=run.id,
        kind="execute_run",
        status="running",
        attempts=1,
        max_attempts=1,
        payload={"run_id": run.id},
    )
    session.add(task)
    session.commit()

    fail(session, task, "sandbox exploded")
    session.refresh(run)
    assert run.status == "failed"
    assert "sandbox exploded" in (run.error or "")
    assert run.finished_at is not None


def test_retry_failed_execute_task_creates_new_run(client, session):
    project = Project(name="retry-run", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    from app.models import TestCase as CaseModel

    case = CaseModel(
        project_id=project.id,
        title="重试用例",
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/health"}], "assertions": []},
        status="enabled",
    )
    session.add(case)
    session.flush()
    run = Run(project_id=project.id, trigger="manual", status="failed", case_ids=[case.id], error="boom")
    session.add(run)
    session.flush()
    task = Task(
        project_id=project.id,
        run_id=run.id,
        kind="execute_run",
        status="failed",
        attempts=1,
        max_attempts=1,
        payload={"run_id": run.id},
    )
    session.add(task)
    session.commit()

    response = client.post(f"/api/tasks/{task.id}/retry")
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["status"] == "queued"
    new_run = session.get(Run, created["run_id"])
    assert new_run is not None
    assert new_run.trigger == "retry"
    assert new_run.case_ids == [case.id]


def test_cancel_task_cascades_to_run(client, session):
    project = Project(name="cancel-run", base_url="http://127.0.0.1:1")
    session.add(project)
    session.flush()
    run = Run(project_id=project.id, trigger="manual", status="running", case_ids=[])
    session.add(run)
    session.flush()
    task = Task(
        project_id=project.id,
        run_id=run.id,
        kind="execute_run",
        status="running",
        attempts=1,
        max_attempts=3,
        payload={"run_id": run.id},
    )
    session.add(task)
    session.commit()

    response = client.post(f"/api/tasks/{task.id}/cancel")
    assert response.status_code == 200, response.text
    session.refresh(task)
    session.refresh(run)
    assert task.status == "cancelled"
    assert run.status == "cancelled"
    assert run.finished_at is not None


def test_false_positive_status_overrides_ai_verdict():
    from types import SimpleNamespace
    from app.api.findings import _matches_verdict

    finding = SimpleNamespace(
        source="whitebox",
        status="false_positive",
        fix_status="open",
        triage_status="ai_triaged",
        triage={"verdict": "real_bug"},
    )
    assert _matches_verdict(finding, "actionable") is False
    assert _matches_verdict(finding, "false_positive") is True

    confirmed = SimpleNamespace(
        source="whitebox",
        status="confirmed",
        fix_status="open",
        triage_status="ai_triaged",
        triage={"verdict": "false_positive"},
    )
    assert _matches_verdict(confirmed, "actionable") is True
