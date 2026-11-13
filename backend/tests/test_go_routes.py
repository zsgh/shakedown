"""Go helper 式路由注册（mymac 的写法）测试。"""
from __future__ import annotations

from pathlib import Path

from app.ingest.code import extract_go_helpers, extract_routes

GO_SOURCE = """
package server

func (s *Server) routes() {
	s.mux.Handle("GET /{$}", s.withGuards(http.HandlerFunc(s.handleIndex)))

	api := func(pattern string, h http.HandlerFunc) {
		s.mux.Handle("GET "+pattern, s.withGuards(h))
		s.mux.Handle("POST "+pattern, s.withGuards(h))
		s.mux.Handle("PUT "+pattern, s.withGuards(h))
		s.mux.Handle("PATCH "+pattern, s.withGuards(h))
		s.mux.Handle("DELETE "+pattern, s.withGuards(h))
	}

	api("/api/health", s.handleHealth)
	api("/api/audit", s.handleAudit)
}
"""


def test_go_helper_definitions_detected():
    helpers = extract_go_helpers(GO_SOURCE)
    assert "api" in helpers
    assert set(helpers["api"]) == {"GET", "POST", "PUT", "PATCH", "DELETE"}


def test_go_helper_call_sites_expanded():
    root = Path("/tmp")
    helpers = extract_go_helpers(GO_SOURCE)
    routes = extract_routes(root / "server.go", GO_SOURCE, root, helpers=helpers)
    keys = {(r.method, r.path) for r in routes}
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert (method, "/api/health") in keys
        assert (method, "/api/audit") in keys
    assert ("GET", "/{$}") in keys


def test_go_single_method_helpers():
    source = """
	g := func(pattern string, h http.HandlerFunc) {
		s.mux.Handle("GET "+pattern, s.withGuards(h))
	}
	p := func(pattern string, h http.HandlerFunc) {
		s.mux.Handle("POST "+pattern, s.withGuards(h))
	}
	g("/api/env/vars", s.handleEnvVars)
	p("/api/env/vars", s.handleEnvCreate)
"""
    root = Path("/tmp")
    helpers = extract_go_helpers(source)
    routes = extract_routes(root / "env.go", source, root, helpers=helpers)
    keys = {(r.method, r.path) for r in routes}
    assert ("GET", "/api/env/vars") in keys
    assert ("POST", "/api/env/vars") in keys
    assert ("PUT", "/api/env/vars") not in keys


def test_routes_without_helpers_still_work():
    root = Path("/tmp")
    routes = extract_routes(root / "server.go", 's.mux.Handle("GET /api/x", h)', root)
    assert {(r.method, r.path) for r in routes} == {("GET", "/api/x")}


def test_go_1_22_handlefunc_routes():
    source = """
func (s *Server) routes() {
	s.mux.HandleFunc("GET /{$}", s.handleIndex)
	s.mux.HandleFunc("POST /admin/backup/{name}/delete", s.deleteBackup)
	http.HandleFunc("/legacy", handler)
}
"""
    root = Path("/tmp")
    routes = extract_routes(root / "server.go", source, root)
    keys = {(r.method, r.path) for r in routes}
    assert ("GET", "/{$}") in keys
    assert ("POST", "/admin/backup/{name}/delete") in keys
    assert ("GET", "/legacy") in keys


def test_go_api_dispatch_resources_detected(tmp_path):
    from app.ingest.code import extract_routes

    content = """package server

func (s *Server) api(w http.ResponseWriter, r *http.Request) {
    p := strings.TrimPrefix(r.URL.Path, "/api/v1")
    switch {
    case seg[0] == "users":
        s.apiUsers(w, r, seg)
    case seg[0] == "backup-targets":
        s.apiBackupTargets(w, r, seg)
    }
}
"""
    path = tmp_path / "api.go"
    path.write_text(content, encoding="utf-8")
    routes = extract_routes(path, content, tmp_path)
    paths = {(route.method, route.path) for route in routes}
    assert ("GET", "/api/v1/users") in paths
    assert ("GET", "/api/v1/backup-targets") in paths
