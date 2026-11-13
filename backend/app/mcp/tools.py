"""MCP 工具实现：直接读项目 DB，不经过内部 HTTP 调用。"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from sqlalchemy import select

from app.api.findings import _findings_for_export, create_finding_integration
from app.api.runs import create_run as create_run_service
from app.api.runs import retry_failed_cases as retry_failed_cases_service
from app.case_verification import record_case_verification
from app.db import SessionLocal
from app.mcp.access import allowed_project_ids, current_client, require_project_access, tenant_allowed
from app.enums import FeedbackAction, FeedbackTarget, FindingStatus, ParseStatus
from app.fixes import claim_fix, claim_work
from app.a11y import audit_metrics, latest_a11y
from app.artifact_verifier import verify_artifacts
from app.audit import list_audit
from app.automation import automation_health, trigger_run
from app.case_enrich import enqueue_case_enrichment, preview_case_enrichment
from app.environment_manager import start_environment, stop_environment
from app.performance import run_performance
from app.process_verifier import run_verification_command, verification_commands
from app.test_data import generate_rows
from app.visual import compare_baseline, latest_screenshot, list_baselines, set_baseline
from app.case_state import (
    apply_execution_state_filter,
    executed_case_ids,
    latest_source_parsed_at,
    states_for_case,
)
from app.exceptions import list_exceptions, update_exception
from app.gaps import collect_case_gaps, filter_gaps, gap_facets, gap_summary, render_gaps_markdown, set_case_gap_state
from app.gate import evaluate_gate
from app.ingest.service import recompute_consistency
from app.integrations.issues import IntegrationError, IntegrationNotConfigured, sync_issue_status
from app.models import (
    CaseResult,
    Endpoint,
    Finding,
    HumanFeedback,
    IntegrationIssue,
    KnowledgeSource,
    Project,
    ProjectMember,
    Run,
    TestCase,
)
from app.worker.queue import enqueue
from app.schemas import RunCreate
from app.reporting.findings_md import (
    build_finding_groups,
    render_findings_markdown,
    summarize_groups,
)
from app.schemas import (
    CaseVerificationCreate,
    CaseVerificationOut,
    FindingFixCreate,
    FindingFixOut,
    FindingIntegrationCreate,
    FindingOut,
)


def _project_name(session, project_id: int) -> str:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"项目不存在：{project_id}")
    return project.name


def list_projects_payload() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        projects = session.scalars(select(Project).order_by(Project.id)).all()
        allowed = allowed_project_ids()
        if allowed:
            projects = [project for project in projects if project.id in allowed]
        client = current_client()
        projects = [project for project in projects if tenant_allowed(project, client)]
        return [{"id": project.id, "name": project.name, "base_url": project.base_url} for project in projects]


def list_members_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        rows = session.scalars(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.role, ProjectMember.actor)
        ).all()
        return {
            "project_id": project_id,
            "items": [
                {
                    "id": row.id,
                    "actor": row.actor,
                    "role": row.role,
                    "active": row.active,
                    "note": row.note,
                }
                for row in rows
            ],
        }


def list_accounts_payload(project_id: int) -> dict[str, Any]:
    from app.crypto import decrypt_json

    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        accounts = decrypt_json(project.test_accounts_encrypted) or {}
        return {
            "project_id": project_id,
            "roles": sorted(accounts.keys()),
            "accounts": [
                {
                    "name": name,
                    "fields": sorted(account.keys()) if isinstance(account, dict) else [],
                }
                for name, account in accounts.items()
            ],
        }


def list_sources_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        _project_name(session, project_id)
        sources = session.scalars(
            select(KnowledgeSource)
            .where(KnowledgeSource.project_id == project_id)
            .order_by(KnowledgeSource.id)
        ).all()
        return {
            "sources": [
                {
                    "id": source.id,
                    "kind": source.kind,
                    "name": source.name,
                    "role": source.role,
                    "location": source.location,
                    "status": source.status,
                    "error": source.error,
                    "version": source.version,
                    "parsed_at": source.parsed_at.isoformat() if source.parsed_at else None,
                }
                for source in sources
            ]
        }


def get_consistency_payload(
    project_id: int,
    *,
    include_weak: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    with SessionLocal() as session:
        _project_name(session, project_id)
        endpoints = session.scalars(
            select(Endpoint)
            .where(Endpoint.project_id == project_id)
            .order_by(Endpoint.path, Endpoint.method)
        ).all()
        counts: dict[str, int] = {}
        items = []
        for endpoint in endpoints:
            info = endpoint.consistency or {}
            state = info.get("state", "unknown")
            counts[state] = counts.get(state, 0) + 1
            if state == "inconsistent" or (include_weak and state == "partial"):
                items.append(
                    {
                        "endpoint_id": endpoint.id,
                        "method": endpoint.method,
                        "path": endpoint.path,
                        "state": state,
                        "sources": info.get("sources") or [],
                        "mismatches": info.get("mismatches") or [],
                    }
                )
        return {
            "project_id": project_id,
            "counts": counts,
            "strong_signals": counts.get("inconsistent", 0) + counts.get("conflict", 0),
            "weak_signals": counts.get("partial", 0),
            "items": items[: max(0, min(limit, 1000))],
        }


def list_case_gaps_payload(
    project_id: int,
    *,
    kind: str = "all",
    include_disabled: bool = False,
    owner_kind: str | None = None,
    execution_state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    _kind = kind if kind in {"all", "pending", "no_steps", "missing_input"} else "all"
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        items = collect_case_gaps(session, project, include_disabled=include_disabled)
        filtered = filter_gaps(
            items,
            _kind,
            owner_kind=owner_kind,
            execution_state=execution_state,
        )
        return {
            "project_id": project_id,
            "summary": gap_summary(items),
            "facets": gap_facets(items),
            "owner_kind": owner_kind or "all",
            "total": len(filtered),
            "items": filtered[: max(0, min(limit, 1000))],
        }


def list_exceptions_payload(
    project_id: int,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        rows = list_exceptions(session, project, status=status, kind=kind, limit=limit)
        return {
            "project_id": project_id,
            "total": len(rows),
            "items": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "severity": row.severity,
                    "status": row.status,
                    "owner": row.owner,
                    "note": row.note,
                    "payload": row.payload,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ],
        }


def update_exception_payload(
    exception_id: int,
    *,
    status: str | None = None,
    owner: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        from app.models import AutomationException

        existing = session.get(AutomationException, exception_id)
        if existing is None:
            raise ValueError(f"例外不存在：{exception_id}")
        require_project_access(existing.project_id, minimum_role="maintainer")
        row = update_exception(session, exception_id, status=status, owner=owner, note=note)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "status": row.status,
            "owner": row.owner,
            "note": row.note,
        }


def automation_health_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return automation_health(session, project)


def trigger_automation_payload(
    project_id: int,
    *,
    policy: dict[str, Any] | None = None,
    trigger: str = "mcp",
    note: str = "",
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        result = trigger_run(session, project, policy=policy or {}, trigger=trigger, note=note)
        session.commit()
        return result


def get_gate_payload(project_id: int, *, run_id: int | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return evaluate_gate(session, project, run_id=run_id)


def list_audit_payload(project_id: int, *, kind: str | None = None, limit: int = 200) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return {"project_id": project_id, "items": list_audit(session, project, kind=kind, limit=limit)}


def verify_artifacts_payload(project_id: int, paths: list[str]) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return verify_artifacts(project, paths)


def environment_action_payload(project_id: int, action: str) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        if action == "start":
            return start_environment(project)
        if action == "stop":
            return stop_environment(project)
        raise ValueError("action 必须是 start 或 stop")


def list_verification_commands_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return {"project_id": project_id, "items": verification_commands(project)}


def run_verification_command_payload(
    project_id: int,
    name: str,
    *,
    case_id: int | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        result = run_verification_command(session, project, name, case_id=case_id)
        session.commit()
        return result


def get_case_a11y_payload(project_id: int, case_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return latest_a11y(session, project, case_id) or {"case_id": case_id, "metrics": {}, "passed": None}


def audit_a11y_payload(metrics: dict[str, Any]) -> dict[str, Any]:
    return audit_metrics(metrics)


def list_visual_baselines_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        rows = list_baselines(session, project)
        return {
            "project_id": project_id,
            "items": [
                {
                    "id": row.id,
                    "case_id": row.case_id,
                    "name": row.name,
                    "sha256": row.sha256,
                    "screenshot_path": row.screenshot_path,
                }
                for row in rows
            ],
        }


def set_visual_baseline_payload(
    project_id: int,
    case_id: int,
    *,
    name: str = "default",
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        row = set_baseline(session, project, case_id, name=name)
        session.commit()
        session.refresh(row)
        return {
            "id": row.id,
            "case_id": row.case_id,
            "name": row.name,
            "sha256": row.sha256,
            "screenshot_path": row.screenshot_path,
        }


def compare_visual_baseline_payload(
    project_id: int,
    case_id: int,
    *,
    name: str = "default",
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return compare_baseline(session, project, case_id, name=name)


def latest_screenshot_payload(project_id: int, case_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return latest_screenshot(session, project, case_id) or {}


def generate_test_data_payload(
    project_id: int,
    *,
    endpoint_id: int | None = None,
    fields: list[dict[str, Any]] | None = None,
    count: int = 1,
    seed: int | None = None,
    unicode: bool = False,
    distribution: str = "uniform",
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        endpoint = session.get(Endpoint, endpoint_id) if endpoint_id else None
        if endpoint is not None and endpoint.project_id != project_id:
            raise ValueError("接口不属于当前项目")
        rows = generate_rows(
            project,
            fields=fields or [],
            endpoint=endpoint,
            count=count,
            seed=seed,
            unicode=unicode,
            distribution=distribution,
        )
        return {"count": len(rows), "rows": rows}


def run_performance_payload(
    project_id: int,
    *,
    method: str = "GET",
    path: str,
    concurrency: int = 5,
    iterations: int = 20,
    duration_seconds: float | None = None,
    headers: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        result = run_performance(
            project,
            method=method,
            path=path,
            concurrency=concurrency,
            iterations=iterations,
            duration_seconds=duration_seconds,
            headers=headers or {},
            query=query or {},
            body=body,
            timeout=timeout,
        )
        from app.performance import persist_performance

        persist_performance(session, project, result)
        session.commit()
        return result


def get_case_gap_report_text(
    project_id: int,
    *,
    include_disabled: bool = False,
    owner_kind: str = "sut-contract",
) -> str:
    """默认只输出 sut-contract 缺口，避免把测试资产缺口混进 SUT 修复清单。"""
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        return render_gaps_markdown(
            project,
            collect_case_gaps(session, project, include_disabled=include_disabled),
            owner_kind=owner_kind or None,
        )


def reparse_source_payload(source_id: int) -> dict[str, Any]:
    """重新解析知识源，只更新 Shakedown 的知识副本，不改被测系统文件。"""
    with SessionLocal() as session:
        source = session.get(KnowledgeSource, source_id)
        if source is None:
            raise ValueError(f"知识源不存在：{source_id}")
        source.status = ParseStatus.PENDING.value
        session.commit()
        task = enqueue(session, "reparse_source", {"source_id": source.id}, project_id=source.project_id)
        session.commit()
        return {"source_id": source.id, "status": source.status, "task_id": task.id}


def run_consistency_check_payload(project_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        recompute_consistency(session, project)
        session.commit()
        counts: dict[str, int] = {}
        for endpoint in session.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all():
            state = (endpoint.consistency or {}).get("state", "unknown")
            counts[state] = counts.get(state, 0) + 1
        return {
            "project_id": project_id,
            "counts": counts,
            "strong_signals": counts.get("inconsistent", 0) + counts.get("conflict", 0),
            "weak_signals": counts.get("partial", 0),
        }


def request_case_enrich_payload(case_id: int) -> dict[str, Any]:
    """让 Shakedown 重新评估用例；只改用例资产，不改被测系统。"""
    with SessionLocal() as session:
        case = session.get(TestCase, case_id)
        if case is None:
            raise ValueError(f"用例不存在：{case_id}")
        require_project_access(case.project_id, minimum_role="maintainer")
        task = enqueue(
            session,
            "enrich_case",
            {"project_id": case.project_id, "case_id": case.id},
            project_id=case.project_id,
        )
        session.commit()
        return {"case_id": case.id, "task_id": task.id, "status": task.status}


def request_case_enrich_batch_payload(
    project_id: int,
    *,
    kind: str = "all",
    owner_kind: str | None = None,
    execution_state: str | None = None,
    missing_input_kind: str | None = None,
    dimension: str | None = None,
    feature_id: int | None = None,
    case_ids: list[int] | None = None,
    include_closed: bool = False,
    include_disabled: bool = False,
    dry_run: bool = True,
    limit: int = 200,
    actor: str = "mcp",
    note: str = "",
) -> dict[str, Any]:
    """批量重新评估缺口用例；dry_run=true 只返回将影响的用例。"""
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        filters = {
            "kind": kind,
            "owner_kind": owner_kind,
            "execution_state": execution_state,
            "missing_input_kind": missing_input_kind,
            "dimension": dimension,
            "feature_id": feature_id,
            "include_closed": include_closed,
            "include_disabled": include_disabled,
        }
        if dry_run:
            return {
                "dry_run": True,
                **preview_case_enrichment(
                    session,
                    project,
                    filters=filters,
                    case_ids=case_ids,
                    limit=limit,
                ),
            }
        result = enqueue_case_enrichment(
            session,
            project,
            filters=filters,
            case_ids=case_ids,
            limit=limit,
            actor=actor,
            note=note,
        )
        session.commit()
        return {"dry_run": False, **result}


def set_case_gap_status_payload(
    project_id: int,
    case_id: int,
    *,
    status: str,
    owner: str = "",
    note: str = "",
) -> dict[str, Any]:
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        state = set_case_gap_state(
            session, project, case_id, status=status, owner=owner, note=note, source="mcp"
        )
        return {
            "project_id": project_id,
            "case_id": state.case_id,
            "status": state.status,
            "owner": state.owner,
            "note": state.note,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }


def claim_finding_payload(
    finding_id: int,
    *,
    assignee: str,
    note: str = "",
) -> dict[str, Any]:
    with SessionLocal() as session:
        finding = session.get(Finding, finding_id)
        if finding is None:
            raise ValueError(f"缺陷不存在：{finding_id}")
        require_project_access(finding.project_id, minimum_role="maintainer")
        record = claim_work(session, finding, actor=assignee, note=note)
        session.commit()
        session.refresh(finding)
        return {
            "finding_id": finding.id,
            "fix_status": finding.fix_status,
            "assignee": finding.assignee,
            "fix_id": record.id,
        }


def set_finding_status_payload(
    finding_id: int,
    *,
    status: str,
    note: str = "",
    assignee: str | None = None,
) -> dict[str, Any]:
    if status not in {item.value for item in FindingStatus}:
        raise ValueError(f"非法缺陷状态：{status}")
    with SessionLocal() as session:
        finding = session.get(Finding, finding_id)
        if finding is None:
            raise ValueError(f"缺陷不存在：{finding_id}")
        require_project_access(finding.project_id, minimum_role="maintainer")
        before = {"status": finding.status, "assignee": finding.assignee}
        now = datetime.now(timezone.utc)
        finding.status = status
        if assignee is not None:
            finding.assignee = assignee.strip()[:120]
        finding.confirmed_by = "mcp" if status != FindingStatus.PENDING.value else None
        finding.confirmed_at = now if status != FindingStatus.PENDING.value else None
        finding.resolution = (
            {}
            if status == FindingStatus.PENDING.value
            else {"status": status, "note": note, "actor": "mcp", "at": now.isoformat()}
        )
        action = {
            FindingStatus.CONFIRMED.value: FeedbackAction.CONFIRM.value,
            FindingStatus.FALSE_POSITIVE.value: FeedbackAction.FALSE_POSITIVE.value,
            FindingStatus.ACCEPTED.value: FeedbackAction.ACCEPT.value,
            FindingStatus.WONT_FIX.value: FeedbackAction.WONT_FIX.value,
        }.get(status, FeedbackAction.MODIFY.value)
        session.add(
            HumanFeedback(
                project_id=finding.project_id,
                target_type=FeedbackTarget.FINDING.value,
                target_id=finding.id,
                action=action,
                before=before,
                after={"status": status, "assignee": finding.assignee},
                note=note,
                author="mcp",
            )
        )
        session.commit()
        session.refresh(finding)
        return {
            "finding_id": finding.id,
            "status": finding.status,
            "assignee": finding.assignee,
            "resolution": finding.resolution,
        }


def sync_external_issues_payload(project_id: int) -> dict[str, Any]:
    """同步外部工单状态；只更新 IntegrationIssue.status，不代替回归验证。"""
    with SessionLocal() as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError(f"项目不存在：{project_id}")
        rows = list(
            session.scalars(select(IntegrationIssue).where(IntegrationIssue.project_id == project_id)).all()
        )
        synced = 0
        failed = 0
        items = []
        for row in rows:
            try:
                status = sync_issue_status(row.provider, row.external_key, project)
            except (IntegrationNotConfigured, IntegrationError) as exc:
                failed += 1
                items.append({"provider": row.provider, "external_key": row.external_key, "error": str(exc)})
                continue
            row.status = status[:16]
            synced += 1
            items.append(
                {
                    "provider": row.provider,
                    "external_key": row.external_key,
                    "external_url": row.external_url,
                    "status": row.status,
                    "finding_ids": row.finding_ids,
                }
            )
        session.commit()
        return {"synced": synced, "failed": failed, "items": items}


def _case_summary(case: TestCase) -> dict[str, Any]:
    return {
        "id": case.id,
        "project_id": case.project_id,
        "feature_id": case.feature_id,
        "title": case.title,
        "dimension": case.dimension,
        "case_type": case.case_type,
        "status": case.status,
        "priority": case.priority,
        "tags": list(case.tags or []),
        "missing_input": case.missing_input,
        "verification_mode": case.verification_mode,
        "verification_status": case.verification_status,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
    }


def _run_summary(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "project_id": run.project_id,
        "status": run.status,
        "trigger": run.trigger,
        "case_ids": list(run.case_ids or []),
        "stats": run.stats or {},
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def list_cases_payload(
    project_id: int,
    *,
    status: str | None = None,
    dimension: str | None = None,
    q: str | None = None,
    execution_state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    with SessionLocal() as session:
        _project_name(session, project_id)
        stmt = select(TestCase).where(TestCase.project_id == project_id)
        if status:
            stmt = stmt.where(TestCase.status == status)
        if dimension:
            stmt = stmt.where(TestCase.dimension == dimension)
        if q:
            stmt = stmt.where(TestCase.title.ilike(f"%{q}%"))
        stmt = apply_execution_state_filter(stmt, session, project_id, execution_state)
        cases = session.scalars(stmt.order_by(TestCase.priority, TestCase.id).limit(max(0, min(limit, 1000)))).all()
        executed = executed_case_ids(session, project_id, [case.id for case in cases])
        latest_parsed_at = latest_source_parsed_at(session, project_id)
        return {
            "cases": [
                {
                    **_case_summary(case),
                    "execution_states": states_for_case(
                        case, executed=executed, latest_parsed_at=latest_parsed_at
                    ),
                }
                for case in cases
            ]
        }


def get_case_payload(case_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        case = session.get(TestCase, case_id)
        if case is None:
            raise ValueError(f"用例不存在：{case_id}")
        require_project_access(case.project_id)
        data = _case_summary(case)
        executed = executed_case_ids(session, case.project_id, [case.id])
        data["execution_states"] = states_for_case(
            case,
            executed=executed,
            latest_parsed_at=latest_source_parsed_at(session, case.project_id),
        )
        data.update(
            {
                "spec": case.spec or {},
                "steps": case.steps or [],
                "expected": case.expected or {},
                "evidence": case.evidence or [],
                "preconditions": case.preconditions or {},
            }
        )
        return data


def list_run_results_payload(
    run_id: int,
    *,
    status: str | None = None,
    include_payload: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    with SessionLocal() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"执行记录不存在：{run_id}")
        require_project_access(run.project_id)
        stmt = select(CaseResult).where(CaseResult.run_id == run_id, CaseResult.rerun.is_(False))
        if status:
            stmt = stmt.where(CaseResult.status == status)
        rows = session.scalars(stmt.order_by(CaseResult.seq).limit(max(0, min(limit, 1000)))).all()
        cases = {
            case.id: case
            for case in session.scalars(
                select(TestCase).where(TestCase.id.in_([row.case_id for row in rows]))
            ).all()
        }
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        results = []
        for row in rows:
            case = cases.get(row.case_id)
            output = row.output or {}
            item = {
                "result_id": row.id,
                "case_id": row.case_id,
                "case_title": case.title if case else None,
                "dimension": case.dimension if case else None,
                "status": row.status,
                "duration_ms": row.duration_ms,
                "errors": output.get("errors") or [],
                "failed_step_index": output.get("failed_step_index"),
                "attribution": row.attribution or {},
                "logs": (row.logs or "")[:2000],
            }
            if include_payload:
                item["request"] = row.request or {}
                item["response"] = row.response or {}
                item["output"] = output
            results.append(item)
        return {"run": _run_summary(run), "counts": counts, "results": results}


def get_run_payload(run_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"执行记录不存在：{run_id}")
        require_project_access(run.project_id)
        return _run_summary(run)


def start_run_payload(
    project_id: int,
    *,
    case_ids: list[int] | None = None,
    dimensions: list[str] | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        run = create_run_service(
            project_id,
            RunCreate(case_ids=case_ids, dimensions=dimensions, trigger="agent"),
            db=session,
        )
        return _run_summary(run)


def rerun_failed_payload(run_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        source = session.get(Run, run_id)
        if source is None:
            raise ValueError(f"执行记录不存在：{run_id}")
        require_project_access(source.project_id, minimum_role="maintainer")
        run = retry_failed_cases_service(run_id, db=session)
        return _run_summary(run)


def list_finding_groups_payload(
    project_id: int,
    *,
    verdict: str = "actionable",
    severity: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        findings = _findings_for_export(
            session,
            project_id,
            source=source,
            status=status,
            severity=severity,
            module=None,
            risk_type=None,
            q=None,
            verdict=verdict,
            limit=5000,
        )
        groups = build_finding_groups(findings)
    return {
        "summary": summarize_groups(groups),
        "groups": [
            {
                "group_key": group["id"],
                "title": group["title"],
                "verdict": group["verdict"],
                "priority": group["priority"],
                "severity": group["severity"],
                "files": group["files"],
                "member_ids": group["member_ids"],
            }
            for group in groups
        ],
    }


def get_finding_payload(finding_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        finding = session.get(Finding, finding_id)
        if finding is None:
            raise ValueError(f"缺陷不存在：{finding_id}")
        require_project_access(finding.project_id)
        return FindingOut.model_validate(finding).model_dump(mode="json")


def get_fix_plan_text(
    project_id: int,
    *,
    verdict: str = "actionable",
    severity: str | None = None,
    status: str | None = None,
    source: str | None = None,
) -> str:
    with SessionLocal() as session:
        project_name = _project_name(session, project_id)
        findings = _findings_for_export(
            session,
            project_id,
            source=source,
            status=status,
            severity=severity,
            module=None,
            risk_type=None,
            q=None,
            verdict=verdict,
            limit=5000,
        )
        groups = build_finding_groups(findings)
    return render_findings_markdown(
        project_name=project_name,
        project_id=project_id,
        groups=groups,
        filters={
            "verdict": verdict,
            "severity": severity,
            "status": status,
            "source": source,
        },
    )


def create_issue_payload(project_id: int, provider: str, group_key: str) -> dict[str, Any]:
    with SessionLocal() as session:
        result = create_finding_integration(
            project_id=project_id,
            provider=provider,
            payload=FindingIntegrationCreate(group_key=group_key),
            db=session,
        )
        return dict(result)


def report_fix_payload(
    finding_id: int,
    *,
    commit_sha: str = "",
    pr_url: str = "",
    files: list[str] | None = None,
    note: str = "",
    test_command: str = "",
    test_result: str = "",
    actor: str = "mcp",
) -> dict[str, Any]:
    with SessionLocal() as session:
        finding = session.get(Finding, finding_id)
        if finding is None:
            raise ValueError(f"缺陷不存在：{finding_id}")
        require_project_access(finding.project_id, minimum_role="maintainer")
        record = claim_fix(
            session,
            finding,
            FindingFixCreate(
                commit_sha=commit_sha,
                pr_url=pr_url,
                files=files or [],
                note=note,
                test_command=test_command,
                test_result=test_result,
            ),
            actor=actor,
            source="mcp",
        )
        session.commit()
        session.refresh(finding)
        session.refresh(record)
        return {
            "finding": FindingOut.model_validate(finding).model_dump(mode="json"),
            "fix": FindingFixOut.model_validate(record).model_dump(mode="json"),
        }


def report_case_verification_payload(
    case_id: int,
    *,
    passed: bool,
    mode: str | None = None,
    command: str = "",
    result: str = "",
    artifacts: list[str] | None = None,
    note: str = "",
    actor: str = "mcp",
    commit_sha: str = "",
    artifact_hashes: dict[str, str] | None = None,
    env_fingerprint: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        case = session.get(TestCase, case_id)
        if case is None:
            raise ValueError(f"用例不存在：{case_id}")
        require_project_access(case.project_id, minimum_role="maintainer")
        record = record_case_verification(
            session,
            case,
            CaseVerificationCreate(
                passed=passed,
                mode=mode,
                command=command,
                result=result,
                artifacts=artifacts or [],
                note=note,
                actor=actor,
                commit_sha=commit_sha,
                artifact_hashes=artifact_hashes or {},
                env_fingerprint=env_fingerprint or {},
                expires_at=expires_at,
            ),
            actor=actor,
            source="mcp",
        )
        session.commit()
        session.refresh(case)
        session.refresh(record)
        return {
            "case": {
                "id": case.id,
                "verification_mode": case.verification_mode,
                "verification_status": case.verification_status,
                "verified_at": case.verified_at.isoformat() if case.verified_at else None,
                "verified_by": case.verified_by,
            },
            "verification": CaseVerificationOut.model_validate(record).model_dump(mode="json"),
        }


def _guard_payload(fn):
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            bound = signature.bind_partial(*args, **kwargs)
            project_id = bound.arguments.get("project_id")
        except TypeError:
            project_id = None
        if project_id is not None:
            minimum = "maintainer" if _is_write_payload(fn.__name__) else "viewer"
            require_project_access(int(project_id), minimum_role=minimum)
        return fn(*args, **kwargs)

    return wrapper


def _is_write_payload(name: str) -> bool:
    """按 payload 名称判断写操作；只用于 MCP 角色下限，最终仍由 scope 校验。"""
    if name.startswith(
        (
            "set_",
            "update_",
            "report_",
            "request_",
            "trigger_",
            "start_",
            "rerun_",
            "claim_",
            "create_",
            "sync_",
            "reparse_",
            "audit_",
            "compare_",
        )
    ):
        return True
    return name in {
        "run_consistency_check_payload",
        "run_verification_command_payload",
        "environment_action_payload",
        "verify_artifacts_payload",
    }


# 所有直接接收 project_id 的 payload 统一做 MCP 项目权限校验。
for _name, _fn in list(globals().items()):
    if (
        inspect.isfunction(_fn)
        and not _name.startswith("_")
        and _name.endswith(("_payload", "_text"))
        and "project_id" in inspect.signature(_fn).parameters
    ):
        globals()[_name] = _guard_payload(_fn)



