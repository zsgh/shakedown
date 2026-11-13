"""边界校验回归：空名称、负数 limit 必须由 API 层拒绝。"""
from __future__ import annotations


def _project(client, name: str = "validation") -> int:
    response = client.post("/api/projects", json={"name": name, "base_url": "http://127.0.0.1:1"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_empty_names_are_rejected(client):
    project_id = _project(client, "empty-name")

    response = client.post(f"/api/projects/{project_id}/features", json={"name": ""})
    assert response.status_code == 422, response.text

    response = client.patch(f"/api/projects/{project_id}", json={"name": ""})
    assert response.status_code == 422, response.text

    created = client.post(f"/api/projects/{project_id}/features", json={"name": "有效功能点"})
    assert created.status_code == 201, created.text
    feature_id = created.json()["id"]

    response = client.patch(f"/api/features/{feature_id}", json={"name": ""})
    assert response.status_code == 422, response.text


def test_consistency_summary_without_api_docs(client):
    project_id = _project(client, "consistency-no-docs")
    response = client.get(f"/api/projects/{project_id}/consistency/summary")
    assert response.status_code == 200, response.text
    assert response.json()["doc_coverage"] is None


def test_openapi_documents_custom_error_envelope(client):
    schema = client.get("/openapi.json").json()
    assert "ErrorEnvelope" in schema["components"]["schemas"]
    operation = schema["paths"]["/api/projects"]["post"]
    error_schema = operation["responses"]["422"]["content"]["application/json"]["schema"]
    assert error_schema == {"$ref": "#/components/schemas/ErrorEnvelope"}


def test_negative_limit_is_rejected(client):
    project_id = _project(client, "negative-limit")
    for path in (
        "/api/tasks?limit=-1",
        f"/api/projects/{project_id}/search?q=test&limit=-1",
        f"/api/projects/{project_id}/cases?limit=-1",
        f"/api/projects/{project_id}/findings?limit=-1",
        f"/api/projects/{project_id}/runs?limit=-1",
    ):
        response = client.get(path)
        assert response.status_code == 422, f"{path} -> {response.status_code}: {response.text[:300]}"
