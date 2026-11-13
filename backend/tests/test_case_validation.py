"""生成后试运行：可判定用例筛选、建 run、结果落状态。"""
from __future__ import annotations

from app.case_validation import (
    apply_validation_outcome,
    create_validation_run,
    validation_eligible,
)
from app.executor.runner import select_cases
from app.models import AutomationException, CaseResult, Run, Task
from app.models import TestCase as CaseModel

STEP = {"action": "request", "method": "GET", "path": "/health"}
ASSERTION = {
    "kind": "status_code",
    "target": "status",
    "operator": "eq",
    "expected": 200,
    "status": "confirmed",
    "evidence": {"source_type": "openapi", "ref": "GET /health"},
}


def _case(
    project_id: int,
    title: str,
    *,
    status: str = "pending",
    steps: list[dict] | None = None,
    assertions: list[dict] | None = None,
    missing_input: str | None = None,
    dimension: str = "functional",
) -> CaseModel:
    steps = steps if steps is not None else [STEP]
    assertions = assertions if assertions is not None else [ASSERTION]
    return CaseModel(
        project_id=project_id,
        title=title,
        dimension=dimension,
        case_type="api",
        steps=steps,
        expected={"assertions": assertions},
        spec={"title": title, "steps": steps, "assertions": assertions},
        status=status,
        missing_input=missing_input,
        verification_mode="api",
    )


def test_eligible_requires_steps_assertions_and_low_risk(session, sample_project):
    good = _case(sample_project.id, "可判定")
    no_steps = _case(sample_project.id, "没有步骤", steps=[])
    pending_only = _case(
        sample_project.id,
        "只有待确认断言",
        assertions=[{**ASSERTION, "status": "pending"}],
    )
    missing = _case(sample_project.id, "缺输入", missing_input="缺少接口定义")
    risky = _case(
        sample_project.id,
        "高风险",
        steps=[{"action": "request", "method": "DELETE", "path": "/api/items/1"}],
    )
    session.add_all([good, no_steps, pending_only, missing, risky])
    session.commit()

    assert validation_eligible(good, sample_project) == (True, "")
    assert validation_eligible(no_steps, sample_project) == (False, "no_steps")
    assert validation_eligible(pending_only, sample_project) == (False, "no_confirmed_assertion")
    assert validation_eligible(missing, sample_project) == (True, "")
    assert validation_eligible(risky, sample_project) == (False, "high_risk")


def test_create_validation_run_picks_pending_and_is_idempotent(session, sample_project):
    pending = _case(sample_project.id, "待确认")
    enabled = _case(sample_project.id, "已启用", status="enabled")
    session.add_all([pending, enabled])
    session.commit()

    run, created = create_validation_run(session, sample_project, limit=10)
    session.commit()

    assert created is True
    assert run is not None
    assert run.trigger == "validation"
    assert run.case_ids == [pending.id]
    assert run.stats["policy"]["validation"] is True
    assert session.query(Task).filter(Task.kind == "execute_run", Task.run_id == run.id).count() == 1

    again, created_again = create_validation_run(session, sample_project, limit=10)
    assert created_again is False
    assert again.id == run.id


def test_validation_run_selects_pending_cases(session, sample_project):
    pending = _case(sample_project.id, "待确认")
    session.add(pending)
    session.commit()

    validation_run = Run(
        project_id=sample_project.id,
        trigger="validation",
        status="queued",
        case_ids=[pending.id],
        stats={"policy": {"validation": True, "allow_pending": True}},
    )
    normal_run = Run(
        project_id=sample_project.id,
        trigger="manual",
        status="queued",
        case_ids=[pending.id],
        stats={"policy": {}},
    )
    session.add_all([validation_run, normal_run])
    session.commit()

    assert [case.id for case in select_cases(session, sample_project, validation_run)] == [pending.id]
    assert select_cases(session, sample_project, normal_run) == []


def test_apply_outcome_promotes_pass_and_demotes_failure(session, sample_project):
    passing = _case(sample_project.id, "能跑通")
    failing = _case(sample_project.id, "跑不通")
    session.add_all([passing, failing])
    session.commit()

    run = Run(
        project_id=sample_project.id,
        trigger="validation",
        status="analyzing",
        case_ids=[passing.id, failing.id],
        stats={"policy": {"validation": True, "allow_pending": True}},
    )
    session.add(run)
    session.commit()
    session.add_all(
        [
            CaseResult(
                project_id=sample_project.id,
                run_id=run.id,
                case_id=passing.id,
                status="pass",
                logs="ok",
            ),
            CaseResult(
                project_id=sample_project.id,
                run_id=run.id,
                case_id=failing.id,
                status="fail",
                logs="expected 200, got 500",
            ),
        ]
    )
    session.commit()

    result = apply_validation_outcome(session, sample_project, run)
    session.commit()

    assert result["promoted"] == 1
    assert result["demoted"] == 1
    assert session.get(CaseModel, passing.id).status == "enabled"
    assert session.get(CaseModel, failing.id).status == "needs_review"
    assert session.get(CaseModel, passing.id).verification["auto_validation"]["status"] == "pass"
    exception = session.query(AutomationException).filter(
        AutomationException.kind == "case_validation_failed",
        AutomationException.target_id == failing.id,
    ).one()
    assert exception.status == "open"
    assert "500" in exception.note
    assert result["next_run_id"] is None


def test_auto_enable_off_keeps_case_pending_but_marks_validated(session, sample_project):
    sample_project.settings_json = {"automation": {"auto_enable_after_validation": False}}
    case = _case(sample_project.id, "通过但不启用")
    session.add(case)
    session.commit()

    run = Run(
        project_id=sample_project.id,
        trigger="validation",
        status="analyzing",
        case_ids=[case.id],
        stats={"policy": {"validation": True, "allow_pending": True}},
    )
    session.add(run)
    session.commit()
    session.add(
        CaseResult(
            project_id=sample_project.id,
            run_id=run.id,
            case_id=case.id,
            status="pass",
        )
    )
    session.commit()

    result = apply_validation_outcome(session, sample_project, run)
    session.commit()

    refreshed = session.get(CaseModel, case.id)
    assert result["promoted"] == 0
    assert refreshed.status == "pending"
    assert refreshed.verification["auto_validation"]["status"] == "pass"
    # 已通过试运行的用例不应该被反复跑
    assert validation_eligible(refreshed, sample_project) == (False, "already_validated")


def test_validation_api_preview_and_run(client, session, sample_project):
    case = _case(sample_project.id, "API 用例")
    session.add(case)
    session.commit()

    preview = client.get(f"/api/projects/{sample_project.id}/cases/validation/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] == 1

    first = client.post(f"/api/projects/{sample_project.id}/cases/validation/run", json={"limit": 10})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True
    assert body["cases"] == 1

    second = client.post(f"/api/projects/{sample_project.id}/cases/validation/run", json={"limit": 10})
    assert second.json()["created"] is False
    assert second.json()["run_id"] == body["run_id"]


def test_generate_handler_enqueues_validation_for_new_cases(monkeypatch, session, sample_project):
    from app.worker import handlers

    case = _case(sample_project.id, "刚生成的用例")
    session.add(case)
    session.commit()
    monkeypatch.setattr(handlers, "generate_cases", lambda *args, **kwargs: [case])

    result = handlers.handle_generate_cases(session, {"project_id": sample_project.id})

    validation = result["validation"]
    assert validation["created"] is True
    run = session.get(Run, validation["run_id"])
    assert run is not None
    assert run.trigger == "validation"
    assert run.case_ids == [case.id]


def test_generate_handler_skips_validation_when_disabled(monkeypatch, session, sample_project):
    from app.worker import handlers

    sample_project.settings_json = {"automation": {"auto_validate_generated": False}}
    case = _case(sample_project.id, "关闭自动试运行")
    session.add(case)
    session.commit()
    monkeypatch.setattr(handlers, "generate_cases", lambda *args, **kwargs: [case])

    result = handlers.handle_generate_cases(session, {"project_id": sample_project.id})

    assert result["validation"]["run_id"] is None
    assert session.query(Run).filter(Run.project_id == sample_project.id, Run.trigger == "validation").count() == 0


def test_plan_features_enqueues_generation_for_empty_project(monkeypatch, session, sample_project):
    from app.models import Feature, Task
    from app.worker import handlers

    feature = Feature(project_id=sample_project.id, name="登录", module="auth")
    session.add(feature)
    session.commit()
    monkeypatch.setattr(handlers, "extract_features", lambda *args, **kwargs: [feature])

    result = handlers.handle_plan_features(session, {"project_id": sample_project.id})

    assert result["generate"]["task_id"]
    task = session.get(Task, result["generate"]["task_id"])
    assert task.kind == "generate_cases"
    assert task.status == "queued"
    assert task.payload["only_missing"] is True


def test_plan_features_does_not_generate_when_cases_exist(monkeypatch, session, sample_project):
    from app.models import Feature
    from app.worker import handlers

    feature = Feature(project_id=sample_project.id, name="登录", module="auth")
    session.add_all([feature, _case(sample_project.id, "已有用例")])
    session.commit()
    monkeypatch.setattr(handlers, "extract_features", lambda *args, **kwargs: [feature])

    result = handlers.handle_plan_features(session, {"project_id": sample_project.id})

    assert "generate" not in result


def test_validation_auto_repairs_wrong_output_stream(session, sample_project):
    assertion = {
        "kind": "stdout_contains",
        "operator": "contains",
        "expected": "flag needs an argument",
        "status": "confirmed",
        "evidence": {"source_type": "code", "ref": "cmd/main.go"},
    }
    step = {"action": "command", "command": ["dm-git", "-restore"], "assert": [assertion]}
    case = _case(sample_project.id, "缺参数报错", steps=[step], assertions=[])
    session.add(case)
    session.commit()
    run = Run(
        project_id=sample_project.id,
        trigger="validation",
        status="analyzing",
        case_ids=[case.id],
        stats={"policy": {"validation": True, "allow_pending": True}},
    )
    session.add(run)
    session.commit()
    session.add(
        CaseResult(
            project_id=sample_project.id,
            run_id=run.id,
            case_id=case.id,
            status="fail",
            output={
                "assertions": [
                    {
                        **assertion,
                        "passed": False,
                        "pending": False,
                        "step_index": 0,
                        "actual": "RC=2\n",
                    }
                ],
                "commands": [
                    {
                        "step_index": 0,
                        "stdout": "RC=2\n",
                        "stderr": "flag needs an argument: -restore\n",
                        "exit_code": 2,
                    }
                ],
            },
        )
    )
    session.commit()

    result = apply_validation_outcome(session, sample_project, run)
    session.commit()

    session.refresh(case)
    assert case.status == "pending"
    assert case.spec["steps"][0]["assert"][0]["kind"] == "stderr_contains"
    assert case.verification["auto_repair_attempts"] == 1
    assert result["auto_repaired"] == 1
    assert result["next_run_id"] is not None


def test_validation_policy_defect_promotes_and_records_finding(session, sample_project):
    assertion = {
        "kind": "status_code",
        "operator": "eq",
        "expected": 401,
        "status": "confirmed",
        "evidence": {"source_type": "policy", "ref": "SEC-SESSION-REVOKE"},
    }
    step = {"action": "request", "method": "GET", "path": "/api/v1/me", "assert": [assertion]}
    case = _case(sample_project.id, "旧会话必须失效", steps=[step], assertions=[])
    session.add(case)
    session.commit()
    run = Run(
        project_id=sample_project.id,
        trigger="validation",
        status="analyzing",
        case_ids=[case.id],
        stats={"policy": {"validation": True, "allow_pending": True}},
    )
    session.add(run)
    session.commit()
    session.add(
        CaseResult(
            project_id=sample_project.id,
            run_id=run.id,
            case_id=case.id,
            status="fail",
            output={
                "assertions": [
                    {
                        **assertion,
                        "passed": False,
                        "pending": False,
                        "step_index": 0,
                        "actual": 200,
                        "detail": "实际状态码 200，期望 401",
                    }
                ]
            },
        )
    )
    session.commit()

    result = apply_validation_outcome(session, sample_project, run)
    session.commit()

    session.refresh(case)
    assert case.status == "enabled"
    assert result["defects"] == 1
    from app.models import Finding

    finding = session.query(Finding).filter(Finding.case_id == case.id).one()
    assert finding.status == "confirmed"
    assert finding.confirmed_by == "auto:attack-pack"
