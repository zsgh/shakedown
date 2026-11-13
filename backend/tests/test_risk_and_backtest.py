"""高风险操作确认 + 回测脚本的关键逻辑测试。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.executor.risk import high_risk_reasons, is_whitelisted

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_backtest_module():
    import sys

    path = REPO_ROOT / "scripts" / "backtest.py"
    spec = importlib.util.spec_from_file_location("shakedown_backtest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["shakedown_backtest"] = module
    spec.loader.exec_module(module)
    return module


def test_delete_step_is_high_risk():
    spec = {"steps": [{"action": "request", "method": "DELETE", "path": "/api/users/1"}]}
    reasons = high_risk_reasons(spec, {})
    assert reasons
    assert "DELETE" in reasons[0]


def test_whitelist_allows_test_data_paths():
    settings = {"risk_whitelist": ["DELETE /api/test-data/*"]}
    spec = {"steps": [{"action": "request", "method": "DELETE", "path": "/api/test-data/42"}]}
    assert high_risk_reasons(spec, settings) == []


def test_is_whitelisted_supports_wildcards():
    assert is_whitelisted({"method": "PUT", "path": "/api/config/x"}, ["PUT /api/config/*"])
    assert not is_whitelisted({"method": "DELETE", "path": "/api/users/1"}, ["PUT /api/config/*"])


def test_explicit_risk_flag():
    assert high_risk_reasons({"risk": "high", "steps": []}, {})


def test_api_requires_confirmation_for_destructive_case(client):
    response = client.post("/api/projects", json={"name": "risk", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]

    spec = {
        "title": "删除用户",
        "dimension": "functional",
        "type": "api",
        "steps": [{"action": "request", "method": "DELETE", "path": "/api/users/1"}],
        "assertions": [],
    }
    response = client.post(f"/api/projects/{project_id}/cases", json={"title": "删除用户", "spec": spec})
    assert response.status_code == 201
    case_id = response.json()["id"]

    response = client.post(f"/api/cases/{case_id}/status", json={"status": "enabled"})
    assert response.status_code == 409
    assert "高风险" in response.json()["error"]["message"]

    response = client.post(f"/api/cases/{case_id}/status", json={"status": "enabled", "confirm_risk": True})
    assert response.status_code == 200
    assert response.json()["status"] == "enabled"


def test_backtest_bug_matching():
    module = _load_backtest_module()
    bug = module.Bug(id="B1", commit="abc", paths=["/api/orders"], keywords=["amount"])
    case = type("Case", (), {"spec": {"steps": [{"path": "/api/orders"}]}})()
    assert module.match_case(case, bug)
    other = type("Case", (), {"spec": {"steps": [{"path": "/api/users"}]}})()
    assert not module.match_case(other, bug)
