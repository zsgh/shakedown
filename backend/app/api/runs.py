"""执行（run）API。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import CaseStatus, RunStatus, TaskStatus
from app.errors import NotFoundError
from app.models import CaseResult, Finding, Project, Run, Task, TestCase
from app.run_service import create_run_for_project
from app.schemas import AttributionUpdate, CaseResultOut, RunCreate, RunOut
from app.worker.queue import enqueue

router = APIRouter(tags=["runs"])


def _result_out(result: CaseResult, case: TestCase | None) -> CaseResultOut:
    return CaseResultOut(
        id=result.id,
        run_id=result.run_id,
        case_id=result.case_id,
        project_id=result.project_id,
        status=result.status,
        request=result.request or {},
        response=result.response or {},
        duration_ms=result.duration_ms,
        logs=result.logs or "",
        screenshot_path=result.screenshot_path,
        trace_path=result.trace_path,
        attribution=result.attribution or {},
        rerun=result.rerun,
        seq=result.seq,
        output=result.output or {},
        case_title=case.title if case else None,
        dimension=case.dimension if case else None,
        created_at=result.created_at,
    )


@router.post("/projects/{project_id}/runs", response_model=RunOut, status_code=201)
def create_run(project_id: int, payload: RunCreate, db: Session = Depends(get_db)) -> Run:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")

    run = create_run_for_project(
        db,
        project,
        trigger=payload.trigger,
        case_ids=payload.case_ids,
        dimensions=payload.dimensions,
        note=payload.note,
    )
    db.commit()
    db.refresh(run)
    return run


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
def list_runs(
    project_id: int,
    limit: int = Query(default=50, ge=0, le=200),
    db: Session = Depends(get_db),
) -> list[Run]:
    return list(
        db.scalars(select(Run).where(Run.project_id == project_id).order_by(Run.created_at.desc()).limit(limit)).all()
    )


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"执行记录不存在：{run_id}")
    return run


@router.get("/runs/{run_id}/results", response_model=list[CaseResultOut])
def run_results(run_id: int, db: Session = Depends(get_db)) -> list[CaseResultOut]:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"执行记录不存在：{run_id}")
    results = db.scalars(
        select(CaseResult).where(CaseResult.run_id == run_id).order_by(CaseResult.rerun, CaseResult.seq)
    ).all()
    cases = {c.id: c for c in db.scalars(select(TestCase).where(TestCase.id.in_([r.case_id for r in results]))).all()}
    return [_result_out(result, cases.get(result.case_id)) for result in results]


@router.post("/runs/{run_id}/retry-failed", response_model=RunOut, status_code=201)
def retry_failed_cases(run_id: int, db: Session = Depends(get_db)) -> Run:
    """把上一次执行里失败 / 错误的用例重新排一次执行。"""
    from app.enums import CaseResultStatus
    from app.errors import ConflictError

    source = db.get(Run, run_id)
    if source is None:
        raise NotFoundError(f"执行记录不存在：{run_id}")
    project = db.get(Project, source.project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{source.project_id}")

    failed_ids = sorted(
        {
            row.case_id
            for row in db.scalars(
                select(CaseResult).where(
                    CaseResult.run_id == run_id,
                    CaseResult.rerun.is_(False),
                    CaseResult.status.in_(
                        [CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value]
                    ),
                )
            ).all()
        }
    )
    if not failed_ids:
        raise ConflictError("这次执行没有失败用例，不需要重跑")

    cases = db.scalars(
        select(TestCase).where(
            TestCase.id.in_(failed_ids),
            TestCase.project_id == project.id,
            TestCase.status == CaseStatus.ENABLED.value,
            func.jsonb_array_length(TestCase.steps) > 0,
        )
    ).all()
    if not cases:
        raise ConflictError("失败用例都已停用，无法重跑")

    run = Run(
        project_id=project.id,
        trigger="retry",
        status=RunStatus.QUEUED.value,
        case_ids=[c.id for c in cases],
        env_snapshot={"base_url": project.base_url, "retry_of": run_id},
        stats={"total": len(cases)},
    )
    db.add(run)
    db.flush()
    task = enqueue(db, "execute_run", {"run_id": run.id}, project_id=project.id, run_id=run.id)
    run.task_id = task.id
    db.commit()
    db.refresh(run)
    return run


@router.get("/results/{result_id}")
def get_result(result_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """单条执行结果详情（缺陷抽屉里查看关联请求 / 响应 / 日志）。"""
    result = db.get(CaseResult, result_id)
    if result is None:
        raise NotFoundError(f"执行结果不存在：{result_id}")
    case = db.get(TestCase, result.case_id)
    run = db.get(Run, result.run_id)
    data = _result_out(result, case).model_dump()
    data["finding_ids"] = [
        f.id for f in db.scalars(select(Finding).where(Finding.result_id == result.id)).all()
    ]
    data["run"] = {"id": run.id, "status": run.status, "created_at": run.created_at} if run else None
    return data


@router.get("/projects/{project_id}/runs/compare")
def compare_runs(
    project_id: int,
    base: int = Query(description="基线运行 ID"),
    target: int = Query(description="对比运行 ID"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """对比两次运行：新失败、已修复、持续失败、新增/移除用例。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    base_run = db.get(Run, base)
    target_run = db.get(Run, target)
    if base_run is None or target_run is None:
        raise NotFoundError("对比的运行不存在")
    if base_run.project_id != project_id or target_run.project_id != project_id:
        raise NotFoundError("运行不属于该项目")

    def latest_status(run: Run) -> dict[int, CaseResult]:
        rows = db.scalars(
            select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False))
        ).all()
        return {row.case_id: row for row in rows}

    old = latest_status(base_run)
    new = latest_status(target_run)
    case_ids = set(old) | set(new)
    cases = {
        case.id: case
        for case in db.scalars(select(TestCase).where(TestCase.id.in_(case_ids))).all()
    } if case_ids else {}

    def classify(base_status: str | None, target_status: str | None) -> str:
        failing = {"fail", "error"}
        if base_status is None:
            return "new"
        if target_status is None:
            return "removed"
        if base_status in failing and target_status == "pass":
            return "fixed"
        if base_status == "pass" and target_status in failing:
            return "broken"
        if base_status in failing and target_status in failing:
            return "still_failing"
        if base_status == "pass" and target_status == "pass":
            return "still_passing"
        return "changed"

    order = {
        "broken": 0,
        "still_failing": 1,
        "fixed": 2,
        "new": 3,
        "removed": 4,
        "changed": 5,
        "still_passing": 6,
    }
    items: list[dict[str, Any]] = []
    for case_id in case_ids:
        case = cases.get(case_id)
        base_row = old.get(case_id)
        target_row = new.get(case_id)
        change = classify(base_row.status if base_row else None, target_row.status if target_row else None)
        items.append(
            {
                "case_id": case_id,
                "title": case.title if case else f"用例 #{case_id}",
                "dimension": case.dimension if case else "",
                "base_status": base_row.status if base_row else None,
                "target_status": target_row.status if target_row else None,
                "change": change,
                "base_result_id": base_row.id if base_row else None,
                "target_result_id": target_row.id if target_row else None,
            }
        )
    items.sort(key=lambda item: (order.get(item["change"], 9), item["case_id"]))

    summary = {key: 0 for key in order}
    for item in items:
        summary[item["change"]] = summary.get(item["change"], 0) + 1

    base_stats = base_run.stats or {}
    target_stats = target_run.stats or {}

    def rate(stats: dict[str, Any]) -> float:
        if stats.get("pass_rate") is not None:
            return float(stats["pass_rate"])
        total = int(stats.get("total") or 0)
        return float(stats.get("passed") or 0) / total if total else 0.0

    def brief(run: Run) -> dict[str, Any]:
        stats = run.stats or {}
        return {
            "id": run.id,
            "status": run.status,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "total": stats.get("total", 0),
            "passed": stats.get("passed", 0),
            "failed": stats.get("failed", 0),
            "flaky": stats.get("flaky", 0),
            "errored": stats.get("errored", 0),
            "pass_rate": rate(stats),
        }

    return {
        "project_id": project_id,
        "base": brief(base_run),
        "target": brief(target_run),
        "delta": {
            "pass_rate": round(rate(target_stats) - rate(base_stats), 4),
            "passed": int(target_stats.get("passed") or 0) - int(base_stats.get("passed") or 0),
            "failed": int(target_stats.get("failed") or 0) - int(base_stats.get("failed") or 0),
        },
        "summary": summary,
        "cases": items,
    }


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: int, db: Session = Depends(get_db)) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise NotFoundError(f"执行记录不存在：{run_id}")
    if run.status in {RunStatus.QUEUED.value, RunStatus.PREPARING.value, RunStatus.RUNNING.value}:
        run.status = RunStatus.CANCELLED.value
        run.finished_at = run.finished_at or datetime.now(timezone.utc)
        if run.task_id:
            task = db.get(Task, run.task_id)
            if task and task.status == TaskStatus.QUEUED.value:
                task.status = TaskStatus.CANCELLED.value
    db.commit()
    db.refresh(run)
    return run


@router.post("/results/{result_id}/attribution")
def update_attribution(result_id: int, payload: AttributionUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    """人工修正失败归因（写入 human_feedback，便于后续优化）。"""
    from app.enums import FeedbackAction, FeedbackTarget
    from app.models import HumanFeedback

    result = db.get(CaseResult, result_id)
    if result is None:
        raise NotFoundError(f"执行结果不存在：{result_id}")
    before = dict(result.attribution or {})
    result.attribution = {
        **before,
        "category": payload.category,
        "reason": payload.reason or before.get("reason", ""),
        "manual": True,
    }
    db.add(
        HumanFeedback(
            project_id=result.project_id,
            target_type=FeedbackTarget.ATTRIBUTION.value,
            target_id=result.id,
            action=FeedbackAction.MODIFY.value,
            before=before,
            after=dict(result.attribution),
            note=payload.note,
        )
    )
    # 人工把结果改成用例/环境问题 → 关闭同结果/同用例的未关闭缺陷；
    # 人工确认为缺陷 → 确保有一条可跟踪的 finding，不留断链。
    case = db.get(TestCase, result.case_id) if result.case_id else None
    now = datetime.now(timezone.utc)
    related = db.scalar(
        select(Finding)
        .where(
            Finding.project_id == result.project_id,
            Finding.source == "execution",
            Finding.status.in_(["pending", "confirmed"]),
            (Finding.result_id == result.id) | (Finding.case_id == result.case_id),
        )
        .order_by(Finding.id.desc())
        .limit(1)
    )
    if payload.category in {"case", "environment"} and related is not None:
        related_before = {"status": related.status, "resolution": related.resolution or {}}
        related.status = "false_positive"
        related.resolution = {
            "status": "false_positive",
            "note": payload.note or payload.reason or "人工修正归因判定为非缺陷",
            "actor": "manual:attribution",
            "at": now.isoformat(),
        }
        related.confirmed_by = "auto:attribution"
        related.confirmed_at = now
        related.description = (
            (related.description or "")
            + "\n\n【人工修正归因】该失败被判为 "
            + payload.category
            + " 问题，已关闭对应缺陷。\n"
            + (payload.reason or "")
        )[:4000]
        db.add(
            HumanFeedback(
                project_id=result.project_id,
                target_type=FeedbackTarget.FINDING.value,
                target_id=related.id,
                action=FeedbackAction.FALSE_POSITIVE.value,
                before=related_before,
                after={"status": related.status, "resolution": related.resolution},
                note=payload.note,
            )
        )
    elif payload.category == "defect" and related is None and case is not None:
        from app.executor.runner import build_curl

        related = Finding(
            project_id=result.project_id,
            source="execution",
            title=f"[人工归因] {case.title}"[:400],
            description=payload.reason or payload.note or "人工修正归因判定为缺陷",
            severity="medium",
            evidence=[
                {
                    "type": "manual_attribution",
                    "result_id": result.id,
                    "run_id": result.run_id,
                    "case_id": case.id,
                    "reason": payload.reason or "",
                    "assertions": (result.output or {}).get("assertions", []),
                }
            ],
            result_id=result.id,
            case_id=case.id,
            module="",
            risk_type="manual_attribution",
            repro=build_curl(result.request or {}),
            status="confirmed",
            confirmed_by="manual:attribution",
            confirmed_at=now,
            resolution={
                "status": "confirmed",
                "note": payload.note or payload.reason or "人工修正归因判定为缺陷",
                "actor": "manual:attribution",
                "at": now.isoformat(),
            },
        )
        db.add(related)
        db.flush()
    elif payload.category == "defect" and related is not None and related.status == "pending":
        related.status = "confirmed"
        related.confirmed_by = "manual:attribution"
        related.confirmed_at = now
        related.resolution = {
            "status": "confirmed",
            "note": payload.note or payload.reason or "人工修正归因判定为缺陷",
            "actor": "manual:attribution",
            "at": now.isoformat(),
        }
    db.commit()
    db.refresh(result)
    return {"id": result.id, "attribution": result.attribution}


@router.get("/runs/{run_id}/trace")
def run_trace(run_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """执行过程的 Agent 轨迹（可追溯）。"""
    from app.models import AgentTrace

    traces = db.scalars(select(AgentTrace).where(AgentTrace.run_id == run_id).order_by(AgentTrace.id)).all()
    return [
        {
            "id": t.id,
            "kind": t.kind,
            "step": t.step,
            "model": t.model,
            "input": t.input,
            "output": t.output,
            "tool_calls": t.tool_calls,
            "duration_ms": t.duration_ms,
            "error": t.error,
            "created_at": t.created_at,
        }
        for t in traces
    ]
