"""worker 循环在数据库故障期间的退避与恢复行为。"""
from __future__ import annotations

from app.config import get_settings
from app.worker.worker import Worker


class RecordingEvent:
    """记录 wait 间隔的 stop_event 替身；达到轮数上限即停止。"""

    def __init__(self, max_waits: int):
        self.waits: list[float] = []
        self.max_waits = max_waits
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def clear(self) -> None:
        self._stopped = False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.max_waits:
            self._stopped = True
        return self._stopped


def test_loop_backs_off_exponentially_on_outage():
    """DB 持续不可达时，重试间隔按 2 的幂增长并封顶，避免刷爆日志。"""
    interval = get_settings().worker_poll_interval
    event = RecordingEvent(max_waits=5)
    worker = Worker(name="worker-test", stop_event=event)

    def failing() -> bool:
        raise RuntimeError("connection refused")

    worker.run_once = failing  # type: ignore[method-assign]
    worker._loop()

    expected = [min(interval * (2**i), 60.0) for i in range(5)]
    assert event.waits == expected


def test_loop_recovers_after_outage():
    """故障恢复后，间隔回落到正常轮询值（不再继续退避）。"""
    interval = get_settings().worker_poll_interval
    event = RecordingEvent(max_waits=4)
    worker = Worker(name="worker-test", stop_event=event)

    calls = {"n": 0}

    def flaky() -> bool:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("connection refused")
        return False  # 恢复后：DB 可达，只是队列空

    worker.run_once = flaky  # type: ignore[method-assign]
    worker._loop()

    # 前两轮退避 interval、2*interval；恢复后回到 interval
    assert event.waits == [interval, interval * 2, interval, interval]
