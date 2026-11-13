"""HTTP 指标与 trace header。"""
from __future__ import annotations


def test_metrics_endpoint_and_trace_header(client):
    live = client.get("/api/health/live")
    assert live.status_code == 200
    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert "migration" in ready.json()

    health = client.get("/api/health")
    assert health.status_code == 200
    trace = health.headers.get("X-Shakedown-Trace-Id")
    assert trace

    metrics = client.get("/api/metrics")
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    body = metrics.text
    assert "shakedown_http_requests_total" in body
    assert "shakedown_automation_exceptions_open" in body
    assert 'path="/api/health"' in body
    assert "shakedown_tasks_total" in body
    assert "shakedown_metrics_db_aggregated 1" in body


def test_http_metrics_aggregate_across_workers(session):
    """模拟两个 worker 进程：各自 flush 后，/metrics 应看到合计值。"""
    from app.metrics import flush_metrics, record_request, render_metrics, reset_local_metrics
    from app.models import MetricCounter

    path = "/api/metrics-multi-worker"
    reset_local_metrics()
    # worker A
    record_request("GET", path, 200, 0.010)
    flush_metrics(force=True)
    reset_local_metrics()
    # worker B
    record_request("GET", path, 200, 0.020)
    flush_metrics(force=True)

    body = render_metrics(session)
    assert (
        'shakedown_http_requests_total{method="GET",path="/api/metrics-multi-worker",status="200"} 2'
        in body
    )
    assert (
        'shakedown_http_request_duration_seconds_count{method="GET",path="/api/metrics-multi-worker"} 2'
        in body
    )

    # 清理，避免跨测试污染
    session.query(MetricCounter).filter(MetricCounter.labels_key.like(f"%{path}%")).delete(
        synchronize_session=False
    )
    session.commit()
    reset_local_metrics()
