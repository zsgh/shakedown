"""轻量指标与请求追踪。

HTTP 计数使用「进程内增量 + Postgres 聚合」：
- 每个进程只在自己的内存里累加，避免每请求写库；
- 达到阈值 / 间隔 / 抓取时批量 upsert 到 ``metric_counters``；
- ``/metrics`` 读取 DB 聚合值，因此多 worker / 多实例共享同一份计数；
- DB 不可用时退回进程内计数，不影响主流程。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextvars import ContextVar
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AutomationException, CaseResult, MetricCounter, Run, Task, TestCase

_current_trace_id: ContextVar[str] = ContextVar("shakedown_trace_id", default="")
_lock = threading.Lock()
_flush_lock = threading.Lock()
_requests: Counter[tuple[str, str, str]] = Counter()
_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
_last_flush = time.monotonic()
_MAX_DURATION_SAMPLES = 2000
_NUMERIC = re.compile(r"/\d+(?=/|$)")

COUNTER_REQUESTS = "shakedown_http_requests_total"
COUNTER_DURATION_COUNT = "shakedown_http_request_duration_seconds_count"
COUNTER_DURATION_SUM = "shakedown_http_request_duration_seconds_sum"
HTTP_COUNTERS = (COUNTER_REQUESTS, COUNTER_DURATION_COUNT, COUNTER_DURATION_SUM)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _current_trace_id.set(trace_id)


def current_trace_id() -> str:
    return _current_trace_id.get() or ""


def normalize_path(path: str) -> str:
    return _NUMERIC.sub("/{id}", path or "/")


def _db_enabled() -> bool:
    return bool(get_settings().metrics_db_enabled)


def _labels_key(labels: dict[str, Any]) -> str:
    return json.dumps(labels, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _local_deltas() -> tuple[dict[tuple[str, str], float], Counter[tuple[str, str, str]], dict[tuple[str, str], list[float]]]:
    """取出并清空本地增量。调用方必须持有 ``_lock``。"""
    global _last_flush
    request_delta = Counter(_requests)
    duration_snapshot = {key: list(values) for key, values in _durations.items()}
    _requests.clear()
    _durations.clear()
    _last_flush = time.monotonic()
    deltas: dict[tuple[str, str], float] = {}
    for (method, path, status), count in request_delta.items():
        labels = {"method": method, "path": path, "status": status}
        key = (COUNTER_REQUESTS, _labels_key(labels))
        deltas[key] = deltas.get(key, 0.0) + count
    for (method, path), samples in duration_snapshot.items():
        labels = {"method": method, "path": path}
        key = (COUNTER_DURATION_COUNT, _labels_key(labels))
        deltas[key] = deltas.get(key, 0.0) + len(samples)
        key = (COUNTER_DURATION_SUM, _labels_key(labels))
        deltas[key] = deltas.get(key, 0.0) + round(sum(samples), 6)
    return deltas, request_delta, duration_snapshot


def _restore_local(
    request_delta: Counter[tuple[str, str, str]],
    duration_snapshot: dict[tuple[str, str], list[float]],
) -> None:
    with _lock:
        _requests.update(request_delta)
        for key, samples in duration_snapshot.items():
            current = _durations[key]
            current.extend(samples)
            if len(current) > _MAX_DURATION_SAMPLES:
                del current[: len(current) - _MAX_DURATION_SAMPLES]


def flush_metrics(force: bool = False) -> int:
    """把本地增量写入共享计数器；返回写入的指标条数。"""
    if not _db_enabled():
        return 0
    settings = get_settings()
    now = time.monotonic()
    with _lock:
        pending = sum(_requests.values())
        due = force or pending >= max(1, settings.metrics_flush_after_requests) or (
            pending > 0 and now - _last_flush >= max(0.1, settings.metrics_flush_interval_seconds)
        )
        if not due or (not _requests and not _durations):
            return 0
        deltas, request_delta, duration_snapshot = _local_deltas()
    if not deltas:
        return 0
    try:
        with _flush_lock, SessionLocal() as session:
            for (name, labels_key), value in deltas.items():
                labels = json.loads(labels_key)
                stmt = pg_insert(MetricCounter).values(
                    name=name,
                    labels=labels,
                    labels_key=labels_key,
                    value=value,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["name", "labels_key"],
                    set_={"value": MetricCounter.value + stmt.excluded.value},
                )
                session.execute(stmt)
            session.commit()
    except Exception:  # noqa: BLE001 —— 指标失败不能影响业务请求
        _restore_local(request_delta, duration_snapshot)
        return 0
    return len(deltas)


def record_request(method: str, path: str, status: int, duration: float) -> None:
    key = (method.upper(), normalize_path(path), str(status))
    with _lock:
        _requests[key] += 1
        samples = _durations[(method.upper(), key[1])]
        samples.append(duration)
        if len(samples) > _MAX_DURATION_SAMPLES:
            del samples[: len(samples) - _MAX_DURATION_SAMPLES]
    try:
        flush_metrics()
    except Exception:  # noqa: BLE001
        pass


def reset_local_metrics() -> None:
    """测试辅助：清空当前进程增量。"""
    global _last_flush
    with _lock:
        _requests.clear()
        _durations.clear()
        _last_flush = 0.0


def _prom_line(name: str, labels: dict[str, Any], value: float | int) -> str:
    if labels:
        body = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in labels.items())
        return f"{name}{{{body}}} {value}"
    return f"{name} {value}"


def _collected_counters(db: Session | None) -> dict[tuple[str, str, str], float]:
    """返回 {(name, labels_key, labels_json): value}，合并 DB 与未 flush 的本地增量。"""
    collected: dict[tuple[str, str, str], float] = {}
    if _db_enabled():
        try:
            if db is not None:
                rows = db.execute(
                    select(MetricCounter.name, MetricCounter.labels_key, MetricCounter.value).where(
                        MetricCounter.name.in_(HTTP_COUNTERS)
                    )
                ).all()
            else:
                with SessionLocal() as session:
                    rows = session.execute(
                        select(MetricCounter.name, MetricCounter.labels_key, MetricCounter.value).where(
                            MetricCounter.name.in_(HTTP_COUNTERS)
                        )
                    ).all()
            for name, labels_key, value in rows:
                collected[(name, labels_key, labels_key)] = float(value)
        except Exception:  # noqa: BLE001
            pass
    # 本地未 flush 的增量（DB 关闭 / flush 失败时也要可见）
    with _lock:
        for (method, path, status), count in _requests.items():
            labels = {"method": method, "path": path, "status": status}
            key = (COUNTER_REQUESTS, _labels_key(labels), _labels_key(labels))
            collected[key] = collected.get(key, 0.0) + count
        for (method, path), samples in _durations.items():
            labels = {"method": method, "path": path}
            labels_key = _labels_key(labels)
            key = (COUNTER_DURATION_COUNT, labels_key, labels_key)
            collected[key] = collected.get(key, 0.0) + len(samples)
            key = (COUNTER_DURATION_SUM, labels_key, labels_key)
            collected[key] = collected.get(key, 0.0) + round(sum(samples), 6)
    return collected


def render_metrics(db: Session | None = None) -> str:
    flush_metrics(force=True)
    lines: list[str] = []
    counters = _collected_counters(db)
    lines.append("# HELP shakedown_http_requests_total HTTP requests by method/path/status")
    lines.append("# TYPE shakedown_http_requests_total counter")
    for (name, labels_key, _), value in sorted(counters.items()):
        if name != COUNTER_REQUESTS:
            continue
        labels = json.loads(labels_key)
        lines.append(_prom_line(COUNTER_REQUESTS, labels, int(value)))
    lines.append("# HELP shakedown_http_request_duration_seconds Request duration")
    lines.append("# TYPE shakedown_http_request_duration_seconds summary")
    for (name, labels_key, _), value in sorted(counters.items()):
        if name not in {COUNTER_DURATION_COUNT, COUNTER_DURATION_SUM}:
            continue
        labels = json.loads(labels_key)
        lines.append(_prom_line(name, labels, int(value) if name.endswith("_count") else round(value, 6)))

    if db is not None:
        lines.append("# HELP shakedown_tasks_total Background tasks by status")
        lines.append("# TYPE shakedown_tasks_total gauge")
        for status, count in db.execute(select(Task.status, func.count()).group_by(Task.status)).all():
            lines.append(_prom_line("shakedown_tasks_total", {"status": status}, count))
        lines.append("# HELP shakedown_runs_total Runs by status")
        lines.append("# TYPE shakedown_runs_total gauge")
        for status, count in db.execute(select(Run.status, func.count()).group_by(Run.status)).all():
            lines.append(_prom_line("shakedown_runs_total", {"status": status}, count))
        lines.append("# HELP shakedown_automation_exceptions_open Open automation exceptions")
        lines.append("# TYPE shakedown_automation_exceptions_open gauge")
        open_count = db.scalar(
            select(func.count()).where(AutomationException.status.in_(["open", "acked"]))
        ) or 0
        lines.append(_prom_line("shakedown_automation_exceptions_open", {}, open_count))
        lines.append("# HELP shakedown_case_results_total Case results by status")
        lines.append("# TYPE shakedown_case_results_total gauge")
        for status, count in db.execute(select(CaseResult.status, func.count()).group_by(CaseResult.status)).all():
            lines.append(_prom_line("shakedown_case_results_total", {"status": status}, count))
        lines.append("# HELP shakedown_cases_total Cases by status")
        lines.append("# TYPE shakedown_cases_total gauge")
        for status, count in db.execute(select(TestCase.status, func.count()).group_by(TestCase.status)).all():
            lines.append(_prom_line("shakedown_cases_total", {"status": status}, count))
    lines.append("# HELP shakedown_metrics_db_aggregated Whether HTTP counters are DB-aggregated")
    lines.append("# TYPE shakedown_metrics_db_aggregated gauge")
    lines.append(_prom_line("shakedown_metrics_db_aggregated", {}, 1 if _db_enabled() else 0))
    return "\n".join(lines) + "\n"


def timed() -> float:
    return time.perf_counter()
