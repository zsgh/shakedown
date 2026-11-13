"""攻击用例包：模式适用、幂等生成、依赖扫描。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.crypto import encrypt_json
from app.models import Endpoint, Finding, KnowledgeSource, Project
from app.models import TestCase as CaseModel
from app.security.deps import DependencyReport, discover_dependencies, scan_dependencies
from app.security.generator import generate_cases, scan_project
from app.security.registry import FindingDraft


def _project(session, tmp_path: Path) -> Project:
    project = Project(
        name="attack-pack",
        base_url="http://127.0.0.1:18088",
        test_accounts_encrypted=encrypt_json(
            {
                "admin": {"username": "admin", "password": "AdminPass1", "token": "dmg_admin"},
                "viewer": {"username": "viewer", "password": "ViewerPass1", "token": "dmg_viewer"},
            }
        ),
    )
    session.add(project)
    session.flush()
    endpoints = [
        ("POST", "/login"),
        ("GET", "/admin"),
        ("GET", "/admin/backup"),
        ("POST", "/admin/users"),
        ("POST", "/admin/users/{name}/password"),
        ("GET", "/admin/backup/{name}"),
        ("GET", "/api/v1/me"),
        ("POST", "/new"),
        ("GET", "/p/{ns}/{name}"),
        ("POST", "/p/{ns}/{name}/delete"),
    ]
    for method, path in endpoints:
        session.add(Endpoint(project_id=project.id, method=method, path=path, summary=""))
    (tmp_path / "go.mod").write_text("module demo\n\ngo 1.22\n", encoding="utf-8")
    session.add(KnowledgeSource(project_id=project.id, kind="code", location=str(tmp_path)))
    session.flush()
    return project


def test_scan_builds_attack_pack(session, tmp_path):
    project = _project(session, tmp_path)
    report = scan_project(session, project, include_findings=False)
    ids = {row["id"] for row in report["patterns"]}
    assert {
        "open-redirect",
        "session-revocation",
        "authz-matrix",
        "csrf",
        "path-traversal",
        "dependency-vulns",
        "idor-project",
        "duplicate-submit",
        "race-condition",
    } <= ids
    keys = {row["attack_key"] for row in report["cases"]}
    assert "open-redirect:/login" in keys
    assert "session-revocation:/admin/users/{name}/password" in keys
    assert "csrf:/admin/users" in keys
    assert "path-traversal:/admin/backup/{name}" in keys
    assert "idor-project:viewer" in keys
    assert "duplicate-submit:new" in keys
    assert "race-condition:new" in keys
    reasons = {row["id"]: row["reason"] for row in report["patterns"]}
    assert "没有 URL" in reasons["ssrf"] or "没有文件上传" in reasons["upload-filename"]
    # 7 个接口下：2 个管理页 × 2 种身份 + /me 匿名 = 5 条越权用例
    assert sum(1 for row in report["cases"] if row["pattern_id"] == "authz-matrix") == 5


def test_generate_is_idempotent(session, tmp_path):
    project = _project(session, tmp_path)
    first = generate_cases(session, project)
    assert first["created_case_ids"]
    created = session.scalars(select(CaseModel).where(CaseModel.project_id == project.id)).all()
    assert len(created) == len(first["created_case_ids"])
    assert all("attack-pack" in (case.tags or []) for case in created)
    assert all((case.spec or {}).get("attack", {}).get("key") for case in created)

    second = generate_cases(session, project)
    assert second["created_case_ids"] == []
    assert second["summary"]["new_cases"] == 0
    again = scan_project(session, project, include_findings=False)
    assert all(row["existing_case_id"] is not None for row in again["cases"])


def test_dependency_finding_is_created(session, tmp_path, monkeypatch):
    project = _project(session, tmp_path)

    def fake_scan(ctx):
        return DependencyReport(
            dependencies=[],
            findings=[
                FindingDraft(
                    key="osv:npm:lodash:4.17.20",
                    title="[供应链] lodash@4.17.20 命中 1 条已知漏洞",
                    description="CVE-2021-23337",
                    severity="high",
                    evidence=[{"type": "dependency", "detail": "npm:lodash@4.17.20"}],
                )
            ],
            errors=[],
            manifests=["package.json"],
            toolchains=[],
        )

    monkeypatch.setattr("app.security.patterns.scan_dependencies", fake_scan)
    result = generate_cases(session, project)
    assert result["created_finding_ids"]
    finding = session.scalar(
        select(Finding).where(Finding.project_id == project.id, Finding.cluster_key == "attack-pack:osv:npm:lodash:4.17.20")
    )
    assert finding is not None
    assert finding.status == "confirmed"
    assert finding.severity == "high"


def test_dependency_manifest_parsing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text(
        "module demo\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.0\n)\n",
        encoding="utf-8",
    )
    (repo / "package.json").write_text('{"dependencies": {"lodash": "^4.17.20"}}', encoding="utf-8")
    (repo / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "packages": {"node_modules/lodash": {"version": "4.17.20"}}}',
        encoding="utf-8",
    )
    deps, manifests, toolchains = discover_dependencies([repo])
    names = {(dep.ecosystem, dep.name, dep.version) for dep in deps}
    assert ("Go", "github.com/gin-gonic/gin", "1.9.0") in names
    assert ("npm", "lodash", "4.17.20") in names
    assert len(manifests) == 2
    assert toolchains and toolchains[0]["version"] == "1.22"


def test_dependency_scan_reports_eol_and_osv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text(
        "module demo\n\ngo 1.22\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.0\n)\n",
        encoding="utf-8",
    )

    def fake_query(deps):
        return {0: [{"id": "GO-2024-0001", "summary": "demo vuln", "severity": "high", "fixed": ["1.9.1"], "aliases": []}]}, []

    monkeypatch.setattr("app.security.deps.query_vulnerabilities", fake_query)
    report = scan_dependencies(type("Ctx", (), {"code_roots": [repo], "scratch": {}})())
    titles = [item.title for item in report.findings]
    assert any("gin@1.9.0" in title for title in titles)
    assert any("Go 1.22" in title and "停止安全维护" in title for title in titles)


def test_parallel_step_schema_is_valid():
    from app.executor.case_schema import validate_case_spec

    spec = {
        "title": "并发创建同名项目",
        "dimension": "security",
        "type": "api",
        "steps": [
            {
                "action": "parallel",
                "parallel": {
                    "count": 3,
                    "request": {"method": "POST", "path": "/new", "body": {"name": "x"}},
                },
                "assert": [
                    {
                        "kind": "json_path",
                        "target": "$.statuses",
                        "operator": "eq",
                        "expected": [200, 200, 303],
                    }
                ],
            }
        ],
        "assertions": [],
    }
    assert validate_case_spec(spec) == []
