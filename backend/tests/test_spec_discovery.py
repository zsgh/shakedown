"""接口文档页（Swagger UI / 自研页面）自动探测 spec 的测试。"""
from __future__ import annotations

import pytest

from app.ingest.openapi import (
    OpenAPIError,
    _extract_spec_url_from_html,
    _page_relative_spec_candidates,
    load_spec,
)

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "paths": {"/api/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
}


@pytest.mark.parametrize(
    "html,expected",
    [
        # Swagger UI 惯例
        (
            '<script>window.onload = function(){ SwaggerUIBundle({url: "/api/openapi.json"}) }</script>',
            "https://h/api/openapi.json",
        ),
        ('const ui = SwaggerUIBundle({ specUrl: "/v3/api-docs" })', "https://h/v3/api-docs"),
        # Redoc
        ('<redoc spec-url="/openapi.json"></redoc>', "https://h/openapi.json"),
        # 自研页面：脚本里 fetch
        (
            'const res = await fetch("/api/openapi.json", { credentials: "same-origin" });',
            "https://h/api/openapi.json",
        ),
        ('fetch("/api/docs.json")', "https://h/api/docs.json"),
        # 相对路径 → 相对文档页所在目录解析
        ('fetch("openapi.json")', "https://h/api/openapi.json"),
    ],
)
def test_spec_url_extracted_from_various_pages(html: str, expected: str):
    assert _extract_spec_url_from_html(html, "https://h/api/docs") == expected


def test_page_relative_candidates():
    candidates = _page_relative_spec_candidates("http://127.0.0.1:8888/api/docs")
    assert "http://127.0.0.1:8888/api/openapi.json" in candidates
    assert "http://127.0.0.1:8888/api/swagger.json" in candidates


def test_load_spec_from_html_page_detects_bundled_spec(monkeypatch):
    """文档页 → 自动找到背后的 spec（用假 HTTP 替代真实网络）。"""
    from app.ingest import openapi as openapi_module

    def fake_fetch(url: str, timeout: float = 20.0, retries: int = 3):
        if url.endswith("/api/docs"):
            return "<html><script>fetch('/api/openapi.json')</script></html>", "text/html"
        if url.endswith("/api/openapi.json"):
            import json

            return json.dumps(SPEC), "application/json"
        raise OpenAPIError(f"HTTP 404 {url}")

    monkeypatch.setattr(openapi_module, "_fetch", fake_fetch)
    spec, _text, origin = load_spec("http://127.0.0.1:8888/api/docs")
    assert origin == "http://127.0.0.1:8888/api/openapi.json"
    assert "/api/x" in spec["paths"]


def test_load_spec_page_without_spec_raises(monkeypatch):
    from app.ingest import openapi as openapi_module

    def fake_fetch(url: str, timeout: float = 20.0, retries: int = 3):
        if url.endswith("/api/docs"):
            return "<html><body>hello</body></html>", "text/html"
        raise OpenAPIError(f"HTTP 404 {url}")

    monkeypatch.setattr(openapi_module, "_fetch", fake_fetch)
    with pytest.raises(OpenAPIError):
        load_spec("http://127.0.0.1:8888/api/docs")
