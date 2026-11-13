"""无人值守调度器：扫描到期的项目 schedule，创建 run 或记录环境失败。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.environment_check import check_target
from app.logging import get_logger
from app.models import Project, ProjectSchedule, Run
from app.run_service import create_run_for_project

logger = get_logger("shakedown.worker.scheduler")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Scheduler:
    def __init__(self, stop_event: threading.Event | None = None):
        self.stop_event = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._next_sla_escalation_at = time.monotonic() + get_settings().sla_escalation_interval_seconds
        self._next_digest_at = time.monotonic() + get_settings().digest_sweep_interval_seconds
        self._next_repair_at = time.monotonic() + get_settings().repair_sweep_interval_seconds

    def run_once(self) -> int:
        now = _now()
        due_ids: list[int] = []
        with session_scope() as session:
            schedules = session.scalars(
                select(ProjectSchedule)
                .where(
                    ProjectSchedule.enabled.is_(True),
                    ProjectSchedule.next_run_at.is_not(None),
                    ProjectSchedule.next_run_at <= now,
                )
                .order_by(ProjectSchedule.next_run_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            ).all()
            for schedule in schedules:
                schedule.next_run_at = now + timedelta(seconds=max(60, int(schedule.interval_seconds or 86400)))
                schedule.last_run_at = now
                due_ids.append(schedule.id)

        for schedule_id in due_ids:
            self._execute_schedule(schedule_id)
        return len(due_ids)

    def _execute_schedule(self, schedule_id: int) -> None:
        with session_scope() as session:
            schedule = session.get(ProjectSchedule, schedule_id)
            if schedule is None or not schedule.enabled:
                return
            project = session.get(Project, schedule.project_id)
            if project is None:
                schedule.last_status = "failed"
                schedule.last_error = "项目不存在"
                return

            from app.automation import check_run_quota

            quota = check_run_quota(session, project)
            if not quota.get("ok"):
                schedule.last_status = "quota_exceeded"
                schedule.last_error = quota["reason"]
                return
            readiness = check_target(project)
            if not readiness.get("ok"):
                target = project.target_start if isinstance(project.target_start, dict) else {}
                if target.get("auto_start"):
                    from app.environment_manager import start_environment

                    started = start_environment(project)
                    readiness = started.get("readiness") or readiness
            if not readiness.get("ok"):
                reason = str(readiness.get("reason") or "环境未就绪")
                run = Run(
                    project_id=project.id,
                    trigger="schedule",
                    status="failed",
                    case_ids=[],
                    env_snapshot={"base_url": project.base_url, "readiness": readiness},
                    stats={"total": 0, "environment": readiness, "schedule_id": schedule.id},
                    error=f"环境未就绪：{reason}",
                )
                session.add(run)
                session.flush()
                schedule.last_run_id = run.id
                schedule.last_status = "environment_unavailable"
                schedule.last_error = reason
                try:
                    from app.exceptions import record_exception

                    record_exception(
                        session,
                        project,
                        kind="environment_unavailable",
                        target_type="run",
                        target_id=run.id,
                        severity="high",
                        note=f"环境未就绪：{reason}",
                        payload=readiness,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("环境例外入队失败：project=%s", project.id)
                try:
                    from app.push import queue_event

                    queue_event(session, project, "environment_unavailable", readiness)
                except Exception:  # noqa: BLE001 —— 通知失败不影响调度
                    logger.warning("环境失败通知入队失败：project=%s", project.id)
                return

            policy = schedule.policy or {}
            if str(policy.get("mode") or "") == "performance":
                targets = policy.get("targets") if isinstance(policy.get("targets"), list) else []
                if not targets:
                    schedule.last_status = "skipped"
                    schedule.last_error = "performance 调度没有配置 targets"
                    return
                from app.worker.queue import enqueue

                task = enqueue(
                    session,
                    "performance_run",
                    {"project_id": project.id, "targets": targets},
                    project_id=project.id,
                )
                schedule.last_status = "queued"
                schedule.last_error = ""
                schedule.last_run_id = None
                logger.info("性能调度入队：project=%s task=%s", project.id, task.id)
                return
            run = create_run_for_project(
                session,
                project,
                trigger="schedule",
                policy=policy,
                skip_empty=True,
            )
            if run is None:
                schedule.last_status = "skipped"
                schedule.last_error = "没有可执行用例"
                return
            schedule.last_run_id = run.id
            schedule.last_status = run.status
            schedule.last_error = ""

    def _run_sla_escalations(self) -> int:
        """按全局间隔扫描开启了 SLA 升级的项目。"""
        from app.escalation import run_sla_escalation, sla_escalation_settings

        created = 0
        with session_scope() as session:
            projects = list(session.scalars(select(Project)).all())
            for project in projects:
                if not sla_escalation_settings(project)["enabled"]:
                    continue
                try:
                    result = run_sla_escalation(session, project, dry_run=False)
                    created += int(result.get("created") or 0)
                except Exception:  # noqa: BLE001 —— 单个项目失败不影响其它项目
                    logger.exception("SLA 升级失败：project=%s", project.id)
            session.commit()
        if created:
            logger.info("SLA 定时升级：新建 %s 个例外", created)
        return created

    def _run_digests(self) -> int:
        """扫描开启了 digest 的项目，按各自间隔投递日报 / 周报。"""
        from app.digest import queue_scheduled_digest

        queued = 0
        with session_scope() as session:
            projects = list(session.scalars(select(Project)).all())
            for project in projects:
                try:
                    result = queue_scheduled_digest(session, project)
                    if result.get("queued"):
                        queued += 1
                except Exception:  # noqa: BLE001
                    logger.exception("digest 投递失败：project=%s", project.id)
            session.commit()
        if queued:
            logger.info("定时 digest：入队 %s 个项目", queued)
        return queued

    def _run_repair_sweeps(self) -> int:
        """对开启了 auto_repair_failed 的项目，自动修复带失败记录的 needs_review 用例。"""
        from app.models import CaseResult, TestCase
        from app.worker.queue import enqueue, has_active_task

        settings = get_settings()
        queued = 0
        with session_scope() as session:
            projects = list(session.scalars(select(Project)).all())
            for project in projects:
                automation = (project.settings_json or {}).get("automation")
                automation = automation if isinstance(automation, dict) else {}
                if not automation.get("auto_repair_failed"):
                    continue
                if has_active_task(session, project_id=project.id, kind="enrich_case"):
                    continue
                failure_cases = session.scalars(
                    select(TestCase)
                    .join(CaseResult, CaseResult.case_id == TestCase.id)
                    .where(
                        TestCase.project_id == project.id,
                        TestCase.status == "needs_review",
                        CaseResult.status.in_(["fail", "error", "flaky"]),
                        CaseResult.rerun.is_(False),
                    )
                    .distinct()
                    .order_by(TestCase.priority, TestCase.id)
                    .limit(max(1, settings.repair_sweep_limit))
                ).all()
                for case in failure_cases:
                    cli_mode = any(
                        isinstance(step, dict) and step.get("action") == "command"
                        for step in (case.steps or [])
                    )
                    enqueue(
                        session,
                        "enrich_case",
                        {
                            "project_id": project.id,
                            "case_id": case.id,
                            "repair": True,
                            "cli_mode": cli_mode,
                        },
                        project_id=project.id,
                    )
                    queued += 1
                    if queued >= max(1, settings.repair_sweep_limit):
                        break
                session.commit()
        return queued

    def _loop(self) -> None:
        settings = get_settings()
        while not self.stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:  # noqa: BLE001
                logger.exception("调度器循环异常")
                processed = 0
            if settings.sla_escalation_enabled and time.monotonic() >= self._next_sla_escalation_at:
                try:
                    self._run_sla_escalations()
                except Exception:  # noqa: BLE001
                    logger.exception("SLA 定时升级循环异常")
                finally:
                    self._next_sla_escalation_at = (
                        time.monotonic() + max(60.0, settings.sla_escalation_interval_seconds)
                    )
            if settings.digest_sweep_enabled and time.monotonic() >= self._next_digest_at:
                try:
                    self._run_digests()
                except Exception:  # noqa: BLE001
                    logger.exception("定时 digest 循环异常")
                finally:
                    self._next_digest_at = (
                        time.monotonic() + max(60.0, settings.digest_sweep_interval_seconds)
                    )
            if settings.repair_sweep_enabled and time.monotonic() >= self._next_repair_at:
                try:
                    repaired = self._run_repair_sweeps()
                    if repaired:
                        logger.info("自动修复扫描：入队 %s 条", repaired)
                except Exception:  # noqa: BLE001
                    logger.exception("自动修复扫描异常")
                finally:
                    self._next_repair_at = time.monotonic() + max(
                        60.0, settings.repair_sweep_interval_seconds
                    )
            delay = settings.scheduler_poll_interval if not processed else 1.0
            if self.stop_event.wait(delay):
                break

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        logger.info("调度器已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
