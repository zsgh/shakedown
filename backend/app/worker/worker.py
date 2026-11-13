"""后台 worker：轮询数据库任务队列，执行状态机。"""
from __future__ import annotations

import threading

from app.config import get_settings
from app.db import session_scope
from app.logging import get_logger
from app.worker import handlers
from app.worker.queue import claim_next, complete, fail, requeue_stale

logger = get_logger("shakedown.worker")


class Worker:
    """一个 worker 线程：取任务 → 执行 → 回写状态。"""

    def __init__(self, name: str = "worker-0", stop_event: threading.Event | None = None):
        self.name = name
        self.stop_event = stop_event or threading.Event()
        self._thread: threading.Thread | None = None

    # ── 任务执行 ─────────────────────────────────────────
    def run_once(self) -> bool:
        """处理一个任务；返回是否处理了任务。"""
        with session_scope() as session:
            task = claim_next(session)
            if task is None:
                return False
            task_id = task.id
            kind = task.kind
            payload = dict(task.payload or {})
            payload.setdefault("_task_id", task_id)

        try:
            handler = handlers.HANDLERS.get(kind)
            if handler is None:
                raise ValueError(f"未知任务类型：{kind}")
            with session_scope() as session:
                result = handler(session, payload)
            with session_scope() as session:
                # 锁住任务行，避免“检查取消”和“写成功”之间被并发取消
                task = session.get(type(task), task_id, with_for_update=True)
                if task is not None:
                    if task.status == "cancelled":
                        logger.info("任务 %s(%s) 已取消，结果不再覆盖状态", task_id, kind)
                    else:
                        complete(session, task, result)
            logger.info("任务 %s(%s) 完成：%s", task_id, kind, result)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s(%s) 失败", task_id, kind)
            with session_scope() as session:
                task = session.get(type(task), task_id, with_for_update=True)
                if task is not None:
                    if task.status == "cancelled":
                        logger.info("任务 %s(%s) 已取消，失败状态不再覆盖取消状态", task_id, kind)
                    else:
                        fail(session, task, f"{type(exc).__name__}: {exc}")
            return True

    # ── 线程循环 ─────────────────────────────────────────
    def _loop(self) -> None:
        """轮询循环。

        数据库不可达时做**指数退避**：首次记录完整 traceback，
        之后只记一行警告并逐步拉长间隔（避免 DB 故障期间刷爆日志），
        恢复后自动回到正常轮询间隔并记一行恢复日志。
        """
        settings = get_settings()
        failures = 0
        while not self.stop_event.is_set():
            delay = settings.worker_poll_interval
            try:
                worked = self.run_once()
                if failures:
                    logger.info("worker %s 已恢复正常（此前连续失败 %d 次）", self.name, failures)
                    failures = 0
            except Exception:  # noqa: BLE001
                failures += 1
                delay = min(settings.worker_poll_interval * (2 ** min(failures - 1, 6)), 60.0)
                if failures == 1:
                    logger.exception("worker 循环异常（进入退避，%.0fs 后重试）", delay)
                else:
                    logger.warning(
                        "worker 循环仍异常（连续第 %d 次），%.0fs 后重试", failures, delay
                    )
                worked = False
            if not worked and self.stop_event.wait(delay):
                break

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        logger.info("worker %s 已启动", self.name)

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)


class WorkerPool:
    def __init__(self, concurrency: int | None = None):
        settings = get_settings()
        self.concurrency = concurrency or settings.worker_concurrency
        self.stop_event = threading.Event()
        self.workers: list[Worker] = []

    def start(self) -> None:
        self.stop_event.clear()
        requeue_stale_with_session()
        for index in range(self.concurrency):
            worker = Worker(f"worker-{index}", self.stop_event)
            worker.start()
            self.workers.append(worker)

    def stop(self) -> None:
        self.stop_event.set()
        for worker in self.workers:
            worker.stop()
        self.workers.clear()


def requeue_stale_with_session() -> int:
    with session_scope() as session:
        return requeue_stale(session)


_pool: WorkerPool | None = None
_scheduler = None


def get_pool() -> WorkerPool:
    global _pool
    if _pool is None:
        _pool = WorkerPool()
    return _pool


def start_worker() -> None:
    global _scheduler
    settings = get_settings()
    if not settings.worker_enabled:
        logger.info("worker 未启用（SHAKEDOWN_WORKER_ENABLED=false）")
        return
    get_pool().start()
    if settings.scheduler_enabled and _scheduler is None:
        from app.worker.scheduler import Scheduler

        _scheduler = Scheduler()
        _scheduler.start()


def stop_worker() -> None:
    global _pool, _scheduler
    if _scheduler:
        _scheduler.stop()
        _scheduler = None
    if _pool:
        _pool.stop()
        _pool = None
