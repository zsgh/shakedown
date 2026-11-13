"""Shakedown 原生 MCP HTTP 入口。

- Streamable HTTP 挂载在 FastAPI 的 ``/mcp/``；
- 必须携带 ``Authorization: Bearer mcp_...``；
- token 只存哈希，权限按 client 的 read / write scope 区分。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.mcp.access import reset_current_client, set_current_client
from app.mcp.tokens import authenticate_token
from app.mcp.tools import (
    audit_a11y_payload,
    automation_health_payload,
    claim_finding_payload,
    compare_visual_baseline_payload,
    create_issue_payload,
    environment_action_payload,
    generate_test_data_payload,
    get_case_gap_report_text,
    get_case_a11y_payload,
    get_case_payload,
    get_gate_payload,
    get_consistency_payload,
    get_finding_payload,
    get_fix_plan_text,
    get_run_payload,
    list_accounts_payload,
    list_audit_payload,
    list_case_gaps_payload,
    list_cases_payload,
    list_exceptions_payload,
    list_finding_groups_payload,
    list_members_payload,
    list_verification_commands_payload,
    list_projects_payload,
    list_sources_payload,
    list_run_results_payload,
    latest_screenshot_payload,
    list_visual_baselines_payload,
    reparse_source_payload,
    report_case_verification_payload,
    report_fix_payload,
    request_case_enrich_batch_payload,
    request_case_enrich_payload,
    rerun_failed_payload,
    run_consistency_check_payload,
    run_performance_payload,
    run_verification_command_payload,
    set_case_gap_status_payload,
    set_finding_status_payload,
    set_visual_baseline_payload,
    start_run_payload,
    sync_external_issues_payload,
    trigger_automation_payload,
    update_exception_payload,
    verify_artifacts_payload,
)
from app.settings_store import effective_settings


def _client_scopes(ctx: Context) -> set[str]:
    headers = ctx.headers
    if not headers:
        # stdio 模式没有 HTTP headers；本地进程默认拥有完整权限。
        return {"read", "write"}
    request = getattr(ctx.request_context, "request", None)
    state = getattr(request, "state", None)
    scopes = getattr(state, "mcp_scopes", None)
    return set(scopes or [])


def _require(ctx: Context, scope: str) -> None:
    if scope not in _client_scopes(ctx):
        raise ToolError(f"MCP client 没有 {scope} 权限")


def _client_identity(ctx: Context) -> str:
    headers = ctx.headers
    if not headers:
        return "stdio"
    request = getattr(ctx.request_context, "request", None)
    state = getattr(request, "state", None)
    client = getattr(state, "mcp_client", None)
    return str((client or {}).get("name") or "mcp")


def _register_tools(server: MCPServer) -> None:
    @server.tool(description="列出可访问的 Shakedown 项目。")
    def list_projects(ctx: Context) -> list[dict[str, Any]]:
        _require(ctx, "read")
        return list_projects_payload()

    @server.tool(description="列出项目测试账号矩阵（账号名 / 字段名，不返回密码和 token）。")
    def list_accounts(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return list_accounts_payload(project_id)

    @server.tool(description="列出项目角色矩阵（owner / maintainer / viewer）。")
    def list_members(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return list_members_payload(project_id)

    @server.tool(description="列出项目知识源（OpenAPI / 代码 / 文档）及解析状态。")
    def list_sources(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return list_sources_payload(project_id)

    @server.tool(description="读取项目 OpenAPI / 代码 / 文档一致性报告；可包含弱信号。")
    def get_consistency_report(
        project_id: int,
        include_weak: bool = False,
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return get_consistency_payload(project_id, include_weak=include_weak, limit=limit)

    @server.tool(
        description=(
            "列出被测系统的契约缺口（待确认断言 / 无步骤 / 待补全说明），"
            "可按执行状态过滤（从未执行 / 已启用无结果 / pending 未启用 / 知识源更新后未复核）。"
            "只读；修复请在被测系统自己的仓库里进行，Shakedown 不会修改被测系统。"
        )
    )
    def list_case_gaps(
        project_id: int,
        kind: str = "all",
        include_disabled: bool = False,
        owner_kind: str = "",
        execution_state: str = "",
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_case_gaps_payload(
            project_id,
            kind=kind,
            include_disabled=include_disabled,
            owner_kind=owner_kind or None,
            execution_state=execution_state or None,
            limit=limit,
        )

    @server.tool(description="查看项目审计时间线：人工反馈、例外、webhook、Agent trace。")
    def list_audit(
        project_id: int,
        kind: str = "",
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_audit_payload(project_id, kind=kind or None, limit=limit)

    @server.tool(description="列出无人值守例外队列（环境 / 低置信度分诊等），可按状态和 kind 过滤。")
    def list_exceptions(
        project_id: int,
        status: str = "",
        kind: str = "",
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_exceptions_payload(
            project_id,
            status=status or None,
            kind=kind or None,
            limit=limit,
        )

    @server.tool(description="处置例外（ack / resolve / ignore），可指派负责人。")
    def update_exception(
        exception_id: int,
        status: str = "",
        owner: str = "",
        note: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return update_exception_payload(
                exception_id,
                status=status or None,
                owner=owner or None,
                note=note if note else None,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="CI gate：把一次 run 收敛成 pass/fail，返回阻断项。")
    def get_automation_gate(
        project_id: int,
        run_id: int | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        try:
            return get_gate_payload(project_id, run_id=run_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="查看无人值守运行健康摘要：最近 run、队列、例外、SLA、执行状态。")
    def get_automation_health(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return automation_health_payload(project_id)

    @server.tool(description="按运行策略触发一次自动化 run（部署 / CI 回调使用）。")
    def trigger_automation_run(
        project_id: int,
        policy: dict[str, Any] | None = None,
        trigger: str = "mcp",
        note: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        return trigger_automation_payload(
            project_id,
            policy=policy,
            trigger=trigger or "mcp",
            note=note,
        )

    @server.tool(description="由 Shakedown 侧读取 artifact_root 下的产物并计算 SHA-256。")
    def verify_artifacts(project_id: int, paths: list[str], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return verify_artifacts_payload(project_id, paths)

    @server.tool(description="启动 / 停止项目配置的被测环境（需要项目显式允许执行命令）。")
    def environment_action(project_id: int, action: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return environment_action_payload(project_id, action)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="列出项目配置的白名单验证命令（进程 / 权限 / 产物）。")
    def list_verification_commands(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return list_verification_commands_payload(project_id)

    @server.tool(description="执行项目配置的白名单验证命令；不接受任意 command。")
    def run_verification_command(
        project_id: int,
        name: str,
        case_id: int | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return run_verification_command_payload(project_id, name, case_id=case_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="读取用例最近一次浏览器 a11y 指标并按基础 WCAG 规则判定。")
    def get_case_a11y(
        project_id: int,
        case_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return get_case_a11y_payload(project_id, case_id)

    @server.tool(description="对一组 a11y 指标执行基础 WCAG 规则审计。")
    def audit_a11y(metrics: dict[str, Any], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return audit_a11y_payload(metrics)

    @server.tool(description="列出视觉基线。")
    def list_visual_baselines(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return list_visual_baselines_payload(project_id)

    @server.tool(description="读取用例最近一次浏览器截图 hash 和路径。")
    def get_latest_screenshot(
        project_id: int,
        case_id: int,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return latest_screenshot_payload(project_id, case_id)

    @server.tool(description="把当前截图 hash 设为视觉基线。")
    def set_visual_baseline(
        project_id: int,
        case_id: int,
        name: str = "default",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        return set_visual_baseline_payload(project_id, case_id, name=name)

    @server.tool(description="比较当前截图 hash 与视觉基线。")
    def compare_visual_baseline(
        project_id: int,
        case_id: int,
        name: str = "default",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return compare_visual_baseline_payload(project_id, case_id, name=name)

    @server.tool(description="按字段语义生成测试数据（email / amount / status / unicode 等）。")
    def generate_test_data(
        project_id: int,
        endpoint_id: int | None = None,
        fields: list[dict[str, Any]] | None = None,
        count: int = 1,
        seed: int | None = None,
        unicode: bool = False,
        distribution: str = "uniform",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        return generate_test_data_payload(
            project_id,
            endpoint_id=endpoint_id,
            fields=fields,
            count=count,
            seed=seed,
            unicode=unicode,
            distribution=distribution,
        )

    @server.tool(description="执行轻量 load / stress / soak 性能采样，返回 RPS 和 P50/P95/P99。")
    def run_performance(
        project_id: int,
        method: str,
        path: str,
        concurrency: int = 5,
        iterations: int = 20,
        duration_seconds: float | None = None,
        headers: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float = 10.0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        return run_performance_payload(
            project_id,
            method=method,
            path=path,
            concurrency=concurrency,
            iterations=iterations,
            duration_seconds=duration_seconds,
            headers=headers,
            query=query,
            body=body,
            timeout=timeout,
        )

    @server.tool(
        description=(
            "获取被测系统的契约缺口 Markdown 报告，可直接交给该系统的编码 Agent。"
            "只读；报告不会自动修改被测系统。"
        )
    )
    def get_case_gap_report(
        project_id: int,
        include_disabled: bool = False,
        owner_kind: str = "sut-contract",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        _require(ctx, "read")
        return get_case_gap_report_text(
            project_id, include_disabled=include_disabled, owner_kind=owner_kind
        )

    @server.tool(
        description=(
            "被测系统文档/OpenAPI 更新后，重新解析指定知识源（需要 write 权限）。"
            "只更新 Shakedown 的知识副本，不修改被测系统文件。"
        )
    )
    def reparse_source(source_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return reparse_source_payload(source_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "重新计算 OpenAPI / 代码 / 文档三方一致性（需要 write 权限）。"
            "建议在 reparse_source 解析完成后再调用。"
        )
    )
    def run_consistency_check(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return run_consistency_check_payload(project_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "让 Shakedown 重新评估某条用例的步骤/断言/依据（需要 write 权限）。"
            "只修改 Shakedown 的用例资产，不修改被测系统。"
        )
    )
    def request_case_enrich(case_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return request_case_enrich_payload(case_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "批量重新评估缺口用例（需要 write 权限）。默认 dry_run=true 只预览；"
            "传 dry_run=false 才会入队。只修改 Shakedown 的用例资产。"
        )
    )
    def request_case_enrich_batch(
        project_id: int,
        kind: str = "all",
        owner_kind: str = "",
        execution_state: str = "",
        missing_input_kind: str = "",
        dimension: str = "",
        feature_id: int | None = None,
        case_ids: list[int] | None = None,
        include_closed: bool = False,
        include_disabled: bool = False,
        dry_run: bool = True,
        limit: int = 200,
        note: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return request_case_enrich_batch_payload(
                project_id,
                kind=kind,
                owner_kind=owner_kind or None,
                execution_state=execution_state or None,
                missing_input_kind=missing_input_kind or None,
                dimension=dimension or None,
                feature_id=feature_id,
                case_ids=case_ids,
                include_closed=include_closed,
                include_disabled=include_disabled,
                dry_run=dry_run,
                limit=limit,
                actor=_client_identity(ctx),
                note=note,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "处置一个契约缺口（需要 write 权限）：认领 / 标记已修复 / 接受风险 / 不修复 / 重新打开。"
            "只更新 Shakedown 的缺口状态，不修改被测系统。"
        )
    )
    def set_case_gap_status(
        project_id: int,
        case_id: int,
        status: str,
        owner: str = "",
        note: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return set_case_gap_status_payload(
                project_id,
                case_id,
                status=status,
                owner=owner,
                note=note,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "设置缺陷处置状态（需要 write 权限）：pending / confirmed / false_positive / accepted / wont_fix。"
            "可同时指派负责人和填写备注。"
        )
    )
    def set_finding_status(
        finding_id: int,
        status: str,
        note: str = "",
        assignee: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return set_finding_status_payload(
                finding_id,
                status=status,
                note=note,
                assignee=assignee,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "同步 GitHub / Jira 工单状态（需要 write 权限）。"
            "只同步工单状态展示，不自动替 Shakedown 判定缺陷已修复。"
        )
    )
    def sync_external_issues(project_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return sync_external_issues_payload(project_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="列出项目用例，可按状态、维度、标题关键词、执行状态过滤。")
    def list_cases(
        project_id: int,
        status: str | None = None,
        dimension: str | None = None,
        q: str | None = None,
        execution_state: str | None = None,
        limit: int = 100,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_cases_payload(
            project_id,
            status=status,
            dimension=dimension,
            q=q,
            execution_state=execution_state,
            limit=limit,
        )

    @server.tool(description="读取单条用例的完整 spec、步骤、断言和前置条件。")
    def get_case(case_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return get_case_payload(case_id)

    @server.tool(description="读取一次执行的状态和汇总指标。")
    def get_run(run_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return get_run_payload(run_id)

    @server.tool(description="读取一次执行的逐用例结果；可只看 fail/error，并可选择带请求/响应。")
    def list_run_results(
        run_id: int,
        status: str | None = None,
        include_payload: bool = False,
        limit: int = 200,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_run_results_payload(
            run_id,
            status=status,
            include_payload=include_payload,
            limit=limit,
        )

    @server.tool(description="触发一次项目执行（需要 write 权限）。")
    def start_run(
        project_id: int,
        case_ids: list[int] | None = None,
        dimensions: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        return start_run_payload(project_id, case_ids=case_ids, dimensions=dimensions)

    @server.tool(description="重跑一次执行中的失败 / 错误用例（需要 write 权限）。")
    def rerun_failed(run_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        try:
            return rerun_failed_payload(run_id)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(str(exc)) from exc

    @server.tool(description="列出 AI 分诊后的缺陷修复组；verdict=false_positive 可取误报复核清单。")
    def list_finding_groups(
        project_id: int,
        verdict: str = "actionable",
        severity: str | None = None,
        status: str | None = None,
        source: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "read")
        return list_finding_groups_payload(
            project_id,
            verdict=verdict,
            severity=severity,
            status=status,
            source=source,
        )

    @server.tool(description="读取单条缺陷的完整证据、代码位置和复现信息。")
    def get_finding(finding_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "read")
        return get_finding_payload(finding_id)

    @server.tool(
        description=(
            "认领一条缺陷的修复工作（需要 write 权限），状态进入 in_progress。"
            "用于多个编码 Agent 并发时避免重复处理；认领后在被测系统自己的仓库里修复。"
        )
    )
    def claim_finding(
        finding_id: int,
        assignee: str,
        note: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return claim_finding_payload(finding_id, assignee=assignee, note=note)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description="获取项目缺陷修复清单 Markdown；verdict=false_positive 可导出误报复核清单。")
    def get_fix_plan(
        project_id: int,
        verdict: str = "actionable",
        severity: str | None = None,
        status: str | None = None,
        source: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        _require(ctx, "read")
        return get_fix_plan_text(
            project_id,
            verdict=verdict,
            severity=severity,
            status=status,
            source=source,
        )

    @server.tool(description="把一个缺陷修复组创建为 GitHub Issue（需要 write 权限）。")
    def create_github_issue(project_id: int, group_key: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        return create_issue_payload(project_id, "github", group_key)

    @server.tool(description="把一个缺陷修复组创建为 Jira 工单（需要 write 权限）。")
    def create_jira_issue(project_id: int, group_key: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        _require(ctx, "write")
        return create_issue_payload(project_id, "jira", group_key)

    @server.tool(
        description=(
            "提交缺陷修复声明（需要 write 权限）。状态会进入 claimed_fixed，"
            "不会直接变成已修复；至少提交 commit / PR / 测试结果 / 说明之一。"
        )
    )
    def report_finding_fix(
        finding_id: int,
        commit_sha: str = "",
        pr_url: str = "",
        files: list[str] | None = None,
        note: str = "",
        test_command: str = "",
        test_result: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return report_fix_payload(
                finding_id,
                commit_sha=commit_sha,
                pr_url=pr_url,
                files=files or [],
                note=note,
                test_command=test_command,
                test_result=test_result,
                actor=_client_identity(ctx),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        description=(
            "提交用例的外部验证结果（需要 write 权限）。适用于构建产物、进程、"
            "浏览器、权限等非 API 验证；只记录 reported_passed / reported_failed，"
            "不会修改 case 或 finding 的验证状态。"
        )
    )
    def report_case_verification(
        case_id: int,
        passed: bool,
        mode: str | None = None,
        command: str = "",
        result: str = "",
        artifacts: list[str] | None = None,
        note: str = "",
        commit_sha: str = "",
        artifact_hashes: dict[str, str] | None = None,
        env_fingerprint: dict[str, Any] | None = None,
        expires_at: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        _require(ctx, "write")
        try:
            return report_case_verification_payload(
                case_id,
                passed=passed,
                mode=mode,
                command=command,
                result=result,
                artifacts=artifacts or [],
                note=note,
                commit_sha=commit_sha,
                artifact_hashes=artifact_hashes or {},
                env_fingerprint=env_fingerprint or {},
                expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
                actor=_client_identity(ctx),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

class _McpAuthApp:
    """ASGI 外壳：校验 Bearer token，并把 scope 传给工具调用。"""

    def __init__(self, app: Any):
        self.app = app
        self.inner = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        settings = effective_settings()
        if not settings.mcp_require_auth:
            client = {"name": "stdio", "scopes": ["read", "write"], "project_ids": []}
            scope.setdefault("state", {})["mcp_scopes"] = client["scopes"]
            scope["state"]["mcp_client"] = client
            token = set_current_client(client)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_current_client(token)
            return
        raw = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                raw = value.decode("latin-1")
                break
        token = raw.removeprefix("Bearer ").removeprefix("bearer ").strip()
        client = await asyncio.to_thread(authenticate_token, token) if token else None
        if client is None:
            response = JSONResponse(
                {"error": {"code": "mcp_unauthorized", "message": "缺少或无效的 MCP Bearer Token"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["mcp_scopes"] = list(client.get("scopes") or [])
        state["mcp_client"] = client
        context_token = set_current_client(client)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_client(context_token)


def build_mcp_app():
    """构建挂载到 FastAPI 的 Streamable HTTP MCP ASGI app。"""
    settings = effective_settings()
    server = MCPServer(
        name="shakedown",
        title="Shakedown MCP",
        version="0.1.0",
        instructions=(
            "Shakedown 测试协作 MCP。先用 list_projects 找项目。"
            "缺陷修复用 list_finding_groups / get_fix_plan / claim_finding；"
            "契约缺口用 list_case_gaps / get_case_gap_report。"
            "缺口处置用 set_case_gap_status；缺陷处置用 set_finding_status。"
            "修复被测系统请在其自己的仓库里进行；Shakedown 只读报告、不改被测系统。"
            "被测系统文档更新后可调用 reparse_source / run_consistency_check / start_run 做回归。"
            "创建外部工单、触发写操作或处置状态需要 write scope。"
        ),
    )
    _register_tools(server)
    allowed_hosts = [
        item.strip()
        for item in (settings.mcp_allowed_hosts or "127.0.0.1,localhost").split(",")
        if item.strip()
    ]
    inner = server.streamable_http_app(
        streamable_http_path="/",
        json_response=False,
        stateless_http=False,
        host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=[],
        ),
    )
    return _McpAuthApp(inner)
