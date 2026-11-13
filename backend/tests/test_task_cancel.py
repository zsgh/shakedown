"""任务取消与白盒断点续跑。"""
from __future__ import annotations

from app.models import Task


def test_cancel_task_and_resume_whitebox(client, session, sample_project):
    task = Task(
        project_id=sample_project.id,
        kind="whitebox_review",
        status="running",
        checkpoint={"whitebox": {"done": 1, "total": 2, "done_modules": ["web"]}},
    )
    session.add(task)
    session.commit()

    response = client.post(f"/api/tasks/{task.id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    response = client.post(f"/api/projects/{sample_project.id}/agent/whitebox?resume=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload["resumed"] is True
    resumed = session.get(Task, payload["task_id"])
    assert resumed is not None
    assert resumed.payload["resume_checkpoint"]["done_modules"] == ["web"]
