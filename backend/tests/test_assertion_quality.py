"""断言强度 / oracle mutation / oracle 趋势。"""
from __future__ import annotations

from app.assertion_quality import case_assertion_quality, mutation_audit, oracle_trend, project_assertion_quality
from app.models import CaseResult, Run


def _case(project_id: int, title: str, assertions: list[dict]) -> object:
    from app.models import TestCase as CaseModel

    return CaseModel(
        project_id=project_id,
        title=title,
        dimension="functional",
        case_type="api",
        spec={"steps": [{"action": "request", "method": "GET", "path": "/api/x"}], "assertions": assertions},
        steps=[{"action": "request", "method": "GET", "path": "/api/x"}],
        status="enabled",
    )


def test_assertion_strength_and_project_summary(session, sample_project, client):
    sample_project.settings_json = {}
    case = _case(
        sample_project.id,
        "强弱断言",
        [
            {
                "kind": "status_code",
                "operator": "eq",
                "expected": 200,
                "status": "confirmed",
                "evidence": {"source_type": "none"},
            },
            {
                "kind": "json_path",
                "target": "$.id",
                "operator": "eq",
                "expected": 1,
                "status": "confirmed",
                "evidence": {"source_type": "openapi", "ref": "GET /api/x"},
            },
            {
                "kind": "db_state",
                "sql": "select 1",
                "operator": "eq",
                "expected": 1,
                "status": "confirmed",
                "evidence": {"source_type": "code", "ref": "db.py:1"},
            },
        ],
    )
    session.add(case)
    session.commit()

    quality = case_assertion_quality(case)
    assert quality["assertions"] == 3
    assert quality["band"] == "medium"
    assert quality["weakest"]["kind"] == "status_code"
    summary = client.get(f"/api/projects/{sample_project.id}/assertion-quality").json()
    assert summary["cases"] == 1
    assert summary["assertions"] == 3
    assert summary["bands"]["medium"] == 1


def test_mutation_audit_kills_strong_assertions_and_flags_exists(session, sample_project, client):
    case = _case(sample_project.id, "mutation", [])
    session.add(case)
    session.flush()
    run = Run(project_id=sample_project.id, status="completed", stats={"pass_rate": 1.0})
    session.add(run)
    session.flush()
    session.add(
        CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="pass",
            seq=0,
            response={"status_code": 200, "body": {"name": "alice"}},
            output={
                "assertions": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "actual": 200,
                        "passed": True,
                    },
                    {
                        "kind": "json_path",
                        "target": "$.name",
                        "operator": "eq",
                        "expected": "alice",
                        "actual": "alice",
                        "passed": True,
                    },
                    {
                        "kind": "text_contains",
                        "operator": "contains",
                        "expected": "alice",
                        "actual": "alice",
                        "passed": True,
                    },
                    {
                        "kind": "json_path",
                        "target": "$.name",
                        "operator": "exists",
                        "expected": True,
                        "actual": "alice",
                        "passed": True,
                    },
                ]
            },
        )
    )
    session.commit()

    report = mutation_audit(session, sample_project, limit=10)
    assert report["assertions_checked"] == 4
    assert report["mutations"] > 0
    assert report["mutation_score"] > 0
    assert any(item["operator"] == "exists" for item in report["weak_assertions"])
    api = client.get(f"/api/projects/{sample_project.id}/assertion-quality/mutation").json()
    assert api["assertions_checked"] == 4


def test_oracle_trend_counts_sources(session, sample_project, client):
    run = Run(
        project_id=sample_project.id,
        status="completed",
        stats={"pass_rate": 0.5},
        case_ids=[],
    )
    session.add(run)
    session.flush()
    case = _case(sample_project.id, "trend", [])
    session.add(case)
    session.flush()
    session.add(
        CaseResult(
            run_id=run.id,
            case_id=case.id,
            project_id=sample_project.id,
            status="pass",
            seq=0,
            output={
                "assertions": [
                    {"kind": "status_code", "passed": True, "evidence": {"source_type": "openapi", "ref": "x"}},
                    {"kind": "json_path", "passed": False, "evidence": {"source_type": "policy", "ref": "POLICY-1"}},
                    {"kind": "json_path", "passed": True, "pending": True, "evidence": {"source_type": "manual", "ref": "m"}},
                ]
            },
        )
    )
    session.commit()

    trend = client.get(f"/api/projects/{sample_project.id}/oracle-trend").json()
    assert trend["points"]
    point = trend["points"][-1]
    assert point["run_id"] == run.id
    assert point["assertions"] == 3
    assert point["oracle_mix"]["contract"] == 1
    assert point["oracle_mix"]["policy"] == 1
    assert point["oracle_mix"]["manual"] == 1


def test_no_assertion_case_is_not_double_counted_as_weak(session, sample_project):
    weak = _case(
        sample_project.id,
        "弱断言",
        [
            {
                "kind": "status_code",
                "operator": "eq",
                "expected": 200,
                "status": "confirmed",
                "evidence": {"source_type": "none"},
            }
        ],
    )
    empty = _case(sample_project.id, "无断言", [])
    session.add_all([weak, empty])
    session.commit()

    summary = project_assertion_quality(session, sample_project)

    assert summary["cases"] == 2
    assert summary["bands"]["no_assertions"] == 1
    assert summary["bands"]["weak"] == 1
    assert sum(summary["bands"].values()) == summary["cases"]
