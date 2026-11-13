"""可视化配置：系统设置（全局）+ 项目设置（测试账号等）。"""
from __future__ import annotations

from app import settings_store


def test_settings_read_and_update(client):
    response = client.get("/api/settings")
    assert response.status_code == 200
    payload = response.json()
    assert "effective" in payload and "schema" in payload
    assert any(item["key"] == "llm_provider" for item in payload["schema"])

    response = client.put("/api/settings", json={"values": {"llm_model": "test-model"}})
    assert response.status_code == 200
    assert response.json()["effective"]["llm_model"] == "test-model"

    response = client.get("/api/settings")
    assert response.json()["effective"]["llm_model"] == "test-model"
    assert "llm_model" in response.json()["overrides"]

    response = client.post("/api/settings/reset")
    assert response.status_code == 200


def test_settings_reject_unknown_key(client):
    response = client.put("/api/settings", json={"values": {"hacker_key": "x"}})
    assert response.status_code == 422


def test_settings_mask_secrets(client):
    settings_store.set_values.__self__ if False else None
    response = client.put("/api/settings", json={"values": {"llm_api_key": "sk-secret-value"}})
    assert response.status_code == 200
    assert response.json()["effective"]["llm_api_key"] == "********"
    response = client.post("/api/settings/reset")


def test_settings_masked_secret_is_not_overwritten(client):
    """设置页回显 ********，原样保存不应该把真实密钥覆盖掉。"""
    try:
        client.put("/api/settings", json={"values": {"llm_api_key": "sk-secret-value"}})
        effective = client.get("/api/settings").json()["effective"]
        assert effective["llm_api_key"] == "********"

        response = client.put("/api/settings", json={"values": effective})
        assert response.status_code == 200
        assert settings_store.get_value("llm_api_key") == "sk-secret-value"
    finally:
        client.post("/api/settings/reset")


def test_settings_boolean_string_value(client):
    """前端 select 提交的是字符串 false，不能当成 bool('false') == True。"""
    try:
        client.put("/api/settings", json={"values": {"worker_enabled": "false"}})
        effective = client.get("/api/settings").json()["effective"]
        assert effective["worker_enabled"] is False
    finally:
        client.post("/api/settings/reset")


def test_project_settings_update_accounts(client):
    response = client.post("/api/projects", json={"name": "cfg", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]

    response = client.patch(
        f"/api/projects/{project_id}",
        json={
            "test_accounts": {"default": {"username": "u1", "password": "p1", "role": "user"}},
            "isolation": {"mode": "prefix", "db_service": "cfg-db"},
            "settings": {"risk_whitelist": ["DELETE /api/test-data/*"]},
        },
    )
    assert response.status_code == 200
    accounts = response.json()["test_accounts"]
    assert accounts["default"]["username"] == "u1"
    assert accounts["default"]["password"] == "********"  # 密码不回显
    assert response.json()["isolation"]["db_service"] == "cfg-db"
    assert response.json()["settings"]["risk_whitelist"] == ["DELETE /api/test-data/*"]


def test_project_settings_masked_password_is_not_overwritten(client):
    """前端回显的是 ********，再保存一次不应该把密码清掉。"""
    from app.crypto import decrypt_json
    from app.db import session_scope
    from app.models import Project

    response = client.post("/api/projects", json={"name": "cfg2", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    client.patch(
        f"/api/projects/{project_id}",
        json={"test_accounts": {"default": {"username": "u1", "password": "p1"}}},
    )
    # 前端拿到的是打码值，再提交一次
    masked = client.get(f"/api/projects/{project_id}").json()["test_accounts"]
    client.patch(f"/api/projects/{project_id}", json={"test_accounts": masked})

    with session_scope() as session:
        project = session.get(Project, project_id)
        accounts = decrypt_json(project.test_accounts_encrypted) or {}
    assert accounts["default"]["password"] == "p1"


def test_settings_probe(client):
    response = client.get("/api/settings/probe")
    assert response.status_code == 200
    assert "llm" in response.json() and "sandbox" in response.json()
