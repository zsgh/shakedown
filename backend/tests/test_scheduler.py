"""调度器、环境就绪与运行策略测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.environment_check import check_target
from app.models import CaseResult, ProjectSchedule, Run, Task
from app.models import TestCase as CaseModel
from app.run_service import select_cases_for_policy
from app.worker.scheduler import Scheduler


def _case(project_id: int, title: str, *, tags=None, priority: int = 100, dimension: str = "functional"):
    case = CaseModel(
        project_id=project_id,
        title=title,
        dimension=dimension,
        case_type="api",
        steps=[{"action": "request", "method": "GET", "path": "/api/health"}],
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/health"}], "assertions": []},
        status="enabled",
        tags=tags or [],
        priority=priority,
    )
    return case


def test_run_policy_selects_smoke_and_caps(session, sample_project):
    session.add_all(
        [
            _case(sample_project.id, "冒烟", tags=["smoke"], priority=100),
            _case(sample_project.id, "高优", priority=10),
            _case(sample_project.id, "低优", priority=200),
        ]
    )
    session.commit()
    smoke = select_cases_for_policy(session, sample_project, {"mode": "smoke", "max_cases": 10})
    assert {case.title for case in smoke} == {"冒烟", "高优"}

    capped = select_cases_for_policy(session, sample_project, {"mode": "full", "max_cases": 1})
    assert len(capped) == 1


def test_environment_check_reports_ok_and_failure(monkeypatch, sample_project):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: Response())
    assert check_target(sample_project)["ok"] is True

    def boom(*args, **kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr("httpx.get", boom)
    result = check_target(sample_project)
    assert result["ok"] is False
    assert "refused" in result["reason"]


def test_schedule_crud_and_run_now_environment_failure(client, session, sample_project):
    response = client.post(
        f"/api/projects/{sample_project.id}/schedules",
        json={"name": "夜间回归", "interval_seconds": 3600, "policy": {"mode": "full"}},
    )
    assert response.status_code == 201, response.text
    schedule = response.json()
    assert schedule["enabled"] is True
    assert schedule["next_run_at"]

    listed = client.get(f"/api/projects/{sample_project.id}/schedules").json()
    assert [item["id"] for item in listed] == [schedule["id"]]

    # sample_project 的 base_url 指向不可达端口，run-now 应记录环境失败，而不是创建执行任务。
    result = client.post(f"/api/schedules/{schedule['id']}/run-now").json()
    assert result["created"] is False
    assert result["status"] == "failed"
    session.refresh(session.get(ProjectSchedule, schedule["id"]))
    row = session.get(ProjectSchedule, schedule["id"])
    assert row.last_status == "environment_unavailable"
    assert row.last_run_id == result["run_id"]
    assert session.get(Run, result["run_id"]).error.startswith("环境未就绪")


def test_scheduler_tick_creates_due_run(session, sample_project, monkeypatch):
    case = _case(sample_project.id, "定时回归")
    schedule = ProjectSchedule(
        project_id=sample_project.id,
        name="每分钟",
        enabled=True,
        interval_seconds=60,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        policy={"mode": "full"},
    )
    session.add_all([case, schedule])
    session.commit()
    monkeypatch.setattr("app.worker.scheduler.check_target", lambda project: {"ok": True})

    processed = Scheduler().run_once()
    assert processed == 1
    session.refresh(schedule)
    assert schedule.last_run_id
    run = session.get(Run, schedule.last_run_id)
    assert run is not None
    assert run.case_ids == [case.id]
    assert session.query(Task).filter(Task.kind == "execute_run", Task.run_id == run.id).count() == 1
    assert schedule.next_run_at > datetime.now(timezone.utc)


def test_scheduler_enqueues_performance_run(session, sample_project, monkeypatch):
    schedule = ProjectSchedule(
        project_id=sample_project.id,
        name="性能调度",
        enabled=True,
        interval_seconds=60,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        policy={"mode": "performance", "targets": [{"path": "/api/health", "iterations": 2}]},
    )
    session.add(schedule)
    session.commit()
    monkeypatch.setattr("app.worker.scheduler.check_target", lambda project: {"ok": True})

    assert Scheduler().run_once() == 1
    task = session.query(Task).filter(Task.kind == "performance_run").one()
    assert task.project_id == sample_project.id
    assert task.payload["targets"][0]["path"] == "/api/health"
