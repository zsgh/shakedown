"""仓库自动发现 / 一键导入 / 文档端点清单抽取测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.discovery import discover_repository
from app.ingest.docapi import extract_doc_endpoints, looks_like_api_doc

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "examples" / "demo-project"


def _chunks(text: str, name: str = "doc.md") -> list[dict]:
    from app.ingest.markdown import split_markdown

    parsed = split_markdown(text, name)
    return [
        {
            "id": index,
            "content": chunk.content,
            "locator": {
                "file_path": chunk.file_path,
                "section_path": chunk.section_path,
                "start_line": chunk.start_line,
            },
        }
        for index, chunk in enumerate(parsed)
    ]


def test_discover_repository_finds_code_and_docs():
    discovery = discover_repository(str(DEMO))
    assert discovery.code_files
    assert any(f.endswith("main.py") for f in discovery.code_files)
    assert any(f.endswith("api.md") for f in discovery.markdown_files)
    # demo-project 的 specs/ 下有 Swagger 2.0 与 OpenAPI 3.0
    assert any(f.endswith("swagger.json") for f in discovery.openapi_files)
    assert any(f.endswith("openapi.yaml") for f in discovery.openapi_files)
    assert discovery.as_dict()["code"]["count"] == len(discovery.code_files)


def test_discover_repository_detects_openapi_file(tmp_path: Path):
    (tmp_path / "api.json").write_text(
        json.dumps({"openapi": "3.0.0", "paths": {"/x": {"get": {"responses": {}}}}}), encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
    discovery = discover_repository(str(tmp_path))
    assert "api.json" in discovery.openapi_files
    assert "package.json" not in discovery.openapi_files


def test_discover_repository_missing_path():
    with pytest.raises(FileNotFoundError):
        discover_repository("/no/such/repo")


def test_extract_doc_endpoints_from_code_block():
    text = """
## 3.2 端点

```
GET    /api/health             {version, uptime}
POST   /api/users             创建
PATCH  /api/users/{id}        改值
```

其它说明文字，不该被误抓。
"""
    endpoints = extract_doc_endpoints(_chunks(text))
    keys = {(e.method, e.path) for e in endpoints}
    assert ("GET", "/api/health") in keys
    assert ("POST", "/api/users") in keys
    assert ("PATCH", "/api/users/{id}") in keys
    summary = {e.path: e.summary for e in endpoints}
    assert summary["/api/health"] == "{version, uptime}"
    assert summary["/api/users"] == "创建"


def test_extract_doc_endpoints_from_table():
    text = """
## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/items | 列表 |
| DELETE | /api/items/{id} | 删除 |
"""
    endpoints = extract_doc_endpoints(_chunks(text))
    keys = {(e.method, e.path) for e in endpoints}
    assert ("GET", "/api/items") in keys
    assert ("DELETE", "/api/items/{id}") in keys


def test_extract_doc_endpoints_ignores_prose():
    text = "这是一段普通说明文字，GET 请求会返回 200，但没有接口路径。\n"
    assert extract_doc_endpoints(_chunks(text)) == []


def test_looks_like_api_doc():
    assert looks_like_api_doc(_chunks("## API\n\nGET /api/a\nPOST /api/b\n"))
    assert not looks_like_api_doc(_chunks("## 说明\n\n就是一段文字。\n"))


def test_auto_import_creates_sources(client):
    """给一个仓库路径，一次导入代码 + 文档。"""
    response = client.post("/api/projects", json={"name": "auto", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]

    response = client.post(
        f"/api/projects/{project_id}/sources/auto-import",
        json={"path": str(DEMO)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    kinds = {item["kind"] for item in payload["sources"]}
    assert "code" in kinds
    assert "markdown" in kinds
    assert all(item["status"] == "ready" for item in payload["sources"])

    sources = client.get(f"/api/projects/{project_id}/sources").json()
    assert len(sources) >= 2


def test_discover_endpoint(client):
    response = client.post("/api/projects", json={"name": "disc", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    response = client.post(f"/api/projects/{project_id}/sources/discover", json={"path": str(DEMO)})
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"]["count"] > 0
    assert payload["markdown"]["count"] > 0


def test_auto_import_reports_missing_path(client):
    response = client.post("/api/projects", json={"name": "missing", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    response = client.post(f"/api/projects/{project_id}/sources/auto-import", json={"path": "/no/such/path"})
    assert response.status_code == 404
