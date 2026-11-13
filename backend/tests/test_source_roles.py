"""知识源角色（spec / notes）的测试。"""
from __future__ import annotations

from pathlib import Path

from app.ingest.docapi import is_dev_log


def test_dev_log_files_are_notes():
    assert is_dev_log("/repo/todo.md")
    assert is_dev_log("docs/changelog.md")
    assert not is_dev_log("docs/DESIGN.md")


def test_markdown_source_roles_split_per_file(client, tmp_path: Path):
    """同一个源里：含端点表的文件是 spec，开发日志/说明是 notes。"""
    (tmp_path / "DESIGN.md").write_text(
        "# 设计\n\n## 端点\n\n```\nGET /api/x   说明\nPOST /api/y  说明\n```\n", encoding="utf-8"
    )
    (tmp_path / "todo.md").write_text("# 待办\n\n- 修一个 bug\n- GET /api/z 老接口\n", encoding="utf-8")

    response = client.post("/api/projects", json={"name": "roles", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "markdown", "location": str(tmp_path)},
    )
    assert response.status_code == 201
    source = response.json()
    assert source["role"] == "spec"  # 源级：有规范文件 → spec
    assert source["meta"]["file_roles"]["DESIGN.md"] == "spec"
    assert source["meta"]["file_roles"]["todo.md"] == "notes"

    # todo.md 的接口提及不应被当成接口
    endpoints = client.get(f"/api/projects/{project_id}/endpoints").json()
    paths = {e["path"] for e in endpoints}
    assert "/api/x" in paths
    assert "/api/z" not in paths


def test_source_role_can_be_overridden(client, tmp_path: Path):
    (tmp_path / "notes.md").write_text("# 说明\n\n随便写点东西。\n", encoding="utf-8")
    response = client.post("/api/projects", json={"name": "override", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/sources?sync=true",
        json={"kind": "markdown", "location": str(tmp_path)},
    )
    source_id = response.json()["id"]
    assert response.json()["role"] == "notes"

    response = client.patch(f"/api/sources/{source_id}", json={"role": "spec"})
    assert response.status_code == 200
    assert response.json()["role"] == "spec"


def test_consistency_includes_weak_signals_by_default(client):
    """文档缺口是「改进项」，默认就要能看到，不折叠。"""
    response = client.post("/api/projects", json={"name": "weak", "base_url": "http://127.0.0.1:1"})
    project_id = response.json()["id"]
    response = client.get(f"/api/projects/{project_id}/consistency")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    # 显式关闭才只看强信号
    response = client.get(f"/api/projects/{project_id}/consistency?include_weak=false")
    assert response.status_code == 200
