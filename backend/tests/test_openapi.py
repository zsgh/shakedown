"""OpenAPI / Swagger 解析测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.openapi import (
    OpenAPIError,
    content_hash,
    detect_version,
    diff_specs,
    load_spec,
    merge_all_of,
    normalize,
    resolve_refs,
    validate_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "examples" / "demo-project" / "specs"


def test_load_swagger_2():
    spec, _text, _origin = load_spec(str(DEMO / "swagger.json"))
    assert detect_version(spec) == "2.0"
    endpoints = normalize(spec)
    paths = {(e.method, e.path) for e in endpoints}
    assert ("POST", "/api/users") in paths
    assert ("GET", "/api/users/{id}") in paths
    assert validate_spec(spec) == []


def test_load_openapi_3():
    spec, _text, _origin = load_spec(str(DEMO / "openapi.yaml"))
    assert detect_version(spec) == "3.0"
    endpoints = normalize(spec)
    by_key = {(e.method, e.path): e for e in endpoints}
    assert ("GET", "/api/admin/stats") in by_key
    stats = by_key[("GET", "/api/admin/stats")]
    assert stats.security["required"] is True
    assert stats.security["schemes"][0]["scheme"] == "bearer"


def test_ref_expansion_and_cycle_guard():
    spec = {
        "openapi": "3.0.0",
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }
            }
        },
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}},
                        }
                    }
                }
            }
        },
    }
    resolved = resolve_refs(spec)
    # 循环引用被标记并停止展开
    assert "x-circular" in json.dumps(resolved)
    endpoints = normalize(spec)
    child = endpoints[0].responses["200"]["schema"]["properties"]["child"]
    assert child.get("x-circular") is True


def test_all_of_merge():
    merged = merge_all_of(
        {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ]
        }
    )
    assert set(merged["properties"]) == {"a", "b"}
    assert set(merged["required"]) == {"a", "b"}


def test_normalized_endpoint_fields():
    spec, _t, _o = load_spec(str(DEMO / "swagger.json"))
    endpoints = {(e.method, e.path): e for e in normalize(spec)}
    create_user = endpoints[("POST", "/api/users")]
    assert create_user.request_body["schema"]["properties"]["name"]["minLength"] == 1
    assert create_user.responses["201"]["schema"]["properties"]["id"]["type"] == "integer"
    assert {p["name"] for p in create_user.params["query"]} == set()


def test_diff_specs_detects_changes():
    old = [{"method": "GET", "path": "/a", "params": {"query": []}, "request_body": {}, "responses": {}, "security": {}}]
    new = [
        {"method": "GET", "path": "/a", "params": {"query": [{"name": "size"}]}, "request_body": {}, "responses": {}, "security": {}},
        {"method": "GET", "path": "/b", "params": {"query": []}, "request_body": {}, "responses": {}, "security": {}},
    ]
    diff = diff_specs(old, new)
    assert diff["added"] and diff["added"][0]["path"] == "/b"
    assert diff["changed"] and diff["changed"][0]["path"] == "/a"


def test_broken_spec_raises_readable_error(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"openapi": "3.0.0", "paths": {"/x": {"get": {"responses": "oops"}}}}', encoding="utf-8")
    with pytest.raises(OpenAPIError):
        load_spec(str(bad))


def test_missing_file_raises():
    with pytest.raises(OpenAPIError):
        load_spec("/nonexistent/spec.json")


def test_content_hash_stable():
    spec = {"openapi": "3.0.0", "paths": {}}
    assert content_hash(spec) == content_hash(json.loads(json.dumps(spec)))
