"""原生 Streamable HTTP MCP：认证、工具发现和只读权限。"""
from __future__ import annotations

import json


def _sse_payload(response) -> dict:
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no SSE data: {response.text[:500]}")


def _initialize(client, token: str) -> tuple[str, dict]:
    response = client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.headers["mcp-session-id"]
    return session_id, _sse_payload(response)


def _tools_call(client, token: str, session_id: str, name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
        },
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return _sse_payload(response)


def test_mcp_http_auth_tools_and_readonly_scope(client):
    unauthorized = client.post(
        "/mcp/",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert unauthorized.status_code == 401

    created = client.post(
        "/api/mcp/clients",
        json={"name": "pytest-reader", "scopes": ["read"]},
    )
    assert created.status_code == 201
    token = created.json()["token"]
    client_id = created.json()["id"]
    assert token.startswith("mcp_")

    session_id, initialized = _initialize(client, token)
    assert initialized["result"]["serverInfo"]["name"] == "shakedown"

    response = client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
        },
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200
    tools = _sse_payload(response)["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {
        "list_projects",
        "list_sources",
        "get_consistency_report",
        "list_case_gaps",
        "get_case_gap_report",
        "list_cases",
        "get_case",
        "get_run",
        "list_run_results",
        "start_run",
        "rerun_failed",
        "reparse_source",
        "run_consistency_check",
        "request_case_enrich",
        "request_case_enrich_batch",
        "set_case_gap_status",
        "set_finding_status",
        "sync_external_issues",
        "list_finding_groups",
        "get_finding",
        "claim_finding",
        "get_fix_plan",
        "create_github_issue",
        "create_jira_issue",
    }

    project = client.post("/api/projects", json={"name": "mcp-read", "base_url": "http://127.0.0.1:1"}).json()
    read_result = _tools_call(client, token, session_id, "list_cases", {"project_id": project["id"], "limit": 1})
    assert read_result["result"]["isError"] is False

    gaps_result = _tools_call(client, token, session_id, "list_case_gaps", {"project_id": project["id"]})
    assert gaps_result["result"]["isError"] is False
    gap_report = _tools_call(client, token, session_id, "get_case_gap_report", {"project_id": project["id"]})
    assert gap_report["result"]["isError"] is False

    enrich_denied = _tools_call(client, token, session_id, "request_case_enrich", {"case_id": 1})
    assert enrich_denied["result"]["isError"] is True
    assert "write" in enrich_denied["result"]["content"][0]["text"]

    result = _tools_call(
        client,
        token,
        session_id,
        "create_github_issue",
        {"project_id": 1, "group_key": "any"},
    )
    assert result["result"]["isError"] is True
    assert "write" in result["result"]["content"][0]["text"]

    deleted = client.delete(f"/api/mcp/clients/{client_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get("/api/mcp/clients").json() == []
