"""轻量性能执行：baseline / load / stress / soak 的统一入口。"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean
from typing import Any

from app.models import PerformanceRun, Project


def _process_rss_mb() -> float:
    """当前进程 RSS（MB）；优先 /proc，其次 ps，失败返回 0。"""
    status = Path("/proc/self/status")
    if status.exists():
        try:
            for line in status.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 2)
        except Exception:  # noqa: BLE001
            pass
    ps = shutil.which("ps")
    if ps:
        try:
            proc = subprocess.run(
                [ps, "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                timeout=2,
            )
            value = (proc.stdout or "").strip().splitlines()
            if value:
                return round(float(value[0].strip()) / 1024.0, 2)
        except Exception:  # noqa: BLE001
            pass
    return 0.0


def _open_fds() -> int:
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path))
        except Exception:  # noqa: BLE001
            continue
    return 0


def _resource_sample() -> dict[str, Any]:
    return {
        "rss_mb": _process_rss_mb(),
        "open_fds": _open_fds(),
        "threads": threading.active_count(),
    }


def _resource_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {"samples": 0}
    first = samples[0]
    last = samples[-1]
    return {
        "samples": len(samples),
        "first": first,
        "last": last,
        "max_rss_mb": max(float(item.get("rss_mb") or 0) for item in samples),
        "memory_growth_mb": round(float(last.get("rss_mb") or 0) - float(first.get("rss_mb") or 0), 2),
        "fd_growth": int(last.get("open_fds") or 0) - int(first.get("open_fds") or 0),
        "thread_growth": int(last.get("threads") or 0) - int(first.get("threads") or 0),
    }


def evaluate_resources(project: Project, result: dict[str, Any], *, mode: str) -> dict[str, Any]:
    """检查内存 / FD / 线程增长；soak / stress 默认开启，其它模式可显式配置。"""
    settings = project.settings_json if isinstance(project.settings_json, dict) else {}
    raw = settings.get("performance_resource_limits")
    configured = isinstance(raw, dict) and bool(raw)
    limits = {
        "max_memory_growth_mb": 128.0,
        "max_fd_growth": 32,
        "max_thread_growth": 8,
    }
    if isinstance(raw, dict):
        for key in list(limits):
            if key not in raw:
                continue
            try:
                limits[key] = float(raw[key])
            except (TypeError, ValueError):
                continue
    resources = result.get("resources") or {}
    checks: list[dict[str, Any]] = []
    violations: list[str] = []
    if mode not in {"soak", "stress"} and not configured:
        return {"configured": False, "passed": True, "checks": [], "violations": [], "resources": resources}
    if int(resources.get("samples") or 0) < 2:
        return {
            "configured": True,
            "passed": True,
            "checks": [],
            "violations": [],
            "resources": resources,
            "note": "资源样本不足，未判定增长",
        }
    for key, limit in limits.items():
        resource_key = key[len("max_") :] if key.startswith("max_") else key
        actual = float(resources.get(resource_key) or 0)
        passed = actual <= limit
        checks.append({"name": key, "actual": actual, "limit": limit, "operator": "le", "passed": passed})
        if not passed:
            violations.append(f"{key}: {actual} > {limit}")
    return {
        "configured": True,
        "passed": not violations,
        "checks": checks,
        "violations": violations,
        "resources": resources,
    }


def evaluate_slo(project: Project, result: dict[str, Any]) -> dict[str, Any]:
    settings = project.settings_json or {}
    slo = settings.get("performance_slo") if isinstance(settings, dict) else None
    if not isinstance(slo, dict) or not slo:
        return {"configured": False, "passed": True, "checks": [], "violations": []}
    latency = result.get("latency_ms") or {}
    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    def check(name: str, actual: float, limit: float, operator: str) -> None:
        passed = actual <= limit if operator == "le" else actual >= limit
        checks.append(
            {
                "name": name,
                "actual": actual,
                "limit": limit,
                "operator": operator,
                "passed": passed,
            }
        )
        if not passed:
            violations.append(f"{name}: {actual} {'>' if operator == 'le' else '<'} {limit}")

    for key, metric in (("p95_ms", "p95"), ("p99_ms", "p99"), ("avg_ms", "avg")):
        if key in slo and metric in latency:
            check(key, float(latency[metric]), float(slo[key]), "le")
    if "error_rate" in slo:
        check("error_rate", 1.0 - float(result.get("success_rate", 0.0)), float(slo["error_rate"]), "le")
    if "min_rps" in slo:
        check("min_rps", float(result.get("requests_per_second", 0.0)), float(slo["min_rps"]), "ge")
    return {"configured": True, "passed": not violations, "checks": checks, "violations": violations}


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def run_performance(
    project: Project,
    *,
    method: str,
    path: str,
    concurrency: int = 5,
    iterations: int = 20,
    duration_seconds: float | None = None,
    headers: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    import httpx

    base_url = str(project.base_url or "").rstrip("/")
    if not base_url:
        raise ValueError("项目 base_url 未配置")
    url = base_url + ("/" + str(path).lstrip("/"))
    workers = max(1, min(int(concurrency), 50))
    total = max(1, min(int(iterations), 5000))
    duration = max(0.0, min(float(duration_seconds or 0), 300.0))
    results: list[dict[str, Any]] = []
    resource_samples = [_resource_sample()]
    last_sampled = 0
    started = time.perf_counter()
    deadline = started + duration if duration else None

    def one(index: int) -> dict[str, Any]:
        call_started = time.perf_counter()
        try:
            response = httpx.request(
                method.upper(),
                url,
                params=query or None,
                headers=headers or None,
                json=body if body is not None else None,
                timeout=timeout,
            )
            elapsed = (time.perf_counter() - call_started) * 1000
            return {
                "index": index,
                "status_code": response.status_code,
                "duration_ms": elapsed,
                "ok": 200 <= response.status_code < 400,
            }
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.perf_counter() - call_started) * 1000
            return {"index": index, "status_code": None, "duration_ms": elapsed, "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        index = 0
        while index < total and (deadline is None or time.perf_counter() < deadline):
            for _ in range(workers):
                if index >= total or (deadline is not None and time.perf_counter() >= deadline):
                    break
                futures.append(pool.submit(one, index))
                index += 1
                if len(resource_samples) < 200 and index - last_sampled >= max(1, workers * 2):
                    resource_samples.append(_resource_sample())
                    last_sampled = index
            for future in as_completed(futures):
                results.append(future.result())
            futures = []
        for future in as_completed(futures):
            results.append(future.result())
    resource_samples.append(_resource_sample())

    elapsed_total = max(0.001, time.perf_counter() - started)
    durations = [item["duration_ms"] for item in results]
    ok_count = sum(1 for item in results if item["ok"])
    result = {
        "method": method.upper(),
        "path": path,
        "concurrency": workers,
        "iterations": len(results),
        "duration_seconds": round(elapsed_total, 2),
        "requests_per_second": round(len(results) / elapsed_total, 2),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "success_rate": round(ok_count / len(results), 4) if results else 0.0,
        "latency_ms": {
            "min": round(min(durations), 2) if durations else 0.0,
            "avg": round(mean(durations), 2) if durations else 0.0,
            "p50": round(_percentile(durations, 0.50), 2),
            "p95": round(_percentile(durations, 0.95), 2),
            "p99": round(_percentile(durations, 0.99), 2),
            "max": round(max(durations), 2) if durations else 0.0,
        },
        "errors": [
            {"status_code": item["status_code"], "error": item.get("error", "")}
            for item in results
            if not item["ok"]
        ][:20],
        "resources": _resource_summary(resource_samples),
    }
    result["slo"] = evaluate_slo(project, result)
    return result


def _apply_resource_checks(project: Project, result: dict[str, Any], *, mode: str) -> dict[str, Any]:
    resource = evaluate_resources(project, result, mode=mode)
    slo = result.setdefault("slo", {"configured": False, "passed": True, "checks": [], "violations": []})
    slo["resource_checks"] = resource["checks"]
    slo["resources"] = resource["resources"]
    if resource.get("note"):
        slo["resource_note"] = resource["note"]
    if resource["configured"]:
        slo["configured"] = True
        slo["checks"] = list(slo.get("checks") or []) + resource["checks"]
        if resource["violations"]:
            slo["passed"] = False
            slo["violations"] = list(slo.get("violations") or []) + resource["violations"]
    return result


def run_performance_mode(
    project: Project,
    *,
    mode: str = "baseline",
    method: str,
    path: str,
    concurrency: int = 5,
    iterations: int = 20,
    duration_seconds: float | None = None,
    headers: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """baseline/load/soak 直接采样；stress 按并发阶梯采样并汇总。"""
    mode = mode if mode in {"baseline", "load", "stress", "soak"} else "baseline"
    if mode != "stress":
        result = run_performance(
            project,
            method=method,
            path=path,
            concurrency=concurrency,
            iterations=iterations,
            duration_seconds=duration_seconds,
            headers=headers,
            query=query,
            body=body,
            timeout=timeout,
        )
        result["mode"] = mode
        result["slo"]["mode"] = mode
        _apply_resource_checks(project, result, mode=mode)
        return result

    base = max(1, min(int(concurrency), 50))
    total = max(3, min(int(iterations), 5000))
    plan = [
        (base, max(1, total // 3)),
        (min(50, base * 2), max(1, total // 3)),
        (min(50, base * 4), max(1, total - 2 * max(1, total // 3))),
    ]
    stages: list[dict[str, Any]] = []
    combined_results: list[dict[str, Any]] = []
    for stage_concurrency, stage_iterations in plan:
        stage = run_performance(
            project,
            method=method,
            path=path,
            concurrency=stage_concurrency,
            iterations=stage_iterations,
            duration_seconds=duration_seconds,
            headers=headers,
            query=query,
            body=body,
            timeout=timeout,
        )
        stage["mode"] = "stress"
        _apply_resource_checks(project, stage, mode="stress")
        stages.append(
            {
                "concurrency": stage_concurrency,
                "iterations": stage["iterations"],
                "success_rate": stage["success_rate"],
                "p95_ms": stage["latency_ms"]["p95"],
                "p99_ms": stage["latency_ms"]["p99"],
                "rps": stage["requests_per_second"],
                "memory_growth_mb": stage["resources"].get("memory_growth_mb", 0),
                "fd_growth": stage["resources"].get("fd_growth", 0),
                "thread_growth": stage["resources"].get("thread_growth", 0),
                "slo_passed": stage["slo"]["passed"],
            }
        )
        combined_results.append(stage)
    total_iterations = sum(item["iterations"] for item in combined_results)
    failed = sum(item["failed"] for item in combined_results)
    success = total_iterations - failed
    p95 = max(item["latency_ms"]["p95"] for item in combined_results)
    p99 = max(item["latency_ms"]["p99"] for item in combined_results)
    first_resource = (combined_results[0].get("resources") or {}).get("first") or {}
    last_resource = (combined_results[-1].get("resources") or {}).get("last") or {}
    combined_resources = {
        "samples": sum(int((item.get("resources") or {}).get("samples") or 0) for item in combined_results),
        "first": first_resource,
        "last": last_resource,
        "max_rss_mb": max(float((item.get("resources") or {}).get("max_rss_mb") or 0) for item in combined_results),
        "memory_growth_mb": round(
            float(last_resource.get("rss_mb") or 0) - float(first_resource.get("rss_mb") or 0), 2
        ),
        "fd_growth": int(last_resource.get("open_fds") or 0) - int(first_resource.get("open_fds") or 0),
        "thread_growth": int(last_resource.get("threads") or 0) - int(first_resource.get("threads") or 0),
    }
    p95_series = [float(item["latency_ms"]["p95"]) for item in combined_results]
    rps_series = [float(item["requests_per_second"]) for item in combined_results]
    success_series = [float(item["success_rate"]) for item in combined_results]
    first_p95 = p95_series[0] if p95_series else 0.0
    peak_rps = max(rps_series) if rps_series else 0.0
    breakpoint = next(
        (
            stage["concurrency"]
            for stage, p95_value, success_value in zip(stages, p95_series, success_series)
            if (first_p95 and p95_value > first_p95 * 2) or success_value < 0.95
        ),
        None,
    )
    analysis = {
        "latency_growth_ratio": round(p95_series[-1] / first_p95, 2) if first_p95 else None,
        "rps_peak": round(peak_rps, 2),
        "rps_drop_from_peak": round(1 - (rps_series[-1] / peak_rps), 4) if peak_rps else 0.0,
        "success_drop": round(success_series[0] - success_series[-1], 4) if success_series else 0.0,
        "breakpoint_concurrency": breakpoint,
        "degraded": bool(
            (first_p95 and p95_series[-1] > first_p95 * 1.5)
            or (peak_rps and rps_series[-1] < peak_rps * 0.7)
            or (success_series and success_series[-1] < success_series[0] - 0.05)
        ),
    }
    result = {
        "mode": "stress",
        "method": method.upper(),
        "path": path,
        "concurrency": max(item["concurrency"] for item in combined_results),
        "iterations": total_iterations,
        "duration_seconds": round(sum(item["duration_seconds"] for item in combined_results), 2),
        "requests_per_second": round(
            sum(item["requests_per_second"] for item in combined_results) / len(combined_results), 2
        ),
        "success": success,
        "failed": failed,
        "success_rate": round(success / total_iterations, 4) if total_iterations else 0.0,
        "latency_ms": {
            "min": min(item["latency_ms"]["min"] for item in combined_results),
            "avg": round(sum(item["latency_ms"]["avg"] for item in combined_results) / len(combined_results), 2),
            "p50": round(sum(item["latency_ms"]["p50"] for item in combined_results) / len(combined_results), 2),
            "p95": p95,
            "p99": p99,
            "max": max(item["latency_ms"]["max"] for item in combined_results),
        },
        "errors": [error for item in combined_results for error in item["errors"]][:20],
        "stages": stages,
        "resources": combined_resources,
        "analysis": analysis,
    }
    result["slo"] = evaluate_slo(project, result)
    result["slo"]["mode"] = "stress"
    result["slo"]["stages"] = stages
    result["slo"]["analysis"] = analysis
    _apply_resource_checks(project, result, mode="stress")
    return result


def slo_degradation(
    db: Any,
    project: Project,
    *,
    path: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """最近一次与历史中位基线比较，识别 p95 / 成功率 / RPS 退化。"""
    from sqlalchemy import select

    stmt = select(PerformanceRun).where(PerformanceRun.project_id == project.id)
    if path:
        stmt = stmt.where(PerformanceRun.path == path)
    rows = list(
        db.scalars(stmt.order_by(PerformanceRun.id.desc()).limit(max(3, min(limit, 50)))).all()
    )
    if len(rows) < 3:
        return {"degraded": False, "reason": "样本不足（至少 3 次性能结果）", "samples": len(rows)}
    latest = rows[0]
    history = rows[1:]
    p95_baseline = _percentile([row.p95_ms for row in history], 0.50)
    p99_baseline = _percentile([row.p99_ms for row in history], 0.50)
    rps_baseline = _percentile([row.requests_per_second for row in history], 0.50)
    success_baseline = _percentile([row.success_rate for row in history], 0.50)
    reasons: list[str] = []
    if p95_baseline and latest.p95_ms > p95_baseline * 1.2:
        reasons.append(f"P95 从 {p95_baseline:.1f}ms 升至 {latest.p95_ms:.1f}ms")
    if p99_baseline and latest.p99_ms > p99_baseline * 1.2:
        reasons.append(f"P99 从 {p99_baseline:.1f}ms 升至 {latest.p99_ms:.1f}ms")
    if rps_baseline and latest.requests_per_second < rps_baseline * 0.8:
        reasons.append(f"RPS 从 {rps_baseline:.1f} 降至 {latest.requests_per_second:.1f}")
    if success_baseline and latest.success_rate < success_baseline - 0.05:
        reasons.append(f"成功率从 {success_baseline:.2%} 降至 {latest.success_rate:.2%}")
    return {
        "degraded": bool(reasons),
        "samples": len(rows),
        "latest": {
            "id": latest.id,
            "p95_ms": latest.p95_ms,
            "p99_ms": latest.p99_ms,
            "requests_per_second": latest.requests_per_second,
            "success_rate": latest.success_rate,
        },
        "baseline": {
            "p95_ms": p95_baseline,
            "p99_ms": p99_baseline,
            "requests_per_second": rps_baseline,
            "success_rate": success_baseline,
        },
        "reasons": reasons,
    }


def persist_performance(session: Any, project: Project, result: dict[str, Any]) -> PerformanceRun:
    latency = result.get("latency_ms") or {}
    row = PerformanceRun(
        project_id=project.id,
        method=str(result.get("method") or "GET")[:16],
        path=str(result.get("path") or ""),
        concurrency=int(result.get("concurrency") or 1),
        iterations=int(result.get("iterations") or 0),
        duration_seconds=float(result.get("duration_seconds") or 0.0),
        success_rate=float(result.get("success_rate") or 0.0),
        requests_per_second=float(result.get("requests_per_second") or 0.0),
        p50_ms=float(latency.get("p50") or 0.0),
        p95_ms=float(latency.get("p95") or 0.0),
        p99_ms=float(latency.get("p99") or 0.0),
        slo_passed=bool((result.get("slo") or {}).get("passed", True)),
        slo=result.get("slo") or {},
    )
    session.add(row)
    session.flush()
    return row
