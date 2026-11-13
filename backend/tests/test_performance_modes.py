"""性能模式（stress / soak）与 SLO 退化检测。"""
from __future__ import annotations

from app.models import PerformanceRun
from app.performance import slo_degradation


def test_stress_mode_runs_stages(client, sample_project, monkeypatch):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    body = client.post(
        f"/api/projects/{sample_project.id}/performance/run",
        json={"mode": "stress", "method": "GET", "path": "/api/health", "concurrency": 2, "iterations": 9},
    ).json()
    assert body["mode"] == "stress"
    assert len(body["stages"]) == 3
    assert body["iterations"] == 9
    assert body["slo"]["mode"] == "stress"

    history = client.get(f"/api/projects/{sample_project.id}/performance/history").json()
    assert history["items"][0]["mode"] == "stress"
    assert "degradation" in history


def test_slo_degradation_detects_latency_and_success_drop(session, sample_project):
    rows = [
        PerformanceRun(project_id=sample_project.id, path="/api/x", p95_ms=100, p99_ms=150, requests_per_second=50, success_rate=1.0),
        PerformanceRun(project_id=sample_project.id, path="/api/x", p95_ms=105, p99_ms=160, requests_per_second=48, success_rate=1.0),
        PerformanceRun(project_id=sample_project.id, path="/api/x", p95_ms=102, p99_ms=155, requests_per_second=49, success_rate=1.0),
        PerformanceRun(project_id=sample_project.id, path="/api/x", p95_ms=300, p99_ms=500, requests_per_second=20, success_rate=0.8),
    ]
    session.add_all(rows)
    session.commit()

    result = slo_degradation(session, sample_project, path="/api/x", limit=5)
    assert result["degraded"] is True
    assert len(result["reasons"]) >= 2
    assert result["latest"]["p95_ms"] == 300


def test_stress_reports_resource_growth_and_breakpoint(client, sample_project, monkeypatch):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    body = client.post(
        f"/api/projects/{sample_project.id}/performance/run",
        json={"mode": "stress", "method": "GET", "path": "/api/health", "concurrency": 2, "iterations": 9},
    ).json()
    assert "resources" in body
    assert body["resources"]["samples"] >= 2
    assert "analysis" in body
    assert "breakpoint_concurrency" in body["analysis"]
    assert all("memory_growth_mb" in stage for stage in body["stages"])


def test_soak_fails_when_resource_growth_exceeds_limit(client, session, sample_project, monkeypatch):
    class Response:
        status_code = 200

    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: Response())
    sample_project.settings_json = {
        "performance_resource_limits": {"max_memory_growth_mb": 10, "max_fd_growth": 2}
    }
    session.add(sample_project)
    session.commit()

    values = iter(
        [
            {"rss_mb": 100, "open_fds": 10, "threads": 2},
            {"rss_mb": 130, "open_fds": 20, "threads": 3},
            {"rss_mb": 180, "open_fds": 40, "threads": 5},
        ]
    )
    monkeypatch.setattr("app.performance._resource_sample", lambda: next(values, {"rss_mb": 180, "open_fds": 40, "threads": 5}))

    body = client.post(
        f"/api/projects/{sample_project.id}/performance/run",
        json={"mode": "soak", "method": "GET", "path": "/api/health", "concurrency": 1, "iterations": 3},
    ).json()
    assert body["resources"]["memory_growth_mb"] == 80
    assert body["resources"]["fd_growth"] == 30
    assert body["slo"]["passed"] is False
    names = {check["name"] for check in body["slo"]["resource_checks"]}
    assert {"max_memory_growth_mb", "max_fd_growth"} <= names
    assert any("max_memory_growth_mb" in item for item in body["slo"]["violations"])
