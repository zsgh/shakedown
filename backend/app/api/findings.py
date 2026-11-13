"""缺陷（finding）API。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.case_review import false_positive_recheck_candidates
from app.enums import FeedbackAction, FeedbackTarget, FindingStatus
from app.errors import NotFoundError
from app.fixes import claim_fix, claim_work, fix_history, verify_fix
from app.integrations.issues import (
    IntegrationError,
    IntegrationNotConfigured,
    build_issue_draft,
    create_issue,
    integration_status,
    sync_issue_status,
)
from app.models import CaseResult, Finding, HumanFeedback, IntegrationIssue, Project
from app.reporting.findings_md import (
    build_finding_groups,
    build_findings_export,
    render_findings_markdown,
)
from app.schemas import (
    FindingAutoVerify,
    FindingBatchUpdate,
    FindingClaim,
    FindingFixCreate,
    FindingFixOut,
    FindingFixVerify,
    FindingIntegrationCreate,
    FindingMergeRequest,
    FindingOut,
    FindingUpdate,
)
from app.worker.queue import enqueue

router = APIRouter(tags=["findings"])


@router.get("/projects/{project_id}/findings", response_model=list[FindingOut])
def list_findings(
    project_id: int,
    source: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    module: str | None = None,
    risk_type: str | None = None,
    fix_status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=200, ge=0, le=1000),
    db: Session = Depends(get_db),
) -> list[Finding]:
    stmt = select(Finding).where(Finding.project_id == project_id)
    if source:
        stmt = stmt.where(Finding.source == source)
    if status:
        stmt = stmt.where(Finding.status == status)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if module:
        stmt = stmt.where(Finding.module == module)
    if risk_type:
        stmt = stmt.where(Finding.risk_type == risk_type)
    if fix_status:
        stmt = stmt.where(Finding.fix_status == fix_status)
    if q:
        stmt = stmt.where(Finding.title.ilike(f"%{q}%"))
    return list(db.scalars(stmt.order_by(Finding.created_at.desc()).limit(limit)).all())


def _matches_verdict(finding: Finding, verdict: str) -> bool:
    # 人工结论优先于 AI 分诊：已关闭/已标误报的条目不能因为 AI 仍判 real_bug 就继续出现在修复清单里。
    closed_status = {
        FindingStatus.FALSE_POSITIVE.value,
        FindingStatus.ACCEPTED.value,
        FindingStatus.WONT_FIX.value,
    }
    if finding.status in closed_status:
        return verdict in {finding.status, "all"} or (verdict == "ignore" and finding.status in closed_status)
    actual = (
        "untriaged"
        if finding.triage_status == "untriaged"
        else str((finding.triage or {}).get("verdict") or "untriaged")
    )
    if verdict == "all":
        return True
    if verdict == "closed":
        return finding.status in closed_status
    if verdict == "actionable":
        # 人工确认过的 finding 优先于 AI 分诊结论。
        if getattr(finding, "fix_status", "open") == "verified_fixed":
            return False
        if finding.status == FindingStatus.CONFIRMED.value:
            return True
        return actual in {"real_bug", "needs_verification"} or (
            finding.source == "execution" and finding.status == "confirmed"
        )
    return actual == verdict


def _findings_for_export(
    db: Session,
    project_id: int,
    *,
    source: str | None,
    status: str | None,
    severity: str | None,
    module: str | None,
    risk_type: str | None,
    q: str | None,
    verdict: str,
    limit: int,
) -> list[Finding]:
    stmt = select(Finding).where(Finding.project_id == project_id)
    if source:
        stmt = stmt.where(Finding.source == source)
    if status:
        stmt = stmt.where(Finding.status == status)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if module:
        stmt = stmt.where(Finding.module == module)
    if risk_type:
        stmt = stmt.where(Finding.risk_type == risk_type)
    if q:
        stmt = stmt.where(Finding.title.ilike(f"%{q}%"))
    rows = list(
        db.scalars(stmt.order_by(Finding.created_at.desc()).limit(limit)).all()
    )
    return [finding for finding in rows if _matches_verdict(finding, verdict)]


def _export_filters(
    *,
    source: str | None,
    status: str | None,
    severity: str | None,
    module: str | None,
    risk_type: str | None,
    q: str | None,
    verdict: str,
) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "severity": severity,
        "module": module,
        "risk_type": risk_type,
        "q": q,
        "verdict": verdict,
    }


@router.get("/projects/{project_id}/findings/export.md")
def export_findings_markdown(
    project_id: int,
    source: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    module: str | None = None,
    risk_type: str | None = None,
    q: str | None = None,
    verdict: str = Query(
        default="actionable",
        description="actionable（建议修复 + 需验证）/ real_bug / needs_verification / false_positive / duplicate / untriaged / all",
    ),
    limit: int = Query(default=2000, ge=0, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    """导出 AI 可直接执行的 Markdown 缺陷修复清单（按分诊根因分组）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    findings = _findings_for_export(
        db,
        project_id,
        source=source,
        status=status,
        severity=severity,
        module=module,
        risk_type=risk_type,
        q=q,
        verdict=verdict,
        limit=limit,
    )
    groups = build_finding_groups(findings)
    filters = _export_filters(
        source=source,
        status=status,
        severity=severity,
        module=module,
        risk_type=risk_type,
        q=q,
        verdict=verdict,
    )
    markdown = render_findings_markdown(
        project_name=project.name,
        project_id=project_id,
        groups=groups,
        filters=filters,
    )
    filename = f"findings-fix-{project_id}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(filename)}"},
    )


@router.get("/projects/{project_id}/findings/export.json")
def export_findings_json(
    project_id: int,
    source: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    module: str | None = None,
    risk_type: str | None = None,
    q: str | None = None,
    verdict: str = Query(
        default="actionable",
        description="actionable / real_bug / needs_verification / false_positive / duplicate / untriaged / all",
    ),
    limit: int = Query(default=2000, ge=0, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    """导出结构化缺陷修复清单，供自动化 / Agent 工具消费。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    findings = _findings_for_export(
        db,
        project_id,
        source=source,
        status=status,
        severity=severity,
        module=module,
        risk_type=risk_type,
        q=q,
        verdict=verdict,
        limit=limit,
    )
    groups = build_finding_groups(findings)
    filters = _export_filters(
        source=source,
        status=status,
        severity=severity,
        module=module,
        risk_type=risk_type,
        q=q,
        verdict=verdict,
    )
    payload = build_findings_export(
        project_name=project.name,
        project_id=project_id,
        groups=groups,
        filters=filters,
    )
    filename = f"findings-fix-{project_id}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(filename)}"},
    )


@router.get("/integrations/status")
def get_integration_status() -> dict[str, Any]:
    """检查 GitHub / Jira 是否配置完整（不返回密钥）。"""
    return integration_status()


@router.get("/projects/{project_id}/findings/integrations/status")
def get_project_integration_status(
    project_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """返回项目级工单集成状态：全局凭据 + 项目目标仓库 / Jira 项目。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return integration_status(project)


@router.get("/projects/{project_id}/findings/integrations")
def list_finding_integrations(
    project_id: int,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    rows = db.scalars(
        select(IntegrationIssue)
        .where(IntegrationIssue.project_id == project_id)
        .order_by(IntegrationIssue.created_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "provider": row.provider,
            "group_key": row.group_key,
            "title": row.title,
            "external_key": row.external_key,
            "external_url": row.external_url,
            "finding_ids": row.finding_ids,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/projects/{project_id}/findings/integrations/sync")
def sync_finding_integrations(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """同步外部工单状态；只更新“工单状态”，不代替 Shakedown 判定缺陷已修复。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    rows = list(
        db.scalars(
            select(IntegrationIssue).where(IntegrationIssue.project_id == project_id)
        ).all()
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
    db.commit()
    return {"synced": synced, "failed": failed, "items": items}


@router.post("/projects/{project_id}/findings/integrations/{provider}")
def create_finding_integration(
    project_id: int,
    provider: str,
    payload: FindingIntegrationCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """把一个 AI 分诊后的修复组创建为 GitHub Issue / Jira 工单。

    ``dry_run=true`` 只返回标题和正文预览，不调用外部服务、不落库。
    """
    if provider not in {"github", "jira"}:
        raise HTTPException(status_code=400, detail="provider 只支持 github / jira")
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    findings = _findings_for_export(
        db,
        project_id,
        source=None,
        status=None,
        severity=None,
        module=None,
        risk_type=None,
        q=None,
        verdict="all",
        limit=5000,
    )
    group = next(
        (item for item in build_finding_groups(findings) if item["id"] == payload.group_key),
        None,
    )
    if group is None:
        raise NotFoundError(f"修复组不存在：{payload.group_key}")
    draft = build_issue_draft(project.name, group)

    if payload.dry_run:
        return {"status": "preview", "provider": provider, "draft": draft}

    existing = db.scalar(
        select(IntegrationIssue).where(
            IntegrationIssue.project_id == project_id,
            IntegrationIssue.provider == provider,
            IntegrationIssue.group_key == payload.group_key,
        )
    )
    if existing is not None:
        return {
            "status": "existing",
            "provider": provider,
            "external_key": existing.external_key,
            "external_url": existing.external_url,
        }

    try:
        created = create_issue(provider, draft, project)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    record = IntegrationIssue(
        project_id=project_id,
        provider=provider,
        group_key=payload.group_key,
        title=draft["title"],
        external_key=created.get("external_key", ""),
        external_url=created.get("external_url", ""),
        finding_ids=draft["finding_ids"],
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "status": "created",
        "provider": provider,
        "external_key": record.external_key,
        "external_url": record.external_url,
        "title": record.title,
    }


@router.get("/projects/{project_id}/findings/recheck-candidates")
def finding_recheck_candidates(
    project_id: int,
    snapshot_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按最新 spec 快照变化，列出可能需要重新打开的历史误报。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return false_positive_recheck_candidates(db, project, snapshot_id=snapshot_id)


@router.get("/projects/{project_id}/findings/clusters")
def finding_clusters(
    project_id: int,
    include_closed: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按 cluster_key 聚合缺陷，附跨 run / 跨用例关联。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    stmt = select(Finding).where(Finding.project_id == project.id)
    if not include_closed:
        stmt = stmt.where(Finding.status.not_in(["false_positive", "accepted", "wont_fix"]))
    findings = list(db.scalars(stmt.order_by(Finding.id)).all())
    result_ids = [finding.result_id for finding in findings if finding.result_id]
    result_rows = db.execute(
        select(CaseResult.id, CaseResult.run_id, CaseResult.case_id).where(CaseResult.id.in_(result_ids))
    ).all() if result_ids else []
    run_by_result = {row[0]: row[1] for row in result_rows}
    case_by_result = {row[0]: row[2] for row in result_rows}
    groups: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = finding.cluster_key or (f"finding:{finding.duplicate_of}" if finding.duplicate_of else f"finding:{finding.id}")
        group = groups.setdefault(
            key,
            {
                "cluster_key": key,
                "finding_ids": [],
                "case_ids": set(),
                "run_ids": set(),
                "severities": {},
                "titles": [],
                "duplicate_ids": [],
            },
        )
        group["finding_ids"].append(finding.id)
        linked_case_id = finding.case_id or case_by_result.get(finding.result_id)
        if linked_case_id:
            group["case_ids"].add(linked_case_id)
        if finding.result_id and run_by_result.get(finding.result_id):
            group["run_ids"].add(run_by_result[finding.result_id])
        group["severities"][finding.severity] = group["severities"].get(finding.severity, 0) + 1
        group["titles"].append(finding.title)
        if finding.duplicate_of:
            group["duplicate_ids"].append(finding.id)
    items = [
        {
            **{k: v for k, v in group.items() if k not in {"case_ids", "run_ids", "titles"}},
            "case_ids": sorted(group["case_ids"]),
            "run_ids": sorted(group["run_ids"]),
            "count": len(group["finding_ids"]),
            "title": group["titles"][0] if group["titles"] else "",
            "cross_run": len(group["run_ids"]) > 1,
        }
        for group in groups.values()
    ]
    items.sort(key=lambda item: (-item["count"], item["cluster_key"]))
    return {
        "project_id": project.id,
        "total": len(items),
        "findings": len(findings),
        "items": items[:limit],
    }


@router.post("/findings/{finding_id}/merge")
def merge_finding(
    finding_id: int,
    payload: FindingMergeRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """人工把一条 finding 合并到同项目的根 finding。"""
    root = db.get(Finding, finding_id)
    duplicate = db.get(Finding, payload.duplicate_id)
    if root is None or duplicate is None:
        raise NotFoundError("finding 不存在")
    if root.project_id != duplicate.project_id:
        raise HTTPException(status_code=409, detail="不能跨项目合并")
    if root.id == duplicate.id:
        raise HTTPException(status_code=409, detail="不能合并到自己")
    if root.duplicate_of:
        root = db.get(Finding, root.duplicate_of) or root
    duplicate.duplicate_of = root.id
    duplicate.cluster_key = root.cluster_key or f"finding:{root.id}"
    triage = duplicate.triage if isinstance(duplicate.triage, dict) else {}
    duplicate.triage = {
        **triage,
        "verdict": "duplicate",
        "classification": "test_issue",
        "reason": payload.note or f"人工合并到 finding #{root.id}",
        "duplicate_of": root.id,
    }
    duplicate.triage_status = "merged"
    duplicate.resolution = {
        **(duplicate.resolution or {}),
        "merged_into": root.id,
        "note": payload.note or "",
    }
    db.add(
        HumanFeedback(
            project_id=root.project_id,
            target_type=FeedbackTarget.FINDING.value,
            target_id=duplicate.id,
            action=FeedbackAction.MODIFY.value,
            before={"duplicate_of": None},
            after={"duplicate_of": root.id, "note": payload.note},
            note=payload.note or "人工合并重复 finding",
        )
    )
    db.commit()
    return {
        "root_id": root.id,
        "duplicate_id": duplicate.id,
        "cluster_key": duplicate.cluster_key,
        "status": duplicate.status,
        "duplicate_of": duplicate.duplicate_of,
    }


@router.post("/projects/{project_id}/findings/triage")
def triage_findings(
    project_id: int,
    source: str = Query(default="whitebox", description="whitebox / execution / all"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """AI 分诊：聚类 + 判定 + 排序，只写派生字段，不删除 finding。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    task = enqueue(
        db,
        "triage_findings",
        {"project_id": project_id, "source": "whitebox" if source == "whitebox" else source},
        project_id=project_id,
    )
    db.commit()
    return {"task_id": task.id, "status": task.status}


@router.post("/projects/{project_id}/findings/batch-status")
def batch_finding_status(
    project_id: int,
    payload: FindingBatchUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按组批量确认 / 标记误报 / 重新打开；原始 finding 字段不修改。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    if payload.status not in {item.value for item in FindingStatus}:
        raise NotFoundError(f"非法状态：{payload.status}")
    findings = db.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.id.in_(payload.finding_ids),
        )
    ).all()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    action = {
        FindingStatus.CONFIRMED.value: FeedbackAction.CONFIRM.value,
        FindingStatus.FALSE_POSITIVE.value: FeedbackAction.FALSE_POSITIVE.value,
        FindingStatus.ACCEPTED.value: FeedbackAction.ACCEPT.value,
        FindingStatus.WONT_FIX.value: FeedbackAction.WONT_FIX.value,
    }.get(payload.status, FeedbackAction.MODIFY.value)
    for finding in findings:
        before = {"status": finding.status, "triage_status": finding.triage_status}
        finding.status = payload.status
        finding.triage_status = "human_reviewed"
        finding.confirmed_by = "local" if payload.status != FindingStatus.PENDING.value else None
        finding.confirmed_at = now if payload.status != FindingStatus.PENDING.value else None
        if payload.assignee is not None:
            finding.assignee = payload.assignee.strip()[:120]
        if payload.status == FindingStatus.PENDING.value:
            finding.resolution = {}
        else:
            finding.resolution = {
                "status": payload.status,
                "note": payload.note,
                "actor": "local",
                "at": now.isoformat(),
            }
        db.add(
            HumanFeedback(
                project_id=project_id,
                target_type=FeedbackTarget.FINDING.value,
                target_id=finding.id,
                action=action,
                before=before,
                after={
                    "status": finding.status,
                    "triage_status": finding.triage_status,
                    "assignee": finding.assignee,
                },
                note=payload.note,
            )
        )
    db.commit()
    return {"updated": len(findings)}


@router.get("/findings/{finding_id}", response_model=FindingOut)
def get_finding(finding_id: int, db: Session = Depends(get_db)) -> Finding:
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    return finding


@router.post("/findings/{finding_id}/claim")
def claim_finding_work(
    finding_id: int,
    payload: FindingClaim,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """编码 Agent 认领修复工作，避免多个 Agent 重复处理同一条缺陷。"""
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    try:
        record = claim_work(db, finding, actor=payload.assignee, note=payload.note)
    except ValueError as exc:
        from app.errors import ConflictError

        raise ConflictError(str(exc)) from exc
    db.commit()
    db.refresh(finding)
    db.refresh(record)
    return {
        "finding": FindingOut.model_validate(finding).model_dump(mode="json"),
        "fix": FindingFixOut.model_validate(record).model_dump(mode="json"),
    }


@router.post("/findings/{finding_id}/fix")
def report_finding_fix(
    finding_id: int,
    payload: FindingFixCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Agent / 工程师提交修复声明；状态先进入 claimed_fixed，等待验证。"""
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    try:
        record = claim_fix(db, finding, payload, actor=payload.actor or "manual", source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(finding)
    db.refresh(record)
    return {
        "finding": FindingOut.model_validate(finding).model_dump(mode="json"),
        "fix": FindingFixOut.model_validate(record).model_dump(mode="json"),
    }


@router.post("/findings/{finding_id}/fix/verify")
def verify_finding_fix(
    finding_id: int,
    payload: FindingFixVerify,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """人工 / 回归测试验证修复；通过则 verified_fixed，否则 reopened。"""
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    try:
        record = verify_fix(db, finding, payload, actor=payload.actor or "manual", source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(finding)
    db.refresh(record)
    return {
        "finding": FindingOut.model_validate(finding).model_dump(mode="json"),
        "fix": FindingFixOut.model_validate(record).model_dump(mode="json"),
    }


@router.post("/projects/{project_id}/findings/auto-verify")
def auto_verify_fixes(
    project_id: int,
    payload: FindingAutoVerify,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """触发待复核修复的自动验证任务。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    task = enqueue(
        db,
        "verify_fixes",
        {"project_id": project_id, "finding_ids": payload.finding_ids or []},
        project_id=project_id,
    )
    db.commit()
    return {"task_id": task.id, "status": task.status}


@router.get("/findings/{finding_id}/fix-history", response_model=list[FindingFixOut])
def get_finding_fix_history(finding_id: int, db: Session = Depends(get_db)) -> list[Any]:
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    return fix_history(db, finding_id)


@router.patch("/findings/{finding_id}", response_model=FindingOut)
def update_finding(finding_id: int, payload: FindingUpdate, db: Session = Depends(get_db)) -> Finding:
    finding = db.get(Finding, finding_id)
    if finding is None:
        raise NotFoundError(f"缺陷不存在：{finding_id}")
    before = {"status": finding.status, "severity": finding.severity, "title": finding.title}
    data = payload.model_dump(exclude_unset=True)
    if data.get("status"):
        if data["status"] not in {s.value for s in FindingStatus}:
            raise NotFoundError(f"非法状态：{data['status']}")
        finding.status = data["status"]
        finding.confirmed_by = data.get("confirmed_by") or ("local" if data["status"] != FindingStatus.PENDING.value else None)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        finding.confirmed_at = now if data["status"] != FindingStatus.PENDING.value else None
        if data["status"] == FindingStatus.PENDING.value:
            finding.resolution = {}
        else:
            finding.resolution = {
                "status": data["status"],
                "note": payload.note,
                "actor": data.get("confirmed_by") or "local",
                "at": now.isoformat(),
            }
    if payload.assignee is not None:
        finding.assignee = payload.assignee.strip()[:120]
    for key in ("severity", "title", "description"):
        if data.get(key):
            setattr(finding, key, data[key])
    db.commit()
    db.refresh(finding)
    action = {
        FindingStatus.CONFIRMED.value: FeedbackAction.CONFIRM.value,
        FindingStatus.FALSE_POSITIVE.value: FeedbackAction.FALSE_POSITIVE.value,
        FindingStatus.ACCEPTED.value: FeedbackAction.ACCEPT.value,
        FindingStatus.WONT_FIX.value: FeedbackAction.WONT_FIX.value,
    }.get(finding.status, FeedbackAction.MODIFY.value)
    db.add(
        HumanFeedback(
            project_id=finding.project_id,
            target_type=FeedbackTarget.FINDING.value,
            target_id=finding.id,
            action=action,
            before=before,
            after={"status": finding.status, "severity": finding.severity, "assignee": finding.assignee},
            note=payload.note,
        )
    )
    db.commit()
    db.refresh(finding)
    _emit_finding_event(db, finding, "finding.updated")
    return finding


def _emit_finding_event(db: Session, finding: Finding, event: str) -> None:
    """状态变更后推送项目 webhook；失败不影响主流程。"""
    try:
        from app.push import queue_event

        project = db.get(Project, finding.project_id)
        if project is None:
            return
        data = {
            "finding_id": finding.id,
            "status": finding.status,
            "fix_status": finding.fix_status,
            "severity": finding.severity,
            "title": finding.title,
            "assignee": finding.assignee or "",
        }
        if queue_event(db, project, event, data) is not None:
            db.commit()
    except Exception:  # noqa: BLE001 - 推送是旁路，不阻塞主流程
        db.rollback()


@router.get("/projects/{project_id}/findings/stats")
def finding_stats(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    findings = db.scalars(select(Finding).where(Finding.project_id == project_id)).all()
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_fix_status: dict[str, int] = {}
    for finding in findings:
        by_source[finding.source] = by_source.get(finding.source, 0) + 1
        by_status[finding.status] = by_status.get(finding.status, 0) + 1
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_fix_status[finding.fix_status or "open"] = by_fix_status.get(finding.fix_status or "open", 0) + 1
    confirmed = by_status.get(FindingStatus.CONFIRMED.value, 0)
    reviewed = confirmed + by_status.get(FindingStatus.FALSE_POSITIVE.value, 0)
    return {
        "total": len(findings),
        "by_source": by_source,
        "by_status": by_status,
        "by_severity": by_severity,
        "by_fix_status": by_fix_status,
        "confirm_rate": round(confirmed / reviewed, 4) if reviewed else None,
    }
