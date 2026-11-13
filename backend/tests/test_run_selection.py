"""执行选择回归：显式选择 0 条用例时，绝不能回退成全量执行。"""
from __future__ import annotations

from app.executor.runner import select_cases
from app.models import Run, TestCase as CaseModel

STEP = {"action": "request", "method": "GET", "path": "/health"}


def _case(
    project_id: int,
    title: str,
    *,
    missing_input: str | None = None,
    steps: list[dict] | None = None,
) -> CaseModel:
    return CaseModel(
        project_id=project_id,
        title=title,
        dimension="functional",
        case_type="api",
        steps=steps if steps is not None else [STEP],
        spec={"title": title, "steps": steps if steps is not None else [STEP], "assertions": []},
        status="enabled",
        missing_input=missing_input,
    )


def test_explicit_empty_case_selection_never_runs_all(client, session, sample_project):
    selected = _case(sample_project.id, "选中的用例")
    other = _case(sample_project.id, "不应被执行的用例")
    blocked = _case(sample_project.id, "没有步骤的用例", missing_input="缺少接口定义", steps=[])
    noted = _case(
        sample_project.id,
        "有步骤但说明待补全的用例",
        missing_input="OpenAPI 未定义响应体结构",
    )
    session.add_all([selected, other, blocked, noted])
    session.commit()

    # 只选一条 → 只跑这一条
    response = client.post(
        f"/api/projects/{sample_project.id}/runs",
        json={"case_ids": [selected.id]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["case_ids"] == [selected.id]
    assert body["stats"]["total"] == 1

    # 有步骤 + missing_input 只是说明，不应阻止执行
    response = client.post(
        f"/api/projects/{sample_project.id}/runs",
        json={"case_ids": [noted.id]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["case_ids"] == [noted.id]
    assert body["stats"]["total"] == 1

    # 没有步骤 → 确实无法执行，只能跑 0 条，不能把 other 也带上
    response = client.post(
        f"/api/projects/{sample_project.id}/runs",
        json={"case_ids": [blocked.id]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["case_ids"] == []
    assert body["stats"]["total"] == 0
    assert body["status"] == "completed"
    assert body["task_id"] is None

    # 显式空列表同理
    response = client.post(
        f"/api/projects/{sample_project.id}/runs",
        json={"case_ids": []},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["case_ids"] == []
    assert body["stats"]["total"] == 0
    assert body["task_id"] is None


def test_select_cases_treats_empty_list_as_no_cases(session, sample_project):
    runnable = _case(sample_project.id, "有步骤用例", missing_input="说明而已")
    session.add(runnable)
    session.commit()
    run = Run(project_id=sample_project.id, trigger="manual", status="queued", case_ids=[])
    session.add(run)
    session.commit()

    assert select_cases(session, sample_project, run) == []

    run.case_ids = [runnable.id]
    session.commit()
    assert [case.id for case in select_cases(session, sample_project, run)] == [runnable.id]
