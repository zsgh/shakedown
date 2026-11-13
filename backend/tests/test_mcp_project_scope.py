"""MCP 客户端项目级访问控制测试。"""
from __future__ import annotations

import pytest

from app.mcp.access import (
    require_project_access,
    reset_current_client,
    set_current_client,
)
from app.mcp.tokens import authenticate_token, create_client


def test_mcp_client_project_allowlist(session, sample_project):
    client, raw = create_client(session, "scoped", ["read", "write"], [sample_project.id])
    session.commit()
    identity = authenticate_token(raw)
    assert identity is not None
    assert identity["project_ids"] == [sample_project.id]
    token = set_current_client(identity)
    try:
        require_project_access(sample_project.id)
        with pytest.raises(PermissionError):
            require_project_access(sample_project.id + 999)
    finally:
        reset_current_client(token)
    session.delete(client)
    session.commit()


def test_mcp_empty_project_scope_means_all(session, sample_project):
    client, raw = create_client(session, "global", ["read"], [])
    session.commit()
    identity = authenticate_token(raw)
    token = set_current_client(identity)
    try:
        require_project_access(sample_project.id)
        require_project_access(sample_project.id + 999)
    finally:
        reset_current_client(token)
    session.delete(client)
    session.commit()


def test_stdio_without_client_is_trusted(session, sample_project):
    """stdio 模式没有 token / current_client，按本机受信任进程处理。"""
    token = set_current_client(None)
    try:
        require_project_access(sample_project.id, minimum_role="owner")
    finally:
        reset_current_client(token)


def test_create_mcp_client_api_returns_project_scope(client, sample_project):
    response = client.post(
        "/api/mcp/clients",
        json={"name": "scoped-api", "scopes": ["read"], "project_ids": [sample_project.id]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["project_ids"] == [sample_project.id]
    assert client.delete(f"/api/mcp/clients/{response.json()['id']}").status_code == 200
