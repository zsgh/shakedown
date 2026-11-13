"""HTML 报告导出（自包含单文件，无外部依赖）。"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; padding: 24px; background: #f6f7f9; color: #1f2328; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 24px 0 10px; }
.sub { color: #656d76; font-size: 13px; margin-bottom: 16px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 12px 16px; min-width: 130px; }
.card .num { font-size: 22px; font-weight: 600; }
.card .label { color: #656d76; font-size: 12px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px; }
th, td { border: 1px solid #d0d7de; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f0f2f5; }
pre { background: #f6f8fa; padding: 8px; overflow-x: auto; font-size: 12px; border-radius: 6px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; border: 1px solid #d0d7de; }
.pass { background: #dafbe1; border-color: #aceebb; }
.fail { background: #ffebe9; border-color: #ffcecb; }
.flaky { background: #fff8c5; border-color: #f3e5a3; }
.error { background: #ffebe9; border-color: #ffcecb; }
.skipped, .pending { background: #eaeef2; }
.defect { background: #ffebe9; border-color: #ff8182; font-weight: 600; }
.muted { color: #656d76; }
details { margin: 4px 0; }
summary { cursor: pointer; }
"""


def _badge(text: str) -> str:
    return f'<span class="badge {html.escape(str(text))}">{html.escape(str(text))}</span>'


def render_run_report(data: dict[str, Any]) -> str:
    """把一次执行的数据渲染成自包含 HTML。"""
    project = data.get("project") or {}
    run = data.get("run") or {}
    results = data.get("results") or []
    findings = data.get("findings") or []

    cards = "".join(
        f'<div class="card"><div class="num">{html.escape(str(value))}</div>'
        f'<div class="label">{html.escape(str(label))}</div></div>'
        for label, value in (
            ("总用例", len(results)),
            ("通过", sum(1 for r in results if r.get("status") == "pass")),
            ("失败", sum(1 for r in results if r.get("status") == "fail")),
            ("flaky", sum(1 for r in results if r.get("status") == "flaky")),
            ("异常", sum(1 for r in results if r.get("status") == "error")),
            ("疑似缺陷", len(findings)),
        )
    )

    rows: list[str] = []
    for result in results:
        attribution = result.get("attribution") or {}
        detail = ""
        if result.get("status") in {"fail", "error"}:
            request_payload = json.dumps(result.get("request") or {}, ensure_ascii=False, indent=2)
            response_payload = json.dumps(result.get("response") or {}, ensure_ascii=False, indent=2)
            detail = (
                f"<details><summary>请求 / 响应 / 日志</summary>"
                f"<pre>{html.escape(request_payload)}</pre>"
                f"<pre>{html.escape(response_payload)}</pre>"
                f"<pre>{html.escape(str(result.get('logs') or ''))}</pre>"
                f"</details>"
            )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(result.get('case_id')))}</td>"
            f"<td>{html.escape(str(result.get('title') or ''))}</td>"
            f"<td>{_badge(result.get('status', ''))}</td>"
            f"<td>{html.escape(str(result.get('dimension') or ''))}</td>"
            f"<td>{html.escape(str(attribution.get('category') or ''))}</td>"
            f"<td>{html.escape(str(attribution.get('reason') or ''))}<br>{detail}</td>"
            "</tr>"
        )

    finding_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(f.get('title')))}</td>"
        f"<td>{html.escape(str(f.get('source')))}</td>"
        f"<td>{_badge(f.get('severity', ''))}</td>"
        f"<td>{html.escape(str(f.get('status')))}</td>"
        f"<td><pre>{html.escape(str(f.get('repro') or ''))}</pre></td>"
        "</tr>"
        for f in findings
    )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Shakedown 执行报告 · {html.escape(str(project.get('name', '')))}</title>
<style>{_CSS}</style></head>
<body>
<h1>Shakedown 执行报告 · {html.escape(str(project.get('name', '')))}</h1>
<div class="sub">运行 #{html.escape(str(run.get('id')))} · 状态 {html.escape(str(run.get('status')))} ·
 开始 {html.escape(str(run.get('started_at') or ''))} · 结束 {html.escape(str(run.get('finished_at') or ''))} ·
 导出时间 {generated}</div>
<div class="cards">{cards}</div>
<h2>执行结果</h2>
<table><thead><tr><th>#</th><th>用例</th><th>结果</th><th>维度</th><th>归因</th><th>详情</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="6">无结果</td></tr>'}</tbody></table>
<h2>疑似缺陷</h2>
<table><thead><tr><th>标题</th><th>来源</th><th>级别</th><th>状态</th><th>复现命令</th></tr></thead>
<tbody>{finding_rows or '<tr><td colspan="5">无</td></tr>'}</tbody></table>
</body></html>
"""


def render_project_report(data: dict[str, Any]) -> str:
    """项目级看板导出。"""
    project = data.get("project") or {}
    overview = data.get("overview") or {}
    coverage = overview.get("coverage") or {}
    modules = coverage.get("modules") or []

    module_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(m.get('module')))}</td>"
        f"<td>{m.get('total', 0)}</td>"
        f"<td>{m.get('covered', 0)}</td>"
        f"<td>{'✅' if m.get('covered', 0) == m.get('total', 0) else '⚠️'}</td>"
        "</tr>"
        for m in modules
    )
    runs = overview.get("runs") or []
    run_rows = "".join(
        "<tr>"
        f"<td>#{html.escape(str(r.get('id')))}</td>"
        f"<td>{html.escape(str(r.get('status')))}</td>"
        f"<td>{r.get('total', 0)}</td>"
        f"<td>{r.get('passed', 0)}</td>"
        f"<td>{r.get('failed', 0)}</td>"
        f"<td>{r.get('flaky', 0)}</td>"
        f"<td>{html.escape(str(r.get('created_at') or ''))}</td>"
        "</tr>"
        for r in runs
    )
    dimension_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(k))}</td>"
        f"<td>{v.get('total', 0)}</td>"
        f"<td>{v.get('enabled', 0)}</td>"
        f"<td>{v.get('passed', 0)}</td>"
        f"<td>{v.get('failed', 0)}</td>"
        "</tr>"
        for k, v in (overview.get("dimensions") or {}).items()
    )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Shakedown 项目报告 · {html.escape(str(project.get('name', '')))}</title>
<style>{_CSS}</style></head>
<body>
<h1>Shakedown 项目报告 · {html.escape(str(project.get('name', '')))}</h1>
<div class="sub">导出时间 {generated}</div>
<div class="cards">
<div class="card"><div class="num">{coverage.get('total_features', 0)}</div><div class="label">功能点</div></div>
<div class="card"><div class="num">{coverage.get('covered_features', 0)}</div><div class="label">已覆盖功能点</div></div>
<div class="card"><div class="num">{round((coverage.get('coverage_rate') or 0) * 100, 1)}%</div><div class="label">覆盖率</div></div>
<div class="card"><div class="num">{overview.get('findings', {}).get('total', 0)}</div><div class="label">缺陷线索</div></div>
</div>
<h2>模块覆盖</h2>
<table><thead><tr><th>模块</th><th>功能点</th><th>已覆盖</th><th>状态</th></tr></thead>
<tbody>{module_rows or '<tr><td colspan="4">无</td></tr>'}</tbody></table>
<h2>各维度用例</h2>
<table><thead><tr><th>维度</th><th>总数</th><th>已启用</th><th>通过</th><th>失败</th></tr></thead>
<tbody>{dimension_rows or '<tr><td colspan="5">无</td></tr>'}</tbody></table>
<h2>执行历史</h2>
<table><thead><tr><th>运行</th><th>状态</th><th>总数</th><th>通过</th><th>失败</th><th>flaky</th><th>时间</th></tr></thead>
<tbody>{run_rows or '<tr><td colspan="7">无</td></tr>'}</tbody></table>
</body></html>
"""
