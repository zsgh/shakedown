"""看板 / 报告 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError
from app.models import CaseResult, Finding, Project, Run, TestCase
from app.reporting.html_export import render_project_report, render_run_report

router = APIRouter(tags=["reports"])


def build_overview(db: Session, project: Project) -> dict[str, Any]:
    """项目总览：覆盖率、各维度用例数与通过率、最近执行趋势。"""
    from app.api.cases import coverage_tree

    coverage = coverage_tree(project.id, db)

    cases = db.scalars(select(TestCase).where(TestCase.project_id == project.id)).all()
    dimensions: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = dimensions.setdefault(case.dimension, {"total": 0, "enabled": 0, "pending": 0, "disabled": 0})
        bucket["total"] += 1
        bucket["enabled"] += int(case.status == "enabled")
        bucket["pending"] += int(case.status in {"pending", "needs_review"})
        bucket["disabled"] += int(case.status == "disabled")

    runs = db.scalars(select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(20)).all()
    run_items: list[dict[str, Any]] = []
    for run in runs:
        stats = run.stats or {}
        run_items.append(
            {
                "id": run.id,
                "status": run.status,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "total": stats.get("total", 0),
                "passed": stats.get("passed", 0),
                "failed": stats.get("failed", 0),
                "flaky": stats.get("flaky", 0),
                "pass_rate": stats.get("pass_rate", 0),
            }
        )

    # 各维度通过率（基于最近一次执行）
    latest_run = runs[0] if runs else None
    if latest_run:
        results = db.scalars(
            select(CaseResult).where(CaseResult.run_id == latest_run.id, CaseResult.rerun.is_(False))
        ).all()
        case_map = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_([r.case_id for r in results]))).all()}
        for result in results:
            case = case_map.get(result.case_id)
            if not case:
                continue
            bucket = dimensions.setdefault(case.dimension, {"total": 0, "enabled": 0, "pending": 0, "disabled": 0, "passed": 0, "failed": 0})
            bucket["passed"] = bucket.get("passed", 0) + int(result.status == "pass")
            bucket["failed"] = bucket.get("failed", 0) + int(result.status in {"fail", "error"})

    findings = db.scalars(select(Finding).where(Finding.project_id == project.id)).all()
    finding_stats: dict[str, Any] = {"total": len(findings)}
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for finding in findings:
        by_source[finding.source] = by_source.get(finding.source, 0) + 1
        by_status[finding.status] = by_status.get(finding.status, 0) + 1
    finding_stats["by_source"] = by_source
    finding_stats["by_status"] = by_status

    inconsistent = 0
    from app.models import Endpoint

    for endpoint in db.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all():
        if (endpoint.consistency or {}).get("state") in {"inconsistent", "conflict"}:
            inconsistent += 1

    # 回测指标（有效性验证）：trigger=backtest 的 run 里带 backtest 明细
    backtest_runs = db.scalars(
        select(Run).where(Run.project_id == project.id, Run.trigger == "backtest").order_by(Run.created_at.desc())
    ).all()
    backtest_items = [run.backtest for run in backtest_runs if run.backtest]
    caught = sum(1 for item in backtest_items if item.get("caught"))
    false_positives = sum(
        max(0, int(item.get("failed_cases") or 0) - len(item.get("matched_cases") or [])) for item in backtest_items
    )
    backtest = {
        "bugs": len(backtest_items),
        "caught": caught,
        "hit_rate": round(caught / len(backtest_items), 4) if backtest_items else None,
        "false_positives": false_positives,
        "items": backtest_items[:50],
    }

    # 行动中心：按优先级告诉用户「下一步该做什么」
    from app.models import KnowledgeSource

    pending_review = sum(1 for case in cases if case.status in {"pending", "needs_review"})
    enabled_cases = sum(1 for case in cases if case.status == "enabled")
    pending_findings = finding_stats["by_status"].get("pending", 0)
    sources = db.scalars(select(KnowledgeSource).where(KnowledgeSource.project_id == project.id)).all()
    failed_sources = sum(1 for source in sources if source.status == "failed")
    latest_stats = (runs[0].stats or {}) if runs else {}
    last_failed = int(latest_stats.get("failed") or 0) + int(latest_stats.get("errored") or 0)

    attention: list[dict[str, Any]] = []
    if not sources:
        attention.append({
            "kind": "sources",
            "level": "info",
            "title": "还没有知识源",
            "detail": "导入代码库、Markdown 文档或 OpenAPI，Agent 才有依据生成用例。",
            "count": 0,
            "action": "去导入",
            "path": "knowledge/sources",
        })
    if failed_sources:
        attention.append({
            "kind": "sources",
            "level": "warning",
            "title": f"{failed_sources} 个知识源解析失败",
            "detail": "解析失败的知识源不会参与功能点与用例生成。",
            "count": failed_sources,
            "action": "去处理",
            "path": "knowledge/sources",
        })
    if pending_review:
        attention.append({
            "kind": "review",
            "level": "warning",
            "title": f"{pending_review} 条用例待审核",
            "detail": "Agent 生成的用例默认不执行，审核启用后才会进入执行集。",
            "count": pending_review,
            "action": "去审核",
            "path": "cases",
        })
    if runs and last_failed:
        attention.append({
            "kind": "run",
            "level": "danger",
            "title": f"最近一次执行有 {last_failed} 条失败 / 错误",
            "detail": "先确认归因，再决定是修产品、改用例还是重跑。",
            "count": last_failed,
            "action": "看失败",
            "path": f"runs/{runs[0].id}?status=fail",
        })
    if pending_findings:
        attention.append({
            "kind": "findings",
            "level": "danger",
            "title": f"{pending_findings} 条缺陷待分诊",
            "detail": "确认真实缺陷或标记误报，结果会回流给 Agent。",
            "count": pending_findings,
            "action": "去分诊",
            "path": "findings",
        })
    if inconsistent:
        attention.append({
            "kind": "consistency",
            "level": "info",
            "title": f"{inconsistent} 个接口三方不一致",
            "detail": "OpenAPI × 代码 × 文档对不上，本身就是缺陷线索。",
            "count": inconsistent,
            "action": "看差异",
            "path": "knowledge/consistency",
        })
    if not enabled_cases:
        attention.append({
            "kind": "run",
            "level": "info",
            "title": "还没有启用的用例",
            "detail": "审核并启用至少一条用例，才能开始执行。",
            "count": 0,
            "action": "去用例",
            "path": "cases",
        })
    if not runs:
        attention.append({
            "kind": "run",
            "level": "info",
            "title": "还没有执行记录",
            "detail": "启用用例后执行一次，拿到第一份测试结果。",
            "count": 0,
            "action": "去执行",
            "path": "runs",
        })
    elif runs[0].status in {"queued", "preparing", "running", "analyzing"}:
        attention.insert(0, {
            "kind": "run",
            "level": "info",
            "title": f"执行 #{runs[0].id} 正在进行",
            "detail": f"当前状态：{runs[0].status}",
            "count": 0,
            "action": "查看进度",
            "path": f"runs/{runs[0].id}",
        })

    return {
        "project_id": project.id,
        "coverage": coverage,
        "dimensions": dimensions,
        "runs": run_items,
        "findings": finding_stats,
        "inconsistent_endpoints": inconsistent,
        "backtest": backtest,
        "attention": attention,
        "pending_review": pending_review,
        "enabled_cases": enabled_cases,
        "total_cases": len(cases),
        "endpoint_count": db.scalar(select(func.count()).select_from(Endpoint).where(Endpoint.project_id == project.id)) or 0,
        "source_count": len(sources),
        "latest_run_id": runs[0].id if runs else None,
        "last_run_status": runs[0].status if runs else None,
    }


@router.get("/projects/{project_id}/report")
def export_project_report(project_id: int, db: Session = Depends(get_db)) -> Response:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    overview = build_overview(db, project)
    html = render_project_report({"project": {"id": project.id, "name": project.name}, "overview": overview})
    filename = f"shakedown-project-{project.id}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/runs/{run_id}/report")
def export_run_report(run_id: int, db: Session = Depends(get_db)) -> Response:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"执行记录不存在：{run_id}")
    project = db.get(Project, run.project_id)
    results = db.scalars(
        select(CaseResult).where(CaseResult.run_id == run_id).order_by(CaseResult.rerun, CaseResult.seq)
    ).all()
    cases = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_([r.case_id for r in results]))).all()}
    result_ids = [r.id for r in results]
    findings = (
        db.scalars(select(Finding).where(Finding.result_id.in_(result_ids))).all() if result_ids else []
    )
    payload = {
        "project": {"id": project.id, "name": project.name} if project else {},
        "run": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "stats": run.stats,
        },
        "results": [
            {
                "case_id": r.case_id,
                "title": cases[r.case_id].title if r.case_id in cases else "",
                "dimension": cases[r.case_id].dimension if r.case_id in cases else "",
                "status": r.status,
                "request": r.request,
                "response": r.response,
                "logs": r.logs,
                "attribution": r.attribution,
                "rerun": r.rerun,
            }
            for r in results
        ],
        "findings": [
            {
                "title": f.title,
                "source": f.source,
                "severity": f.severity,
                "status": f.status,
                "repro": f.repro,
            }
            for f in findings
        ],
    }
    html = render_run_report(payload)
    filename = f"shakedown-run-{run.id}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
