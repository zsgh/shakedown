"""数据库驱动的任务队列（第一版不引入 Celery）。

特点：
- 任务落库，支持断点恢复（checkpoint 字段）；
- ``FOR UPDATE SKIP LOCKED`` 抢占，支持多 worker 并行；
- 租约（lease_expires_at）+ 心跳，避免 worker 崩溃后任务卡死。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import RunStatus, TaskKind, TaskStatus
from app.logging import get_logger
from app.models import Run, Task

logger = get_logger("shakedown.worker.queue")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mark_run_failed(session: Session, task: Task, error: str) -> None:
    """执行任务最终失败时，把对应的 Run 也置为 failed，避免看板永远卡在 running。"""
    if task.kind != TaskKind.EXECUTE_RUN.value or task.run_id is None:
        return
    run = session.get(Run, task.run_id)
    if run is None or run.status in {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }:
        return
    run.status = RunStatus.FAILED.value
    run.error = error[:4000]
    run.finished_at = _now()


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    project_id: int | None = None,
    run_id: int | None = None,
    priority: int = 100,
    max_attempts: int | None = None,
) -> Task:
    settings = get_settings()
    task = Task(
        project_id=project_id,
        run_id=run_id,
        kind=kind,
        status=TaskStatus.QUEUED.value,
        priority=priority,
        payload=payload or {},
        max_attempts=max_attempts if max_attempts is not None else settings.worker_max_attempts,
    )
    session.add(task)
    session.flush()
    return task


def claim_next(session: Session, *, kinds: list[str] | None = None) -> Task | None:
    """抢占一个待执行任务（跳过已被其它 worker 租约占用的任务）。"""
    settings = get_settings()
    now = _now()
    stmt = (
        select(Task)
        .where(Task.status == TaskStatus.QUEUED.value)
        .where(or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now))
        .order_by(Task.priority, Task.id)
        .limit(1)
    )
    if kinds:
        stmt = stmt.where(Task.kind.in_(kinds))
    task = session.scalar(stmt.with_for_update(skip_locked=True))
    if task is None:
        return None
    task.status = TaskStatus.RUNNING.value
    task.attempts += 1
    task.started_at = task.started_at or now
    task.heartbeat_at = now
    task.lease_expires_at = now + timedelta(seconds=settings.worker_lease_seconds)
    session.flush()
    return task


def heartbeat(session: Session, task: Task) -> None:
    settings = get_settings()
    now = _now()
    session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=settings.worker_lease_seconds))
    )
    session.commit()


def save_checkpoint(session: Session, task: Task, checkpoint: dict[str, Any]) -> None:
    task.checkpoint = {**(task.checkpoint or {}), **checkpoint}
    session.commit()


def has_active_task(session: Session, *, project_id: int, kind: str) -> bool:
    """项目下是否已有排队 / 执行中的同类任务（用于避免自动串联重复入队）。"""
    row = session.scalar(
        select(Task.id)
        .where(
            Task.project_id == int(project_id),
            Task.kind == kind,
            Task.status.in_([TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]),
        )
        .limit(1)
    )
    return row is not None


def complete(session: Session, task: Task, result: dict[str, Any] | None = None) -> None:
    task.status = TaskStatus.SUCCEEDED.value
    task.result = result or {}
    task.error = None
    task.finished_at = _now()
    task.lease_expires_at = None
    session.commit()


def fail(session: Session, task: Task, error: str, *, retry: bool = True) -> None:
    task.error = error[:8000]
    task.finished_at = _now()
    task.lease_expires_at = None
    if retry and task.attempts < task.max_attempts:
        task.status = TaskStatus.QUEUED.value
        task.finished_at = None
    else:
        task.status = TaskStatus.FAILED.value
        _mark_run_failed(session, task, error)
    session.commit()


def requeue_stale(session: Session) -> int:
    """把租约过期的 running 任务放回队列（worker 崩溃恢复）。"""
    now = _now()
    tasks = session.scalars(
        select(Task).where(Task.status == TaskStatus.RUNNING.value, Task.lease_expires_at.is_not(None), Task.lease_expires_at < now)
    ).all()
    for task in tasks:
        if task.attempts < task.max_attempts:
            task.status = TaskStatus.QUEUED.value
            task.error = "worker 租约超时，已重新排队"
        else:
            task.status = TaskStatus.FAILED.value
            task.error = "worker 租约超时，任务最终失败"
            _mark_run_failed(session, task, task.error)
    session.commit()
    return len(tasks)


TASK_KINDS = [kind.value for kind in TaskKind]
