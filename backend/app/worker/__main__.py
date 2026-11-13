"""独立 worker 进程入口：``python -m app.worker``。"""
from __future__ import annotations

import signal
import threading

from app.config import get_settings
from app.logging import get_logger
from app.worker.worker import start_worker, stop_worker

logger = get_logger("shakedown.worker.main")


def main() -> None:
    settings = get_settings()
    if not settings.worker_enabled:
        logger.error("SHAKEDOWN_WORKER_ENABLED=false，独立 worker 不会启动")
        return
    stop = threading.Event()

    def _handle(signum, _frame):
        logger.info("worker 收到信号 %s，准备退出", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    start_worker()
    logger.info("独立 worker 已启动（scheduler=%s）", settings.scheduler_enabled)
    stop.wait()
    stop_worker()
    logger.info("独立 worker 已退出")


if __name__ == "__main__":
    main()
