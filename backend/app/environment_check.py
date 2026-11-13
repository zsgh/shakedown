"""被测环境就绪检查：环境没准备好时，不把问题算成产品缺陷。"""
from __future__ import annotations

import time
from typing import Any

from app.models import Project


def check_target(project: Project, *, timeout: float | None = None) -> dict[str, Any]:
    """按项目配置做一次轻量探活；返回可写入 run.env_snapshot / stats 的结果。"""
    settings = project.settings_json or {}
    base_url = str(project.base_url or "").strip()
    if not base_url:
        return {"ok": False, "reason": "base_url 未配置", "url": "", "status_code": None}

    probes = settings.get("readiness_probes")
    if isinstance(probes, list) and probes:
        results: list[dict[str, Any]] = []
        overall = True
        for probe in probes:
            if not isinstance(probe, dict):
                continue
            probe_path = str(probe.get("path") or "/").strip()
            if not probe_path.startswith("/"):
                probe_path = "/" + probe_path
            probe_method = str(probe.get("method") or "GET").upper()
            probe_url = base_url.rstrip("/") + probe_path
            probe_expected = probe.get("expected_status", [200, 204])
            if isinstance(probe_expected, int):
                probe_expected = [probe_expected]
            probe_status = {int(item) for item in probe_expected} if isinstance(probe_expected, list) else {200, 204}
            probe_headers = probe.get("headers") if isinstance(probe.get("headers"), dict) else {}
            started = time.perf_counter()
            try:
                import httpx

                response = httpx.request(
                    probe_method,
                    probe_url,
                    timeout=float(probe.get("timeout", settings.get("readiness_timeout", 5.0))),
                    follow_redirects=True,
                    headers=probe_headers,
                )
                ok = response.status_code in probe_status
                item = {
                    "ok": ok,
                    "url": probe_url,
                    "method": probe_method,
                    "status_code": response.status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            except Exception as exc:  # noqa: BLE001
                ok = False
                item = {
                    "ok": False,
                    "url": probe_url,
                    "method": probe_method,
                    "status_code": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            overall = overall and ok
            results.append(item)
        return {
            "ok": overall,
            "reason": "" if overall else "部分 readiness 探针失败",
            "probes": results,
        }

    configured_path = str(settings.get("readiness_path") or "").strip()
    paths = [configured_path] if configured_path else ["/health", "/api/health", "/"]
    paths = ["/" + item.lstrip("/") if item else "/" for item in paths]
    expected = settings.get("readiness_expected_status", [200, 204])
    if isinstance(expected, int):
        expected = [expected]
    expected_status = {int(item) for item in expected} if isinstance(expected, list) else {200, 204}
    request_timeout = float(timeout if timeout is not None else settings.get("readiness_timeout", 5.0))
    headers = settings.get("readiness_headers")
    request_headers = headers if isinstance(headers, dict) else {}

    last_result: dict[str, Any] = {}
    for path in paths:
        url = base_url.rstrip("/") + path
        started = time.perf_counter()
        try:
            import httpx

            response = httpx.get(
                url,
                timeout=request_timeout,
                follow_redirects=True,
                headers=request_headers,
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            ok = response.status_code in expected_status
            last_result = {
                "ok": ok,
                "reason": "" if ok else f"HTTP {response.status_code}，期望 {sorted(expected_status)}",
                "url": url,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            }
            if ok:
                return last_result
        except Exception as exc:  # noqa: BLE001 —— 环境不可达是预期分支
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            last_result = {
                "ok": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "url": url,
                "status_code": None,
                "elapsed_ms": elapsed_ms,
            }
    return last_result or {"ok": False, "reason": "没有可用的 readiness 路径", "url": base_url}
