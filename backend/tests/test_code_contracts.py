"""代码即契约：路由归一化、handler 解析、用例→端点匹配。"""
from __future__ import annotations

from app.agent.agents import (
    _code_excerpts,
    _endpoints_for_case,
    _route_key,
)
from app.models import TestCase as CaseModel


def test_route_key_normalizes_case_templates():
    assert _route_key("post", "/admin/backup/{{name}}/delete") == (
        "POST",
        "/admin/backup/{}/delete",
    )
    assert _route_key("POST", "/admin/backup/{name}/delete") == (
        "POST",
        "/admin/backup/{}/delete",
    )


def test_code_excerpts_resolve_handler_body_from_route_line():
    index = [
        ("internal/server/server.go", 1, 10, 'mux.HandleFunc("POST /x", s.doX)'),
        (
            "internal/server/handlers.go",
            20,
            40,
            "func (s *Server) doX(w http.ResponseWriter, r *http.Request) {\n"
            "\twriteJSON(w, 201, map[string]string{\"ok\": \"yes\"})\n}",
        ),
    ]
    endpoint = {
        "method": "POST",
        "path": "/x",
        "responses": {},
        "sources": [{"type": "code", "ref": "internal/server/server.go:1"}],
    }

    excerpts = _code_excerpts(index, [endpoint])

    assert any(item["ref"] == "internal/server/handlers.go:20" for item in excerpts)
    assert any("writeJSON(w, 201" in item["content"] for item in excerpts)


def test_endpoints_for_case_matches_templated_path():
    case = CaseModel(
        project_id=1,
        title="删除备份",
        dimension="functional",
        case_type="api",
        steps=[{"action": "request", "method": "POST", "path": "/admin/backup/{{name}}/delete"}],
        spec={"steps": []},
        status="pending",
    )
    endpoints = [
        {"id": 1, "method": "POST", "path": "/admin/backup/{name}/delete"},
        {"id": 2, "method": "GET", "path": "/admin/backup"},
    ]

    assert [item["id"] for item in _endpoints_for_case(case, endpoints)] == [1]
