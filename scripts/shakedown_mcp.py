#!/usr/bin/env python3
"""Shakedown MCP server（stdio）。

让编码 Agent 读取缺陷与契约缺口、拿修复清单、提交用例外部验证、提交修复声明 / 验证结果、创建工单，
并在被测系统文档更新后重新解析知识源、跑一致性检查和回归。
Shakedown 只提供报告与测试，不修改被测系统的文档或代码。
只用标准库 + 项目已有的 httpx，不额外依赖 MCP SDK。

启动：
    SHAKEDOWN_API_URL=http://127.0.0.1:8000 .venv/bin/python scripts/shakedown_mcp.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlencode

import httpx

API_BASE = os.environ.get("SHAKEDOWN_API_URL", "http://127.0.0.1:8000").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_finding_groups",
        "description": "列出项目的缺陷修复组（AI 分诊后的根因分组），可用 verdict/severity/status/source 过滤；verdict=false_positive 可看误报复核清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "verdict": {
                    "type": "string",
                    "enum": ["actionable", "real_bug", "needs_verification", "false_positive", "duplicate", "untriaged", "all"],
                },
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "status": {"type": "string", "enum": ["pending", "confirmed", "false_positive"]},
                "source": {"type": "string", "enum": ["whitebox", "execution"]},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_finding",
        "description": "读取单条原始 finding 的完整证据、代码位置和复现信息。",
        "inputSchema": {
            "type": "object",
            "properties": {"finding_id": {"type": "integer"}},
            "required": ["finding_id"],
        },
    },
    {
        "name": "claim_finding",
        "description": "认领一条缺陷修复工作，状态进入 in_progress，避免多个 Agent 重复处理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer"},
                "assignee": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["finding_id", "assignee"],
        },
    },
    {
        "name": "get_fix_plan",
        "description": "获取项目缺陷修复清单（Markdown，包含证据、复现、验收标准）；verdict=false_positive 可导出误报复核清单。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "verdict": {
                    "type": "string",
                    "enum": ["actionable", "real_bug", "needs_verification", "false_positive", "duplicate", "untriaged", "all"],
                },
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "status": {"type": "string", "enum": ["pending", "confirmed", "false_positive"]},
                "source": {"type": "string", "enum": ["whitebox", "execution"]},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "create_github_issue",
        "description": "把一个缺陷修复组创建为 GitHub Issue（需要先在系统设置配置 GitHub）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "group_key": {"type": "string"},
            },
            "required": ["project_id", "group_key"],
        },
    },
    {
        "name": "create_jira_issue",
        "description": "把一个缺陷修复组创建为 Jira 工单（需要先在系统设置配置 Jira）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "group_key": {"type": "string"},
            },
            "required": ["project_id", "group_key"],
        },
    },
    {
        "name": "list_sources",
        "description": "列出项目知识源和解析状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "get_consistency_report",
        "description": "读取项目 OpenAPI / 代码 / 文档一致性报告。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "include_weak": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "list_case_gaps",
        "description": "列出被测系统的契约缺口（待确认断言 / 无步骤 / 待补全说明）。只读，不会修改被测系统。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["all", "pending", "no_steps", "missing_input"]},
                "include_disabled": {"type": "boolean"},
                "execution_state": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_case_gap_report",
        "description": "获取契约缺口 Markdown 报告，可交给被测系统的编码 Agent。只读，不会修改被测系统。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "include_disabled": {"type": "boolean"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "reparse_source",
        "description": "被测系统文档/OpenAPI 更新后重新解析知识源；只更新 Shakedown 的知识副本，不修改被测系统。",
        "inputSchema": {
            "type": "object",
            "properties": {"source_id": {"type": "integer"}},
            "required": ["source_id"],
        },
    },
    {
        "name": "run_consistency_check",
        "description": "重新计算 OpenAPI / 代码 / 文档三方一致性；建议在 reparse_source 完成后调用。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "request_case_enrich",
        "description": "让 Shakedown 重新评估某条用例的步骤/断言/依据；只改用例资产，不修改被测系统。",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "integer"}},
            "required": ["case_id"],
        },
    },
    {
        "name": "request_case_enrich_batch",
        "description": "按缺口/执行状态批量重新评估用例；默认 dry_run=true 只预览，dry_run=false 才入队。只修改 Shakedown 的用例资产。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "kind": {"type": "string", "enum": ["all", "pending", "questions", "no_steps", "missing_input"]},
                "owner_kind": {"type": "string"},
                "execution_state": {"type": "string"},
                "missing_input_kind": {"type": "string"},
                "dimension": {"type": "string"},
                "feature_id": {"type": "integer"},
                "case_ids": {"type": "array", "items": {"type": "integer"}},
                "include_closed": {"type": "boolean"},
                "include_disabled": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "limit": {"type": "integer"},
                "note": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "set_case_gap_status",
        "description": "处置契约缺口：open / claimed / fixed / accepted / wont_fix；只更新 Shakedown 的缺口状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "case_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "claimed", "fixed", "accepted", "wont_fix"]},
                "owner": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["project_id", "case_id", "status"],
        },
    },
    {
        "name": "set_finding_status",
        "description": "设置缺陷处置：pending / confirmed / false_positive / accepted / wont_fix；可指派负责人并备注。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "confirmed", "false_positive", "accepted", "wont_fix"],
                },
                "note": {"type": "string"},
                "assignee": {"type": "string"},
            },
            "required": ["finding_id", "status"],
        },
    },
    {
        "name": "sync_external_issues",
        "description": "同步 GitHub / Jira 工单状态；只更新展示，不自动判定缺陷已修复。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "list_cases",
        "description": "列出项目用例，可按状态、维度、标题关键词过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "status": {"type": "string"},
                "dimension": {"type": "string"},
                "q": {"type": "string"},
                "execution_state": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "get_case",
        "description": "读取单条用例的完整 spec、步骤、断言和前置条件。",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "integer"}},
            "required": ["case_id"],
        },
    },
    {
        "name": "get_run",
        "description": "读取一次执行的状态和汇总指标。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "integer"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "list_run_results",
        "description": "读取一次执行的逐用例结果，可只看失败/错误并选择带请求响应。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
                "status": {"type": "string"},
                "include_payload": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "start_run",
        "description": "触发一次项目执行（需要 write 权限）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "case_ids": {"type": "array", "items": {"type": "integer"}},
                "dimensions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "rerun_failed",
        "description": "重跑一次执行中的失败 / 错误用例（需要 write 权限）。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "integer"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "report_finding_fix",
        "description": "提交缺陷修复声明；状态进入 claimed_fixed，等待回归验证。至少提供 commit / PR / 测试结果 / 说明之一。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer"},
                "commit_sha": {"type": "string"},
                "pr_url": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
                "test_command": {"type": "string"},
                "test_result": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "verify_finding_fix",
        "description": "提交缺陷修复验证结果；verified=true → verified_fixed，verified=false → reopened。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "integer"},
                "verified": {"type": "boolean"},
                "note": {"type": "string"},
                "test_command": {"type": "string"},
                "test_result": {"type": "string"},
            },
            "required": ["finding_id", "verified"],
        },
    },
    {
        "name": "report_case_verification",
        "description": "提交用例外部验证结果（构建产物 / 进程 / 浏览器 / 权限）；关联待验证 finding 会同步更新。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "integer"},
                "passed": {"type": "boolean"},
                "mode": {
                    "type": "string",
                    "enum": ["api", "process", "artifact", "frontend", "privileged", "manual"],
                },
                "command": {"type": "string"},
                "result": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
                "commit_sha": {"type": "string"},
                "artifact_hashes": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "env_fingerprint": {"type": "object"},
                "expires_at": {"type": "string", "description": "ISO8601 有效期"},
            },
            "required": ["case_id", "passed"],
        },
    },
]


def _request(method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    if params:
        clean = {key: value for key, value in params.items() if value not in (None, "")}
        if clean:
            url += "?" + urlencode(clean)
    with httpx.Client(timeout=60.0) as client:
        response = client.request(method, url, json=body)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or response.json().get("error")
        except ValueError:
            detail = response.text[:500]
        raise RuntimeError(f"Shakedown API {response.status_code}: {detail}")
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    return response.text


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "list_finding_groups":
            project_id = int(args["project_id"])
            data = _request(
                "GET",
                f"/api/projects/{project_id}/findings/export.json",
                params={
                    "verdict": args.get("verdict", "actionable"),
                    "severity": args.get("severity"),
                    "status": args.get("status"),
                    "source": args.get("source"),
                },
            )
            # 只返回摘要，避免把完整证据塞爆上下文；需要详情用 get_fix_plan / get_finding。
            return _tool_result(
                {
                    "summary": data.get("summary"),
                    "groups": [
                        {
                            "group_key": group["id"],
                            "title": group["title"],
                            "verdict": group["verdict"],
                            "priority": group["priority"],
                            "severity": group["severity"],
                            "files": group["files"],
                            "member_ids": group["member_ids"],
                        }
                        for group in data.get("groups", [])
                    ],
                }
            )
        if name == "get_finding":
            finding_id = int(args["finding_id"])
            return _tool_result(_request("GET", f"/api/findings/{finding_id}"))
        if name == "claim_finding":
            finding_id = int(args["finding_id"])
            return _tool_result(
                _request(
                    "POST",
                    f"/api/findings/{finding_id}/claim",
                    body={"assignee": args["assignee"], "note": args.get("note", "")},
                )
            )
        if name == "get_fix_plan":
            project_id = int(args["project_id"])
            text = _request(
                "GET",
                f"/api/projects/{project_id}/findings/export.md",
                params={
                    "verdict": args.get("verdict", "actionable"),
                    "severity": args.get("severity"),
                    "status": args.get("status"),
                    "source": args.get("source"),
                },
            )
            return _tool_result(text)
        if name in {"create_github_issue", "create_jira_issue"}:
            project_id = int(args["project_id"])
            provider = "github" if name == "create_github_issue" else "jira"
            return _tool_result(
                _request(
                    "POST",
                    f"/api/projects/{project_id}/findings/integrations/{provider}",
                    body={"group_key": args["group_key"]},
                )
            )
        if name == "list_sources":
            return _tool_result(_request("GET", f"/api/projects/{int(args['project_id'])}/sources"))
        if name == "get_consistency_report":
            project_id = int(args["project_id"])
            items = _request(
                "GET",
                f"/api/projects/{project_id}/consistency",
                params={"include_weak": bool(args.get("include_weak", False))},
            )
            return _tool_result({"project_id": project_id, "items": items[: int(args.get("limit", 200))]})
        if name == "list_case_gaps":
            project_id = int(args["project_id"])
            return _tool_result(
                _request(
                    "GET",
                    f"/api/projects/{project_id}/case-gaps",
                    params={
                        "kind": args.get("kind", "all"),
                        "include_disabled": bool(args.get("include_disabled", False)),
                        "execution_state": args.get("execution_state"),
                        "limit": int(args.get("limit", 100)),
                    },
                )
            )
        if name == "get_case_gap_report":
            project_id = int(args["project_id"])
            return _tool_result(
                _request(
                    "GET",
                    f"/api/projects/{project_id}/case-gaps/export.md",
                    params={"include_disabled": bool(args.get("include_disabled", False))},
                )
            )
        if name == "reparse_source":
            return _tool_result(_request("POST", f"/api/sources/{int(args['source_id'])}/reparse", body={}))
        if name == "run_consistency_check":
            return _tool_result(
                _request("POST", f"/api/projects/{int(args['project_id'])}/consistency/check", body={})
            )
        if name == "request_case_enrich":
            return _tool_result(_request("POST", f"/api/cases/{int(args['case_id'])}/enrich", body={}))
        if name == "request_case_enrich_batch":
            project_id = int(args["project_id"])
            filters = {
                "kind": args.get("kind", "all"),
                "owner_kind": args.get("owner_kind"),
                "execution_state": args.get("execution_state"),
                "missing_input_kind": args.get("missing_input_kind"),
                "dimension": args.get("dimension"),
                "feature_id": args.get("feature_id"),
                "include_closed": bool(args.get("include_closed", False)),
                "include_disabled": bool(args.get("include_disabled", False)),
            }
            return _tool_result(
                _request(
                    "POST",
                    f"/api/projects/{project_id}/cases/enrich",
                    body={
                        "filters": filters,
                        "case_ids": args.get("case_ids"),
                        "dry_run": bool(args.get("dry_run", True)),
                        "limit": max(1, min(int(args.get("limit", 200)), 200)),
                        "actor": "stdio",
                        "note": args.get("note", ""),
                    },
                )
            )
        if name == "set_case_gap_status":
            project_id = int(args["project_id"])
            case_id = int(args["case_id"])
            return _tool_result(
                _request(
                    "PATCH",
                    f"/api/projects/{project_id}/case-gaps/{case_id}",
                    body={
                        "status": args["status"],
                        "owner": args.get("owner", ""),
                        "note": args.get("note", ""),
                    },
                )
            )
        if name == "set_finding_status":
            finding_id = int(args["finding_id"])
            return _tool_result(
                _request(
                    "PATCH",
                    f"/api/findings/{finding_id}",
                    body={
                        "status": args["status"],
                        "note": args.get("note", ""),
                        "assignee": args.get("assignee"),
                    },
                )
            )
        if name == "sync_external_issues":
            return _tool_result(
                _request(
                    "POST",
                    f"/api/projects/{int(args['project_id'])}/findings/integrations/sync",
                    body={},
                )
            )
        if name == "list_cases":
            project_id = int(args["project_id"])
            return _tool_result(
                _request(
                    "GET",
                    f"/api/projects/{project_id}/cases",
                    params={
                        "status": args.get("status"),
                        "dimension": args.get("dimension"),
                        "q": args.get("q"),
                        "execution_state": args.get("execution_state"),
                        "limit": args.get("limit", 100),
                    },
                )
            )
        if name == "get_case":
            return _tool_result(_request("GET", f"/api/cases/{int(args['case_id'])}"))
        if name == "get_run":
            return _tool_result(_request("GET", f"/api/runs/{int(args['run_id'])}"))
        if name == "list_run_results":
            run_id = int(args["run_id"])
            rows = _request("GET", f"/api/runs/{run_id}/results")
            status = args.get("status")
            if status:
                rows = [row for row in rows if row.get("status") == status]
            limit = max(0, min(int(args.get("limit", 200)), 1000))
            rows = rows[:limit]
            if not args.get("include_payload"):
                for row in rows:
                    row.pop("request", None)
                    row.pop("response", None)
                    row.pop("output", None)
            return _tool_result({"run_id": run_id, "results": rows})
        if name == "start_run":
            project_id = int(args["project_id"])
            body = {
                "case_ids": args.get("case_ids"),
                "dimensions": args.get("dimensions"),
                "trigger": "agent",
            }
            return _tool_result(_request("POST", f"/api/projects/{project_id}/runs", body=body))
        if name == "rerun_failed":
            return _tool_result(_request("POST", f"/api/runs/{int(args['run_id'])}/retry-failed", body={}))
        if name == "report_finding_fix":
            finding_id = int(args["finding_id"])
            body = {
                "commit_sha": args.get("commit_sha", ""),
                "pr_url": args.get("pr_url", ""),
                "files": args.get("files", []),
                "note": args.get("note", ""),
                "test_command": args.get("test_command", ""),
                "test_result": args.get("test_result", ""),
                "actor": "stdio",
            }
            return _tool_result(_request("POST", f"/api/findings/{finding_id}/fix", body=body))
        if name == "verify_finding_fix":
            finding_id = int(args["finding_id"])
            body = {
                "verified": bool(args["verified"]),
                "note": args.get("note", ""),
                "test_command": args.get("test_command", ""),
                "test_result": args.get("test_result", ""),
                "actor": "stdio",
            }
            return _tool_result(
                _request("POST", f"/api/findings/{finding_id}/fix/verify", body=body)
            )
        if name == "report_case_verification":
            case_id = int(args["case_id"])
            body = {
                "passed": bool(args["passed"]),
                "mode": args.get("mode"),
                "command": args.get("command", ""),
                "result": args.get("result", ""),
                "artifacts": args.get("artifacts", []),
                "note": args.get("note", ""),
                "actor": "stdio",
                "commit_sha": args.get("commit_sha", ""),
                "artifact_hashes": args.get("artifact_hashes", {}),
                "env_fingerprint": args.get("env_fingerprint", {}),
                "expires_at": args.get("expires_at", ""),
            }
            return _tool_result(
                _request("POST", f"/api/cases/{case_id}/verification", body=body)
            )
        raise RuntimeError(f"未知工具：{name}")
    except Exception as exc:  # noqa: BLE001 - MCP 工具错误要回传给 Agent
        return _tool_result(f"{type(exc).__name__}: {exc}", is_error=True)


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shakedown", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        result = _call_tool(str(params.get("name") or ""), dict(params.get("arguments") or {}))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = _handle(message)
        except Exception as exc:  # noqa: BLE001
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
