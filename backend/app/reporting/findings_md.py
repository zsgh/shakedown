"""缺陷 → AI / 人可执行的修复清单。

导出目标是「拿到文件就能开工」：
- 按 AI 分诊后的根因分组，而不是把几百条原始 finding 平铺；
- 每条 finding 保留 ID、证据定位、代码片段、复现命令；
- 附带修复建议与验收标准，Markdown 给编码 AI，JSON 给自动化。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from app.models import Finding

VERDICT_LABEL = {
    "real_bug": "建议修复",
    "needs_verification": "需运行时验证",
    "false_positive": "建议忽略",
    "duplicate": "重复",
    "untriaged": "未分诊",
    "accepted": "已接受风险",
    "wont_fix": "不修复",
}
ACTION_LABEL = {
    "fix_code": "修复代码",
    "add_test": "补验证用例",
    "ignore": "忽略",
    "merge": "合并重复",
    "review": "人工复核",
}
SEVERITY_LABEL = {"critical": "严重", "high": "高", "medium": "中", "low": "低"}
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
VERDICT_RANK = {
    "real_bug": 0,
    "needs_verification": 1,
    "untriaged": 2,
    "duplicate": 3,
    "false_positive": 4,
    "accepted": 5,
    "wont_fix": 6,
}
SENSITIVE_KEY = re.compile(r"(?i)authorization|cookie|token|secret|password|api[_-]?key")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _redact_text(value: str) -> str:
    text = value
    # 先把 Authorization: Bearer xxx 整体处理，避免后续通用规则把 Bearer 也吃掉
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)((?:authorization|cookie|token|secret|password|api[_-]?key)\s*[:=]\s*)"
        r"(?!bearer\b|[\$\{\(\[])[^\s,;\"']+",
        r"\1***",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text)
    # 代码/配置里的字符串字面量：apiKey: "sk-..." / token = '...'
    text = re.sub(
        r"(?i)((?:authorization|cookie|token|secret|password|api[_-]?key)\s*[:=]\s*)"
        r"(['\"])([^'\"]+)\2",
        r"\1\2***\2",
        text,
    )
    text = re.sub(
        r"(?i)(\"(?:authorization|cookie|token|secret|password|api[_-]?key)\"\s*:\s*\")[^\"]*",
        r"\1***",
        text,
    )
    return text


def _redact(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if SENSITIVE_KEY.fullmatch(str(child_key)) and child_value not in (None, ""):
                out[child_key] = "***"
            else:
                out[child_key] = _redact(child_value, str(child_key))
        return out
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _verdict(finding: Finding) -> str:
    # 人工结论优先：误报 / 接受风险 / 不修复 / 已确认不能被 AI 的旧分诊结果覆盖。
    if finding.status in {"false_positive", "accepted", "wont_fix"}:
        return finding.status
    if finding.status == "confirmed":
        return "real_bug"
    if finding.triage_status == "untriaged":
        return "untriaged"
    return str((finding.triage or {}).get("verdict") or "untriaged")


def _priority(verdict: str, severity: str) -> str:
    if verdict == "real_bug":
        return "P0" if severity in {"critical", "high"} else "P1"
    if verdict in {"needs_verification", "untriaged"}:
        return "P2"
    return "P3"


def _serialize_finding(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "source": finding.source,
        "title": _redact_text(finding.title or ""),
        "description": _redact_text(finding.description or ""),
        "severity": finding.severity,
        "status": finding.status,
        "module": finding.module,
        "risk_type": finding.risk_type,
        "evidence": _redact(finding.evidence or []),
        "code_location": _redact(finding.code_location or {}),
        "repro": _redact_text(finding.repro or ""),
        "case_id": finding.case_id,
        "result_id": finding.result_id,
        "triage_status": finding.triage_status,
        "triage": _redact(finding.triage or {}),
        "cluster_key": finding.cluster_key,
        "duplicate_of": finding.duplicate_of,
        "fix_status": finding.fix_status or "open",
        "fix_verified_at": _iso(finding.fix_verified_at),
        "fix_claim": _redact(finding.fix_claim or {}),
        "created_at": _iso(finding.created_at),
    }


def build_finding_groups(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    """把同一 cluster_key 的 finding 合成一个修复任务。"""
    buckets: dict[str, list[Finding]] = {}
    for finding in findings:
        key = finding.cluster_key or f"finding:{finding.id}"
        buckets.setdefault(key, []).append(finding)

    groups: list[dict[str, Any]] = []
    for key, members in buckets.items():
        members = sorted(
            members,
            key=lambda item: (SEVERITY_RANK.get(item.severity, 9), item.id),
        )
        triaged = next((member for member in members if _verdict(member) != "untriaged"), members[0])
        triage = triaged.triage or {}
        verdict = _verdict(triaged)
        severity = min(
            (member.severity or "medium" for member in members),
            key=lambda value: SEVERITY_RANK.get(value, 9),
        )
        title = str(
            triage.get("group_title")
            or triage.get("title")
            or triaged.risk_type
            or triaged.title
            or "未命名缺陷组"
        )
        files: list[str] = []
        modules: list[str] = []
        for member in members:
            location = member.code_location or {}
            file_path = str(location.get("file_path") or "").strip()
            if file_path and file_path not in files:
                files.append(file_path)
            module = (member.module or "").strip()
            if module and module not in modules:
                modules.append(module)
        groups.append(
            {
                "id": key,
                "title": _redact_text(title),
                "verdict": verdict,
                "verdict_label": VERDICT_LABEL.get(verdict, verdict),
                "priority": _priority(verdict, severity),
                "severity": severity,
                "severity_label": SEVERITY_LABEL.get(severity, severity),
                "confidence": max(
                    float((member.triage or {}).get("confidence") or 0) for member in members
                ),
                "reason": _redact_text(str(triage.get("reason") or "")),
                "recommended_action": str(triage.get("recommended_action") or ""),
                "action_label": ACTION_LABEL.get(
                    str(triage.get("recommended_action") or ""), "人工复核"
                ),
                "modules": modules,
                "files": files,
                "member_ids": [member.id for member in members],
                "findings": [_serialize_finding(member) for member in members],
            }
        )

    groups.sort(
        key=lambda group: (
            VERDICT_RANK.get(group["verdict"], 9),
            SEVERITY_RANK.get(group["severity"], 9),
            -len(group["findings"]),
            group["id"],
        )
    )
    return groups


def summarize_groups(groups: list[dict[str, Any]]) -> dict[str, Any]:
    by_verdict: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for group in groups:
        by_verdict[group["verdict"]] = by_verdict.get(group["verdict"], 0) + len(group["findings"])
        by_severity[group["severity"]] = by_severity.get(group["severity"], 0) + 1
    return {
        "groups": len(groups),
        "findings": sum(len(group["findings"]) for group in groups),
        "by_verdict": by_verdict,
        "by_severity": by_severity,
    }


def _fence(text: str) -> list[str]:
    fence = "```"
    while fence in text:
        fence += "`"
    return [f"{fence}text", text.rstrip(), fence]


def _md_inline(value: Any) -> str:
    return str(value or "").replace("\n", " ").strip()


def _render_evidence(lines: list[str], evidence: list[dict[str, Any]]) -> None:
    if not evidence:
        return
    lines.append("**证据**")
    lines.append("")
    for item in evidence:
        if not isinstance(item, dict):
            lines.append(f"- {_md_inline(item)}")
            continue
        source_type = str(item.get("source_type") or item.get("type") or "证据")
        ref = _md_inline(item.get("ref"))
        quote = _md_inline(item.get("quote"))
        detail = item.get("detail")
        prefix = f"- `{source_type}`"
        if ref:
            prefix += f" `{ref}`"
        if quote:
            prefix += f" — {quote}"
        lines.append(prefix)
        if detail not in (None, ""):
            detail_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False, indent=2)
            lines.extend(_fence(detail_text))
            lines.append("")
    lines.append("")


def render_findings_markdown(
    *,
    project_name: str,
    project_id: int,
    groups: list[dict[str, Any]],
    filters: dict[str, Any],
) -> str:
    summary = summarize_groups(groups)
    lines: list[str] = []
    lines.append(f"# 缺陷修复清单 · {project_name}")
    lines.append("")
    lines.append(f"> 由 Shakedown 导出 · {datetime.now().strftime('%Y-%m-%d %H:%M')} · 项目 #{project_id}")
    lines.append(
        f"> 共 {summary['groups']} 个修复组 / {summary['findings']} 条缺陷；"
        f"建议修复 {summary['by_verdict'].get('real_bug', 0)} 条，"
        f"需验证 {summary['by_verdict'].get('needs_verification', 0)} 条。"
    )
    active_filters = {key: value for key, value in filters.items() if value not in (None, "", "all")}
    if active_filters:
        lines.append("> 导出筛选：" + " · ".join(f"`{key}={value}`" for key, value in active_filters.items()))
    lines.append("")
    lines.append("## 使用说明（修复前必读）")
    lines.append("")
    lines.append("1. 按 **P0 → P3** 顺序处理；完成一项后把「任务清单」里的 `- [ ]` 改成 `- [x]`。")
    lines.append("2. 先读证据和代码位置，再决定改代码、改 spec 还是改文档；不要脱离证据盲改。")
    lines.append("3. 同一组 finding 通常共享根因，优先做一次集中修复，不要逐条打补丁。")
    lines.append("4. 删除、权限、进程操作等高风险修复，先补回归测试，涉及不可逆操作时要求人工确认。")
    lines.append("5. 验收：重新执行相关用例 / 白盒走查后，对应 finding 不再出现；并在 PR 说明中引用 finding ID。")
    lines.append("6. 本清单中的 Authorization / Cookie / token 等敏感值已做脱敏，不要把真实密钥写回代码或工单。")
    lines.append("")
    if not groups:
        lines.append("## 任务清单")
        lines.append("")
        lines.append("当前筛选下没有需要修复的缺陷。")
        lines.append("")
        return "\n".join(lines)

    lines.append("## 任务清单")
    lines.append("")
    for index, group in enumerate(groups, start=1):
        member_text = "、".join(f"#{item['id']}" for item in group["findings"][:8])
        if len(group["findings"]) > 8:
            member_text += f" 等 {len(group['findings'])} 条"
        lines.append(
            f"- [ ] **#{index} [{group['priority']}] [{group['verdict_label']}] "
            f"{_md_inline(group['title'])}** — {group['severity_label']} · "
            f"{len(group['findings'])} 条（{member_text}）"
        )
    lines.append("")
    lines.append("## 任务详情")
    lines.append("")
    for index, group in enumerate(groups, start=1):
        lines.append(
            f"### #{index} · [{group['priority']}] · [{group['verdict_label']}] "
            f"{_md_inline(group['title'])}"
        )
        lines.append("")
        lines.append(f"- **修复组键**：`{group['id']}`")
        lines.append(f"- **建议动作**：{group['action_label']}")
        lines.append(f"- **严重级别**：{group['severity_label']}（{group['severity']}）")
        if group["confidence"]:
            lines.append(f"- **AI 置信度**：{round(group['confidence'] * 100)}%")
        if group["modules"]:
            lines.append("- **涉及模块**：" + "、".join(f"`{item}`" for item in group["modules"]))
        if group["files"]:
            lines.append("- **涉及文件**：" + "、".join(f"`{item}`" for item in group["files"]))
        if group["reason"]:
            lines.append("")
            lines.append("**修复理由 / 根因判断**")
            lines.append("")
            lines.append(group["reason"])
        lines.append("")
        lines.append("#### 原始发现")
        lines.append("")
        for finding in group["findings"]:
            lines.append(
                f"##### #{finding['id']} · {SEVERITY_LABEL.get(finding['severity'], finding['severity'])} "
                f"· {finding['status']} · {_md_inline(finding['title'])}"
            )
            lines.append("")
            lines.append(f"- **来源**：{finding['source']} · **模块**：`{finding['module'] or '—'}`")
            if finding["risk_type"]:
                lines.append(f"- **风险类型**：{_md_inline(finding['risk_type'])}")
            if finding.get("fix_status") and finding["fix_status"] != "open":
                lines.append(f"- **修复状态**：{finding['fix_status']}")
                claim = finding.get("fix_claim") or {}
                if claim.get("commit_sha"):
                    lines.append(f"- **修复 Commit**：`{claim['commit_sha']}`")
                if claim.get("pr_url"):
                    lines.append(f"- **修复 PR**：{claim['pr_url']}")
                if claim.get("note"):
                    lines.append(f"- **修复说明**：{_md_inline(claim['note'])}")
            if finding["description"]:
                lines.append("")
                lines.append(finding["description"])
            location = finding["code_location"] or {}
            if location.get("file_path"):
                start = location.get("start_line")
                end = location.get("end_line")
                line_text = f":{start}" if start else ""
                if end and end != start:
                    line_text += f"-{end}"
                lines.append("")
                lines.append(f"**代码位置**：`{location.get('file_path')}{line_text}`")
                snippet = location.get("snippet")
                if snippet:
                    lines.extend(_fence(str(snippet)))
            _render_evidence(lines, finding["evidence"])
            if finding["repro"]:
                lines.append("**复现命令**")
                lines.append("")
                lines.extend(_fence(finding["repro"]))
                lines.append("")
            if finding["duplicate_of"]:
                lines.append(f"- **重复于**：finding #{finding['duplicate_of']}")
                lines.append("")
            lines.append("---")
            lines.append("")
        lines.append("#### 验收标准")
        lines.append("")
        for finding in group["findings"]:
            lines.append(
                f"- [ ] 修复后重新执行 / 走查，finding #{finding['id']} 不再出现或已明确标记为误报"
            )
        lines.append("- [ ] 新增或更新回归测试，覆盖该根因")
        lines.append("- [ ] PR 描述引用上面的 finding ID，并说明验证方式")
        lines.append("")
    return "\n".join(lines)


def render_group_issue_markdown(group: dict[str, Any], project_name: str) -> str:
    """单个修复组的工单正文，作为 GitHub / Jira Issue 的 body。"""
    lines = [
        f"由 Shakedown 从项目「{project_name}」导出。原始 finding 保留在平台，修复后请回填状态。",
        "",
        f"## 结论：{group['verdict_label']}（{group['priority']}）",
        "",
        f"- 严重级别：{group['severity_label']}（{group['severity']}）",
        f"- 建议动作：{group['action_label']}",
    ]
    if group["modules"]:
        lines.append("- 涉及模块：" + "、".join(f"`{item}`" for item in group["modules"]))
    if group["files"]:
        lines.append("- 涉及文件：" + "、".join(f"`{item}`" for item in group["files"]))
    if group["reason"]:
        lines.extend(["", "## 根因判断", "", group["reason"]])
    lines.extend(["", "## 原始发现", ""])
    for finding in group["findings"]:
        lines.append(f"### #{finding['id']} · {finding['severity']} · {finding['status']}")
        lines.append("")
        lines.append(f"**{finding['title']}**")
        lines.append("")
        if finding["description"]:
            lines.append(finding["description"])
            lines.append("")
        location = finding["code_location"] or {}
        if location.get("file_path"):
            start = location.get("start_line")
            end = location.get("end_line")
            suffix = f":{start}" if start else ""
            if end and end != start:
                suffix += f"-{end}"
            lines.append(f"代码位置：`{location.get('file_path')}{suffix}`")
            lines.append("")
        _render_evidence(lines, finding["evidence"])
        if finding["repro"]:
            lines.extend(["**复现命令**", ""])
            lines.extend(_fence(finding["repro"]))
            lines.append("")
    lines.extend(
        [
            "## 验收标准",
            "",
            "- [ ] 修复后重新执行 / 白盒走查，对应 finding 不再出现或已明确标记为误报",
            "- [ ] 补充回归测试，覆盖该根因",
            "- [ ] PR 描述引用原始 finding ID",
            "",
            f"<!-- shakedown:group:{group['id']} -->",
        ]
    )
    return "\n".join(lines)


def build_findings_export(
    *,
    project_name: str,
    project_id: int,
    groups: list[dict[str, Any]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": "findings-export-v1",
        "project": {"id": project_id, "name": project_name},
        "generated_at": _now_iso(),
        "filters": filters,
        "summary": summarize_groups(groups),
        "groups": groups,
    }
