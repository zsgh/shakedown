"""Markdown 远程导入 / GitHub URL 转换 / Go 路由抽取测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.code import extract_routes
from app.ingest.markdown import (
    MarkdownSourceError,
    github_raw_url,
    is_url,
    resolve_markdown_inputs,
    split_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_github_blob_url_converted_to_raw():
    assert (
        github_raw_url("https://github.com/zsgh/mymac/blob/main/todo.md")
        == "https://raw.githubusercontent.com/zsgh/mymac/main/todo.md"
    )
    assert (
        github_raw_url("https://github.com/owner/repo/blob/main/docs/api.md")
        == "https://raw.githubusercontent.com/owner/repo/main/docs/api.md"
    )
    # 非 GitHub 地址原样返回
    assert github_raw_url("https://example.com/a.md") == "https://example.com/a.md"


def test_is_url():
    assert is_url("https://github.com/a/b")
    assert not is_url("/Users/me/repo")


def test_missing_local_path_gives_actionable_error():
    with pytest.raises(MarkdownSourceError) as excinfo:
        resolve_markdown_inputs("/no/such/path/xyz")
    message = str(excinfo.value)
    assert "路径不存在" in message
    assert "本地" in message  # 告诉用户该填什么


def test_local_directory_resolves_to_files(tmp_path: Path):
    (tmp_path / "a.md").write_text("# A\n\n内容", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# B\n\n内容", encoding="utf-8")
    inputs = resolve_markdown_inputs(str(tmp_path))
    assert len(inputs) == 2
    texts = [text for text, _origin in inputs]
    assert any("内容" in text for text in texts)


def test_go_serve_mux_routes():
    """Go 1.22 ServeMux：mux.Handle("GET /api/procs", ...)"""
    content = """
    s.mux.Handle("GET /api/procs", s.withGuards(http.HandlerFunc(s.handleProcs)))
    s.mux.Handle("POST /api/procs/{pid}/signal", s.withGuards(http.HandlerFunc(s.handleSignal)))
    s.mux.Handle("GET /{$}", s.withGuards(http.HandlerFunc(s.handleIndex)))
    """
    root = Path("/tmp")
    routes = extract_routes(root / "server.go", content, root)
    keys = {(route.method, route.path) for route in routes}
    assert ("GET", "/api/procs") in keys
    assert ("POST", "/api/procs/{pid}/signal") in keys
    assert ("GET", "/{$}") in keys


def test_go_legacy_handlefunc_route():
    content = 'http.HandleFunc("/api/health", handler)'
    root = Path("/tmp")
    routes = extract_routes(root / "main.go", content, root)
    assert {(r.method, r.path) for r in routes} == {("GET", "/api/health")}


def test_decorator_route_still_works():
    content = '@app.get("/api/users")\ndef list_users():\n    pass\n'
    root = Path("/tmp")
    routes = extract_routes(root / "main.py", content, root)
    assert {(r.method, r.path) for r in routes} == {("GET", "/api/users")}


def test_markdown_split_of_remote_text():
    chunks = split_markdown("# 标题\n\n正文\n\n## 二级\n\n更多\n", "https://example.com/a.md")
    assert len(chunks) >= 2
    assert chunks[0].file_path == "https://example.com/a.md"
