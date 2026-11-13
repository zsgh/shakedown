"""项目 RBAC / 租户边界：MCP 角色、token 租户、API 可选 actor 头。"""
from __future__ import annotations

import pytest

from app.mcp.access import require_project_access, reset_current_client, set_current_client
from app.mcp.tokens import authenticate_token, create_client
from app.mcp.tools import list_projects_payload
from app.models import ProjectMember
from app.models import TestCase as CaseModel


def _client(project_id: int, actor: str, *, tenant: str = "", bound: bool = True) -> dict:
    return {
        "name": actor,
        "actor": actor if bound else "",
        "scopes": ["read", "write"],
        "project_ids": [project_id],
        "tenant": tenant,
    }


def test_mcp_viewer_cannot_write(session, sample_project):
    session.add(ProjectMember(project_id=sample_project.id, actor="viewer-bot", role="viewer"))
    session.commit()
    client = _client(sample_project.id, "viewer-bot")

    require_project_access(sample_project.id, client)  # read 允许
    with pytest.raises(PermissionError, match="maintainer"):
        require_project_access(sample_project.id, client, minimum_role="maintainer")


def test_mcp_owner_and_inactive_member(session, sample_project):
    session.add(ProjectMember(project_id=sample_project.id, actor="owner-bot", role="owner"))
    session.add(
        ProjectMember(project_id=sample_project.id, actor="gone-bot", role="maintainer", active=False)
    )
    session.commit()

    require_project_access(sample_project.id, _client(sample_project.id, "owner-bot"), minimum_role="owner")
    with pytest.raises(PermissionError, match="停用"):
        require_project_access(sample_project.id, _client(sample_project.id, "gone-bot"))


def test_mcp_explicit_actor_without_member_is_denied(session, sample_project):
    with pytest.raises(PermissionError, match="成员不存在"):
        require_project_access(sample_project.id, _client(sample_project.id, "ghost-bot"))


def test_mcp_tenant_and_project_allowlist(session, sample_project):
    sample_project.tenant = "tenant-a"
    session.commit()

    require_project_access(sample_project.id, _client(sample_project.id, "bot", tenant="tenant-a", bound=False))
    with pytest.raises(PermissionError, match="租户"):
        require_project_access(sample_project.id, _client(sample_project.id, "bot", tenant="tenant-b", bound=False))

    other = _client(sample_project.id, "bot", bound=False)
    other["project_ids"] = [sample_project.id + 999]
    with pytest.raises(PermissionError, match="无权访问"):
        require_project_access(sample_project.id, other)


def test_token_carries_tenant_and_actor(session):
    client, raw = create_client(
        session,
        "ci-bot",
        ["read", "write"],
        tenant="tenant-a",
        actor="qa-lead",
    )
    session.commit()
    identity = authenticate_token(raw)
    assert identity is not None
    assert identity["tenant"] == "tenant-a"
    assert identity["actor"] == "qa-lead"
    session.delete(session.get(type(client), client.id))
    session.commit()


def test_api_actor_header_enforces_role_and_tenant(client, session, sample_project):
    sample_project.tenant = "tenant-a"
    session.add_all(
        [
            ProjectMember(project_id=sample_project.id, actor="viewer", role="viewer"),
            ProjectMember(project_id=sample_project.id, actor="maint", role="maintainer"),
        ]
    )
    session.commit()

    # 不传 header：保持兼容，旧调用不受影响。
    assert client.get(f"/api/projects/{sample_project.id}").status_code == 200

    viewer_headers = {"X-Shakedown-Actor": "viewer", "X-Shakedown-Tenant": "tenant-a"}
    assert client.get(f"/api/projects/{sample_project.id}", headers=viewer_headers).status_code == 200
    denied = client.post(
        f"/api/projects/{sample_project.id}/schedules",
        json={"name": "nightly", "interval_seconds": 3600, "policy": {}},
        headers=viewer_headers,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"

    maint_headers = {"X-Shakedown-Actor": "maint", "X-Shakedown-Tenant": "tenant-a"}
    created = client.post(
        f"/api/projects/{sample_project.id}/schedules",
        json={"name": "nightly", "interval_seconds": 3600, "policy": {}},
        headers=maint_headers,
    )
    assert created.status_code == 201, created.text

    wrong_tenant = client.get(
        f"/api/projects/{sample_project.id}",
        headers={"X-Shakedown-Actor": "viewer", "X-Shakedown-Tenant": "tenant-b"},
    )
    assert wrong_tenant.status_code == 403

    unknown_actor = client.get(
        f"/api/projects/{sample_project.id}",
        headers={"X-Shakedown-Actor": "stranger", "X-Shakedown-Tenant": "tenant-a"},
    )
    assert unknown_actor.status_code == 403

    listed = client.get("/api/projects", headers={"X-Shakedown-Tenant": "tenant-a"}).json()
    assert sample_project.id in {item["id"] for item in listed}
    assert all(item.get("tenant") == "tenant-a" for item in listed)

    scoped = client.get(
        "/api/projects",
        headers={"X-Shakedown-Actor": "viewer", "X-Shakedown-Tenant": "tenant-a"},
    ).json()
    assert {item["id"] for item in scoped} == {sample_project.id}

    case = CaseModel(
        project_id=sample_project.id,
        title="角色校验用例",
        dimension="functional",
        case_type="api",
        status="enabled",
        steps=[],
        spec={},
    )
    session.add(case)
    session.commit()
    case_read = client.get(f"/api/cases/{case.id}", headers=viewer_headers)
    assert case_read.status_code == 200
    case_write = client.patch(f"/api/cases/{case.id}", json={"title": "viewer 改不了"}, headers=viewer_headers)
    assert case_write.status_code == 403
    case_write_ok = client.patch(f"/api/cases/{case.id}", json={"title": "maintainer 可以"}, headers=maint_headers)
    assert case_write_ok.status_code == 200, case_write_ok.text


def test_mcp_project_list_is_tenant_scoped(session, sample_project):
    from app.models import Project

    sample_project.tenant = "tenant-a"
    other = Project(name="other-tenant", tenant="tenant-b", settings_json={})
    session.add(other)
    session.commit()

    token = set_current_client(
        {"name": "bot", "actor": "bot", "scopes": ["read"], "project_ids": [], "tenant": "tenant-a"}
    )
    try:
        ids = {item["id"] for item in list_projects_payload()}
    finally:
        reset_current_client(token)
    assert sample_project.id in ids
    assert other.id not in ids
