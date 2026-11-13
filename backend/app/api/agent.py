"""Agent 相关 API：轨迹查看、规划 / 生成 / 白盒走查触发。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import RunStatus
from app.errors import ConflictError, NotFoundError
from app.models import AgentTrace, Project, Run, Task
from app.schemas import AgentTraceOut, TaskOut
from app.worker.queue import enqueue

router = APIRouter(tags=["agent"])


@router.get("/projects/{project_id}/traces", response_model=list[AgentTraceOut])
def list_traces(
    project_id: int,
    kind: str | None = None,
    run_id: int | None = None,
    task_id: int | None = None,
    limit: int = Query(default=100, ge=0, le=500),
    db: Session = Depends(get_db),
) -> list[AgentTrace]:
    stmt = select(AgentTrace).where(AgentTrace.project_id == project_id)
    if kind:
        stmt = stmt.where(AgentTrace.kind == kind)
    if run_id:
        stmt = stmt.where(AgentTrace.run_id == run_id)
    if task_id:
        stmt = stmt.where(AgentTrace.task_id == task_id)
    return list(db.scalars(stmt.order_by(AgentTrace.id.desc()).limit(limit)).all())


@router.get("/traces/{trace_id}", response_model=AgentTraceOut)
def get_trace(trace_id: int, db: Session = Depends(get_db)) -> AgentTrace:
    trace = db.get(AgentTrace, trace_id)
    if trace is None:
        raise NotFoundError(f"轨迹不存在：{trace_id}")
    return trace


@router.post("/projects/{project_id}/agent/plan-features")
def plan_features(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """触发功能点抽取。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    task = enqueue(db, "plan_features", {"project_id": project_id}, project_id=project_id)
    db.commit()
    return {"task_id": task.id, "status": task.status}


@router.post("/projects/{project_id}/agent/extract-endpoints")
def extract_endpoints(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """让 Agent 读文档（非标准接口文档也认），把接口登记进清单。"""
    from app.agent.agents import extract_doc_endpoints
    from app.agent.providers import get_provider

    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    endpoints = extract_doc_endpoints(db, project, get_provider())
    db.commit()
    return {"endpoints": len(endpoints)}


@router.post("/projects/{project_id}/agent/whitebox")
def whitebox(
    project_id: int,
    resume: bool = Query(default=False, description="继续最近一次被停止 / 失败的白盒走查"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """触发白盒代码走查；resume=true 时复用最近一次未完成任务的 done_modules。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    payload: dict[str, Any] = {"project_id": project_id}
    if resume:
        last = db.scalar(
            select(Task)
            .where(
                Task.project_id == project_id,
                Task.kind == "whitebox_review",
                Task.status.in_(["cancelled", "failed"]),
            )
            .order_by(Task.id.desc())
            .limit(1)
        )
        checkpoint = dict((last.checkpoint or {}).get("whitebox") or {}) if last else {}
        if checkpoint.get("done_modules"):
            payload["resume_checkpoint"] = checkpoint
    task = enqueue(db, "whitebox_review", payload, project_id=project_id)
    db.commit()
    return {"task_id": task.id, "status": task.status, "resumed": bool(payload.get("resume_checkpoint"))}


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(project_id: int, limit: int = Query(default=50, ge=0, le=200), db: Session = Depends(get_db)) -> list[Task]:
    return list(
        db.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id.desc()).limit(limit)).all()
    )


@router.get("/tasks", response_model=list[TaskOut])
def list_all_tasks(
    project_id: int | None = None,
    active: bool = Query(default=False, description="只看排队中 / 运行中的任务"),
    limit: int = Query(default=50, ge=0, le=200),
    db: Session = Depends(get_db),
) -> list[Task]:
    stmt = select(Task)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if active:
        stmt = stmt.where(Task.status.in_(["queued", "running"]))
    return list(db.scalars(stmt.order_by(Task.id.desc()).limit(limit)).all())


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在：{task_id}")
    return task


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task_id: int, db: Session = Depends(get_db)) -> Task:
    """请求停止 queued/running 任务。

    不会删除已经完成的 finding / 用例；worker 在模块边界或批次边界退出。
    已完成 / 已失败的任务不会被改回 cancelled。
    """
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在：{task_id}")
    if task.status in {"queued", "running"}:
        task.status = "cancelled"
        task.lease_expires_at = None
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        task.finished_at = now
        # 从 Agent 活动页停止执行任务时，同步取消 Run，避免 Run 继续跑 / 卡在 running。
        if task.kind == "execute_run" and task.run_id is not None:
            run = db.get(Run, task.run_id)
            if run is not None and run.status in {
                RunStatus.QUEUED.value,
                RunStatus.PREPARING.value,
                RunStatus.RUNNING.value,
                RunStatus.ANALYZING.value,
            }:
                run.status = RunStatus.CANCELLED.value
                run.finished_at = now
        db.commit()
        db.refresh(task)
    return task


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
def retry_task(task_id: int, db: Session = Depends(get_db)) -> Task:
    """重试失败 / 取消的任务。

    execute_run 会创建一条新的 Run（保留原 run.case_ids），其他任务复制原 payload 重新入队。
    """
    task = db.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在：{task_id}")
    if task.status not in {"failed", "cancelled"}:
        raise ConflictError("只有失败或已取消的任务可以重试")

    if task.kind == "execute_run":
        run = db.get(Run, task.run_id) if task.run_id is not None else None
        if run is None or not run.case_ids:
            raise ConflictError("执行任务没有可重试的用例")
        project = db.get(Project, run.project_id)
        new_run = Run(
            project_id=run.project_id,
            trigger="retry",
            status=RunStatus.QUEUED.value,
            case_ids=list(run.case_ids),
            env_snapshot={"base_url": project.base_url if project else "", "retry_of": run.id},
            stats={"total": len(run.case_ids)},
        )
        db.add(new_run)
        db.flush()
        new_task = enqueue(
            db,
            "execute_run",
            {"run_id": new_run.id},
            project_id=run.project_id,
            run_id=new_run.id,
        )
        new_run.task_id = new_task.id
        db.commit()
        db.refresh(new_task)
        return new_task

    new_task = enqueue(
        db,
        task.kind,
        dict(task.payload or {}),
        project_id=task.project_id,
        run_id=task.run_id,
        priority=task.priority,
        max_attempts=task.max_attempts,
    )
    db.commit()
    db.refresh(new_task)
    return new_task
