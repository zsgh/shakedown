"""Markdown / 代码切块、路由抽取、一致性对照测试。"""
from __future__ import annotations

from pathlib import Path

from app.ingest.code import chunk_code_file, extract_routes, iter_source_files
from app.ingest.consistency import check_consistency, extract_doc_mentions, normalize_path
from app.ingest.markdown import split_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO = REPO_ROOT / "examples" / "demo-project"


def test_markdown_split_keeps_section_path():
    text = """# 标题

正文一

## 子标题

正文二

### 更深

正文三
"""
    chunks = split_markdown(text, "doc.md")
    assert chunks
    paths = [chunk.section_path for chunk in chunks]
    assert ["标题"] in paths
    assert ["标题", "子标题"] in paths
    assert ["标题", "子标题", "更深"] in paths
    # 每个 chunk 都要有起止行号，便于跳转原文
    assert all(chunk.start_line >= 1 and chunk.end_line >= chunk.start_line for chunk in chunks)


def test_markdown_fence_not_treated_as_heading():
    text = "正文\n```\n# 不是标题\n```\n"
    chunks = split_markdown(text, "doc.md")
    assert len(chunks) == 1


def test_code_chunking_and_routes():
    root = DEMO / "app"
    files = iter_source_files(root)
    assert files
    main = root / "main.py"
    chunks = chunk_code_file(main, root)
    assert chunks
    assert all(chunk.file_path == "main.py" for chunk in chunks)
    assert all(chunk.start_line >= 1 for chunk in chunks)

    routes = extract_routes(main, main.read_text(encoding="utf-8"), root)
    keys = {(route.method, route.path) for route in routes}
    assert ("GET", "/api/users") in keys
    assert ("POST", "/api/orders") in keys
    assert ("DELETE", "/api/users/{id}") in keys


def test_normalize_path_variants():
    assert normalize_path("/api/users/") == "/api/users"
    assert normalize_path("api/users") == "/api/users"
    assert normalize_path("/api/users/:id") == "/api/users/{id}"
    assert normalize_path("/api/users/<id>") == "/api/users/{id}"
    assert normalize_path("/api/artifacts/{path:path}") == "/api/artifacts/{path}"


def test_route_extraction_ignores_comments_docstrings_and_non_api_relative_calls():
    from app.ingest.code import extract_routes

    content = '''"""\n@router.get("/{project_id}")\n"""\n# @router.delete("/x")\nr.get("total")\n@router.get("")\n'''
    routes = extract_routes(Path("api/demo.py"), content, REPO_ROOT)
    assert [(r.method, r.path) for r in routes] == [("GET", "")]


def test_router_prefix_applied_to_local_route():
    from app.ingest.service import _router_prefix

    assert _router_prefix('router = APIRouter(prefix="/projects")\n') == "/projects"
    assert _router_prefix('router = APIRouter()\n') == ""


def test_doc_mentions_extracted():
    chunks = [
        {
            "id": 1,
            "content": "## 用户列表\n\n`GET /api/users` 返回用户列表，参数 `page`、`size`。",
            "locator": {"file_path": "docs/api.md", "section_path": ["用户列表"]},
        }
    ]
    mentions = extract_doc_mentions(chunks)
    assert any(m["path"] == "/api/users" for m in mentions)
    assert any(m["method"] == "GET" for m in mentions)


def test_consistency_reports_doc_only_and_code_only():
    openapi = [{"method": "GET", "path": "/api/users", "params": {"query": []}, "responses": {}, "security": {}}]
    code = [{"method": "GET", "path": "/api/secret", "params": {"query": []}, "responses": {}, "security": {}}]
    docs = [{"method": "GET", "path": "/api/reports", "ref": "docs/api.md", "chunk_id": 3, "quote": "GET /api/reports"}]
    report = check_consistency(openapi, code, docs)
    states = {key: value["state"] for key, value in report.items()}
    # 项目有 spec 与代码 → 两边都没有实现的接口是强信号
    assert states["GET /api/secret"] == "inconsistent"
    assert states["GET /api/reports"] == "inconsistent"
    kinds = {mismatch["kind"] for value in report.values() for mismatch in value["mismatches"]}
    assert "code_only" in kinds
    assert "spec_only" in kinds


def test_consistency_without_spec_is_only_weak_signal():
    """项目没有 spec 时，「代码有、文档没覆盖」只是文档缺口（partial），不是不一致。"""
    code = [{"method": "GET", "path": "/api/apps", "params": {"query": []}, "responses": {}, "security": {}}]
    docs = [{"method": "GET", "path": "/api/health", "ref": "docs/api.md", "chunk_id": 3, "quote": "GET /api/health"}]
    report = check_consistency([], code, docs)
    states = {key: value["state"] for key, value in report.items()}
    assert states["GET /api/apps"] == "partial"
    assert states["GET /api/health"] == "inconsistent"  # 文档提到但代码里没有 → 强信号


def test_consistency_single_source_project_is_unknown():
    """项目只有代码一类来源 → 无从对照，不算问题。"""
    code = [{"method": "GET", "path": "/api/apps", "params": {"query": []}, "responses": {}, "security": {}}]
    report = check_consistency([], code, [])
    assert report["GET /api/apps"]["state"] == "unknown"
    assert report["GET /api/apps"]["mismatches"] == []


def test_doc_path_with_template_matches_endpoint():
    """文档里的具体路径应归一到接口模板路径。"""
    from app.ingest.consistency import path_pattern

    assert path_pattern("/api/procs/{pid}").fullmatch("/api/procs/566")
    assert path_pattern("/api/procs/{pid}/signal").fullmatch("/api/procs/566/signal")
    assert not path_pattern("/api/procs/{pid}").fullmatch("/api/procs/566/signal")


def test_doc_mentions_filter_out_file_paths():
    """文档里的文件路径 / 系统目录不应被当成接口。"""
    from app.ingest.consistency import extract_doc_mentions

    chunks = [
        {
            "id": 1,
            "content": (
                "改 ~/.bash_profile 与 /etc/hosts；\n"
                "依赖 github.com/shirou/gopsutil/v4；\n"
                "调用 `curl http://127.0.0.1:8888/api/health` 验证；\n"
                "接口：GET /api/procs/{pid}/sample"
            ),
            "locator": {"file_path": "docs/api.md"},
        }
    ]
    mentions = extract_doc_mentions(chunks)
    paths = {m["path"] for m in mentions}
    assert "/api/health" in paths
    assert "/api/procs/{pid}/sample" in paths
    assert "/.bash_profile" not in paths
    assert "/etc/hosts" not in paths
