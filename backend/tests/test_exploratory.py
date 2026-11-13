"""探索式执行层：差分 / fuzz / 变形。"""
from __future__ import annotations

from app.exploratory import run_differential, run_fuzz, run_metamorphic


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else "json"
        self.headers = {}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def test_differential_detects_body_key_drift(sample_project, monkeypatch):
    def fake_request(method, url, *, params=None, json=None, headers=None, timeout=None):
        if (params or {}).get("page") == 1:
            return _Response(200, {"items": [], "total": 0})
        return _Response(200, {"data": [], "total": 0})

    monkeypatch.setattr("httpx.request", fake_request)
    result = run_differential(
        sample_project,
        method="GET",
        path="/api/items",
        variants=[{"name": "p1", "query": {"page": 1}}, {"name": "p2", "query": {"page": 2}}],
    )
    assert result["mode"] == "differential"
    assert any(item["kind"] == "body_keys" for item in result["differences"])


def test_fuzz_flags_5xx_and_leak(sample_project, monkeypatch):
    monkeypatch.setattr(
        "httpx.request",
        lambda *args, **kwargs: _Response(500, "Traceback: SQLSTATE 42P01"),
    )
    result = run_fuzz(
        sample_project,
        method="POST",
        path="/api/items",
        payloads=[{"name": "x"}],
    )
    assert result["signals"]
    assert {item["signal"] for item in result["signals"]} == {"5xx", "possible_information_leak"}


def test_metamorphic_detects_field_mismatch(sample_project, monkeypatch):
    def fake_request(method, url, *, params=None, json=None, headers=None, timeout=None):
        if method == "POST":
            return _Response(201, {"id": 7, "name": "alice"})
        return _Response(200, {"id": 7, "name": "bob"})

    monkeypatch.setattr("httpx.request", fake_request)
    result = run_metamorphic(
        sample_project,
        method="POST",
        path="/api/items",
        body={"name": "alice"},
        read_path="/api/items/{id}",
    )
    assert result["identifier"] == 7
    assert result["differences"][0]["field"] == "name"


def test_exploratory_api_requires_allow_write(client, sample_project, monkeypatch):
    response = client.post(
        f"/api/projects/{sample_project.id}/exploratory/run",
        json={"mode": "fuzz", "method": "POST", "path": "/api/items", "payloads": [{}]},
    )
    assert response.status_code == 400


def test_property_mode_passes_and_reports_failures(sample_project, monkeypatch):
    from app.exploratory import run_property

    responses = iter(
        [
            _Response(200, {"id": 1, "name": "alice"}),
            _Response(500, {"id": 2}),
        ]
    )
    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: next(responses))

    result = run_property(
        sample_project,
        method="GET",
        path="/api/items",
        properties=[
            {"name": "ok_status", "kind": "status_in", "values": [200, 201]},
            {"name": "has_id", "kind": "body_has_keys", "keys": ["id", "name"]},
            {"name": "fast", "kind": "latency_le", "limit_ms": 5000},
        ],
        cases=[{"name": "good", "query": {"page": 1}}, {"name": "bad", "query": {"page": 2}}],
    )
    assert result["mode"] == "property"
    assert result["case_count"] == 2
    assert result["passed"] is False
    assert {item["property"] for item in result["failures"]} == {"ok_status", "has_id"}
    assert result["definitions"][0]["name"] == "ok_status"


def test_property_mode_repeat_equal_detects_drift(sample_project, monkeypatch):
    from app.exploratory import run_property

    responses = iter(
        [
            _Response(200, {"id": 1}),
            _Response(200, {"id": 1}),
            _Response(200, {"id": 2}),
        ]
    )
    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: next(responses))
    result = run_property(
        sample_project,
        method="GET",
        path="/api/items",
        properties=[{"name": "stable", "kind": "repeat_equal", "times": 3}],
        cases=[{"name": "one"}],
    )
    assert result["passed"] is False
    assert result["failures"][0]["kind"] == "repeat_equal"


def test_property_mode_generator_and_project_defaults(client, session, sample_project, monkeypatch):
    sample_project.settings_json = {
        "exploratory_properties": [{"name": "ok", "kind": "status_between", "min": 200, "max": 299}]
    }
    session.add(sample_project)
    session.commit()
    monkeypatch.setattr("httpx.request", lambda *args, **kwargs: _Response(200, {"ok": True}))

    body = client.post(
        f"/api/projects/{sample_project.id}/exploratory/run",
        json={
            "mode": "property",
            "method": "GET",
            "path": "/api/items",
            "properties": [],
            "generator": {
                "count": 3,
                "seed": 1,
                "fields": [{"name": "email", "type": "string"}],
            },
        },
    ).json()
    assert body["case_count"] == 3
    assert body["passed"] is True
    assert body["property_count"] == 1
