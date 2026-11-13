"""无人值守触发与健康摘要 API。"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.artifact_retention import prune_artifacts
from app.artifact_verifier import verify_artifacts
from app.audit import list_audit
from app.automation import automation_health, trigger_run
from app.config import get_settings
from app.db import get_db
from app.environment_manager import environment_status, start_environment, stop_environment
from app.exploratory import run_differential, run_fuzz, run_metamorphic, run_property
from app.errors import NotFoundError
from app.gate import evaluate_gate
from app.models import Endpoint, Project
from app.performance import persist_performance, run_performance_mode, slo_degradation
from app.process_verifier import run_verification_command, verification_commands
from app.schemas import (
    ArtifactVerifyRequest,
    AutomationTriggerRequest,
    ExploratoryRunRequest,
    PerformanceRunRequest,
    TestDataGenerateRequest,
    VerificationCommandRunRequest,
)
from app.test_data import generate_rows

router = APIRouter(tags=["automation"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


@router.post("/projects/{project_id}/automation/trigger")
def trigger_automation_run(
    project_id: int,
    payload: AutomationTriggerRequest,
    x_shakedown_trigger_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """供部署 / CI / PR 回调触发一次运行；可选项目级 token。"""
    project = _project(db, project_id)
    expected = str((project.settings_json or {}).get("automation_trigger_token") or "")
    if expected and x_shakedown_trigger_token != expected:
        raise HTTPException(status_code=403, detail="自动化触发 token 不匹配")
    result = trigger_run(
        db,
        project,
        policy=payload.policy.model_dump() if payload.policy else {},
        trigger=payload.trigger or "deploy",
        note=payload.note,
    )
    db.commit()
    return result


@router.post("/projects/{project_id}/automation/github")
async def github_automation_webhook(
    project_id: int,
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """GitHub PR / push webhook：验签后按策略触发 run。"""
    project = _project(db, project_id)
    settings = project.settings_json or {}
    secret = str(settings.get("github_webhook_secret") or "")
    if not secret:
        raise HTTPException(status_code=400, detail="项目未配置 github_webhook_secret")
    raw = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="GitHub 签名校验失败")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    event = x_github_event or ""
    action = str(payload.get("action") or "")
    allowed_events = settings.get("github_trigger_events") or ["pull_request", "push"]
    if isinstance(allowed_events, str):
        allowed_events = [allowed_events]
    if event not in set(str(item) for item in allowed_events):
        return {"triggered": False, "reason": f"忽略事件：{event}"}
    if event == "pull_request" and action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
        return {"triggered": False, "reason": f"忽略 PR action：{action}"}
    policy = settings.get("github_trigger_policy") or {"mode": "smoke", "max_cases": 30}
    result = trigger_run(
        db,
        project,
        policy=policy if isinstance(policy, dict) else {},
        trigger=f"github:{event}"[:16],
        note=f"GitHub {event} {action}".strip(),
    )
    db.commit()
    return {"triggered": True, "event": event, "action": action, **result}


@router.post("/projects/{project_id}/automation/gitlab")
async def gitlab_automation_webhook(
    project_id: int,
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """GitLab MR / push webhook：token 校验后按策略触发 run。"""
    project = _project(db, project_id)
    settings = project.settings_json or {}
    secret = str(settings.get("gitlab_webhook_secret") or "")
    if not secret:
        raise HTTPException(status_code=400, detail="项目未配置 gitlab_webhook_secret")
    if not x_gitlab_token or not hmac.compare_digest(secret, x_gitlab_token):
        raise HTTPException(status_code=403, detail="GitLab token 校验失败")
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = {}
    event = (x_gitlab_event or "").lower()
    if "merge request" in event:
        action = str((payload.get("object_attributes") or {}).get("action") or "")
        if action not in {"open", "reopen", "update", "ready"}:
            return {"triggered": False, "reason": f"忽略 MR action：{action}"}
        trigger = "gitlab:mr"
    elif "push" in event:
        trigger = "gitlab:push"
    else:
        return {"triggered": False, "reason": f"忽略事件：{x_gitlab_event}"}
    policy = settings.get("gitlab_trigger_policy") or {"mode": "smoke", "max_cases": 30}
    result = trigger_run(
        db,
        project,
        policy=policy if isinstance(policy, dict) else {},
        trigger=trigger,
        note=f"GitLab {x_gitlab_event}",
    )
    db.commit()
    return {"triggered": True, "event": x_gitlab_event, **result}


@router.post("/projects/{project_id}/exploratory/run")
def run_exploratory(
    project_id: int,
    payload: ExploratoryRunRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """差分 / fuzz / 变形的独立观测入口；不生成硬断言或 confirmed finding。"""
    project = _project(db, project_id)
    method = payload.method.upper()
    if method not in {"GET", "HEAD", "OPTIONS"} and not payload.allow_write:
        raise HTTPException(status_code=400, detail="写方法探索需要显式 allow_write=true")
    try:
        if payload.mode == "differential":
            return run_differential(
                project,
                method=method,
                path=payload.path,
                variants=payload.variants,
                headers=payload.headers,
            )
        if payload.mode == "fuzz":
            return run_fuzz(
                project,
                method=method,
                path=payload.path,
                payloads=payload.payloads,
                headers=payload.headers,
            )
        if payload.mode == "property":
            return run_property(
                project,
                method=method,
                path=payload.path,
                properties=payload.properties,
                cases=payload.cases,
                headers=payload.headers,
                generator=payload.generator,
            )
        return run_metamorphic(
            project,
            method=method,
            path=payload.path,
            body=payload.body,
            read_path=payload.read_path,
            id_field=payload.id_field,
            compare_fields=payload.compare_fields or None,
            headers=payload.headers,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/projects/{project_id}/automation/health")
def get_automation_health(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return automation_health(db, _project(db, project_id))


@router.get("/projects/{project_id}/verification-commands")
def list_verification_commands(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    return {"project_id": project.id, "items": verification_commands(project)}


@router.post("/projects/{project_id}/verification-commands/{name}/run")
def run_project_verification_command(
    project_id: int,
    name: str,
    payload: VerificationCommandRunRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        result = run_verification_command(db, project, name, case_id=payload.case_id)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    db.commit()
    return result


@router.get("/projects/{project_id}/automation/gate")
def get_automation_gate(
    project_id: int,
    run_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        return evaluate_gate(db, project, run_id=run_id)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/projects/{project_id}/audit")
def get_project_audit(
    project_id: int,
    kind: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    return {"project_id": project.id, "items": list_audit(db, project, kind=kind, limit=limit)}


@router.get("/projects/{project_id}/environment/status")
def get_environment_status(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return environment_status(_project(db, project_id))


@router.post("/projects/{project_id}/performance/run")
def run_project_performance(
    project_id: int,
    payload: PerformanceRunRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    try:
        result = run_performance_mode(
            project,
            mode=payload.mode,
            method=payload.method,
            path=payload.path,
            concurrency=payload.concurrency,
            iterations=payload.iterations,
            duration_seconds=payload.duration_seconds,
            headers=payload.headers,
            query=payload.query,
            body=payload.body,
            timeout=payload.timeout,
        )
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    row = persist_performance(db, project, result)
    db.commit()
    result["id"] = row.id
    return result


@router.get("/projects/{project_id}/performance/history")
def performance_history(
    project_id: int,
    path: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models import PerformanceRun

    project = _project(db, project_id)
    stmt = select(PerformanceRun).where(PerformanceRun.project_id == project.id)
    if path:
        stmt = stmt.where(PerformanceRun.path == path)
    rows = list(
        db.scalars(
            stmt.order_by(PerformanceRun.created_at.desc(), PerformanceRun.id.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
    )
    rows.reverse()
    return {
        "project_id": project.id,
        "count": len(rows),
        "items": [
            {
                "id": row.id,
                "method": row.method,
                "path": row.path,
                "concurrency": row.concurrency,
                "iterations": row.iterations,
                "success_rate": row.success_rate,
                "requests_per_second": row.requests_per_second,
                "p50_ms": row.p50_ms,
                "p95_ms": row.p95_ms,
                "p99_ms": row.p99_ms,
                "slo_passed": row.slo_passed,
                "slo": row.slo,
                "mode": (row.slo or {}).get("mode", "baseline") if isinstance(row.slo, dict) else "baseline",
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "trend": {
            "p95_ms": [row.p95_ms for row in rows],
            "p99_ms": [row.p99_ms for row in rows],
            "requests_per_second": [row.requests_per_second for row in rows],
            "success_rate": [row.success_rate for row in rows],
        },
        "degradation": slo_degradation(db, project, path=path),
    }


@router.post("/projects/{project_id}/test-data/generate")
def generate_test_data(
    project_id: int,
    payload: TestDataGenerateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    endpoint = db.get(Endpoint, payload.endpoint_id) if payload.endpoint_id else None
    if endpoint is not None and endpoint.project_id != project.id:
        raise NotFoundError(f"接口不属于当前项目：{payload.endpoint_id}")
    rows = generate_rows(
        project,
        fields=payload.fields,
        endpoint=endpoint,
        count=payload.count,
        seed=payload.seed,
        unicode=payload.unicode,
        distribution=payload.distribution,
    )
    return {"count": len(rows), "rows": rows}


@router.post("/projects/{project_id}/artifacts/verify")
def verify_project_artifacts(
    project_id: int,
    payload: ArtifactVerifyRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return verify_artifacts(_project(db, project_id), payload.paths)


@router.post("/projects/{project_id}/artifacts/prune")
def prune_project_artifacts(
    project_id: int,
    dry_run: bool = True,
    retention_days: int = 30,
    keep_runs: int = 5,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """清理过期 run 的 artifacts；默认 dry_run，不删除数据库结果。"""
    project = _project(db, project_id)
    return prune_artifacts(
        db,
        project,
        artifacts_root=get_settings().artifacts_dir,
        retention_days=retention_days,
        keep_runs=keep_runs,
        dry_run=dry_run,
    )


@router.post("/projects/{project_id}/environment/start")
def start_project_environment(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return start_environment(_project(db, project_id))


@router.post("/projects/{project_id}/environment/stop")
def stop_project_environment(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return stop_environment(_project(db, project_id))
