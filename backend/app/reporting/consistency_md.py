"""三方一致性 → AI 可直接执行的 Markdown 修复清单。

设计目标：这份 md 导出后可以直接扔给编码 AI（Claude Code / Cursor / Copilot Chat）开工 ——
每条不一致都带**证据定位**（文件:行号 / spec 声明 / 文档原文）、**修复指引**（该改代码还是改 spec）
与**统一验收标准**，AI 不需要回到平台查任何东西。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

SOURCE_LABEL = {"openapi": "spec", "code": "代码", "doc": "文档"}

# 强信号的 kind：契约与实现直接冲突，必须处理；其余 kind 都是弱信号（改进项）
STRONG_KINDS = {"code_only", "spec_only", "doc_only"}

# 状态 → (优先级, 优先级说明)；仅在接口没有 mismatch 明细时兜底使用
_PRIORITY_BY_STATE = {
    "inconsistent": ("P0", "强信号"),
    "conflict": ("P0", "强信号"),
    "partial": ("P2", "弱信号"),
}

# mismatch.kind → (短标签, 强弱, 修复指引)
KIND_GUIDANCE: dict[str, tuple[str, str, str]] = {
    "code_only": (
        "代码有 / spec 缺",
        "强信号",
        "先判断接口意图，再决定改哪边：\n"
        "1. 接口是**有意实现**的 → 在 OpenAPI spec 中补齐该接口定义（参数、请求体、响应、鉴权），"
        "以「证据」里代码的实际行为为准，不要凭空编造契约；\n"
        "2. 接口是**废弃 / 临时代码** → 删除代码中的路由，并确认文档中也没有提及它。",
    ),
    "spec_only": (
        "spec 有 / 未实现",
        "强信号",
        "spec 声明了但代码里找不到路由：\n"
        "1. 接口**应当存在** → 按 spec 实现路由，实现后补齐响应与错误分支；\n"
        "2. 接口**计划取消** → 从 OpenAPI spec 删除该条目，并同步清理文档中的描述。",
    ),
    "method_only": (
        "方法未声明",
        "弱信号",
        "同路径的其它来源只覆盖了部分方法：\n"
        "1. 该方法是**有意新增**的 → 把它补进缺失的那一侧（spec 声明 / 代码路由 / 文档说明）；\n"
        "2. 该方法**不该存在** → 删除多余的一侧。",
    ),
    "doc_missing": (
        "文档缺口",
        "弱信号",
        "接口有实现但文档没写。**以代码行为为准**在文档中补一段说明：用途、参数、响应字段、错误码；"
        "格式参考同目录下已有接口的写法。",
    ),
    "param_mismatch": (
        "参数不一致",
        "弱信号",
        "spec 声明与文档描述的参数对不上（通常是文档漏写或写了示例参数）。"
        "**以代码实现为准**：把文档补齐，或把 spec 与文档对齐到同一套参数；"
        "文档里的示例 query 值不算正式参数声明。",
    ),
    "doc_only": (
        "文档独有",
        "强信号",
        "文档提到、代码与 spec 都没有：\n"
        "1. 接口**应当存在** → 按文档描述实现路由并补 spec；\n"
        "2. 接口**已废弃** → 从文档删除该描述。",
    ),
}

# 合同（spec 声明 / 代码签名）里值得展示给 AI 的字段
_CONTRACT_FIELDS = ("params", "request_body", "responses", "security")


def render_consistency_markdown(
    project_name: str,
    items: list[dict[str, Any]],
    sources: list[dict[str, Any]] | None = None,
    project_id: int | None = None,
    base_url: str = "",
) -> str:
    """把一致性对照结果渲染成 AI 可执行的任务清单 Markdown。

    ``items`` 与 ``GET /projects/{id}/consistency`` 的返回同构。
    """
    strong = [i for i in items if _priority_of(i)[0] == "P0"]
    weak = [i for i in items if _priority_of(i)[0] != "P0"]
    ordered = strong + weak

    lines: list[str] = []
    lines.append(f"# 三方一致性修复清单 · {project_name}")
    lines.append("")
    lines.append(
        f"> 由 Shakedown 导出 · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        " · 对照来源：OpenAPI spec × 代码路由 × 项目文档"
    )
    lines.append(f"> 共 {len(ordered)} 项：强信号 {len(strong)} 项（P0）· 弱信号 {len(weak)} 项（P2）")
    lines.append("")
    _render_usage(lines)
    _render_sources(lines, sources or [])

    if not ordered:
        lines.append("## 任务清单")
        lines.append("")
        lines.append("没有发现三方不一致，无需修复。")
        lines.append("")
        return "\n".join(lines)

    _render_checklist(lines, ordered)
    _render_details(lines, ordered)
    _render_verify(lines, project_id, base_url)
    return "\n".join(lines)


def _render_usage(lines: list[str]) -> None:
    lines.append("## 使用说明（修复前必读）")
    lines.append("")
    lines.append("1. 按 **P0 → P2** 顺序处理；每完成一项，把下方「任务清单」里对应的 `- [ ]` 改成 `- [x]`。")
    lines.append("2. 每条任务都带**证据**（文件:行号 / spec 声明 / 文档原文）与**修复指引**，不要脱离证据盲改。")
    lines.append("3. 修复要**三处同步**：代码、OpenAPI spec、文档改了任意一处，检查另外两处是否需要跟着改。")
    lines.append(
        "4. 不确定「该改代码还是该改 spec」时，以**接口的真实意图**为准（详见每条任务的修复指引），"
        "而不是机械地让一边迁就另一边。"
    )
    lines.append(
        "5. **验收标准**：重新导入改过的知识源并重算一致性后，"
        "该接口从不一致清单中消失（状态变为 `consistent`）。"
    )
    lines.append("")


def _render_sources(lines: list[str], sources: list[dict[str, Any]]) -> None:
    if not sources:
        return
    lines.append("### 本项目知识源（改哪个文件看这里）")
    lines.append("")
    lines.append("| 类型 | 名称 | 位置 |")
    lines.append("|---|---|---|")
    for source in sources:
        kind = str(source.get("kind") or "?")
        if kind == "openapi":
            kind = "spec（OpenAPI）"
        elif kind == "markdown":
            kind = "文档"
        elif kind == "code":
            kind = "代码"
        role = source.get("role")
        kind = f"{kind}（{role}）" if role and role != "spec" else kind
        lines.append(f"| {kind} | {source.get('name') or ''} | `{source.get('location') or ''}` |")
    lines.append("")


def _render_checklist(lines: list[str], items: list[dict[str, Any]]) -> None:
    lines.append("## 任务清单")
    lines.append("")
    for index, item in enumerate(items, start=1):
        priority, strength = _priority_of(item)
        kinds = {m.get("kind") for m in (item.get("mismatches") or [])}
        labels = [KIND_GUIDANCE.get(k, (k, "", ""))[0] for k in kinds if k]
        label = "、".join(dict.fromkeys(labels)) or "待确认"
        lines.append(
            f"- [ ] **{priority} #{index}** `{item.get('method', '?')} {item.get('path', '?')}`"
            f" —— {label}（{strength}）"
        )
    lines.append("")


def _render_details(lines: list[str], items: list[dict[str, Any]]) -> None:
    lines.append("## 任务详情")
    lines.append("")
    for index, item in enumerate(items, start=1):
        priority, strength = _priority_of(item)
        method = item.get("method", "?")
        path = item.get("path", "?")
        lines.append(f"### #{index} · {priority} · `{method} {path}`")
        lines.append("")
        summary = (item.get("summary") or "").strip()
        if summary:
            lines.append(f"- **接口摘要**：`{summary}`")
        sources = item.get("sources") or []
        if sources:
            source_text = "、".join(SOURCE_LABEL.get(s, s) for s in sources)
            lines.append(f"- **涉及来源**：{source_text}")
        lines.append(f"- **优先级**：{priority}（{strength}）")
        lines.append("")
        mismatches = item.get("mismatches") or []
        if not mismatches:
            lines.append("（无明细，仅有状态标记）")
            lines.append("")
            continue
        for mismatch in mismatches:
            _render_mismatch(lines, mismatch)
        lines.append("---")
        lines.append("")


def _render_mismatch(lines: list[str], mismatch: dict[str, Any]) -> None:
    kind = str(mismatch.get("kind") or "unknown")
    label, strength, guidance = KIND_GUIDANCE.get(kind, (kind, "", ""))
    lines.append(f"#### {label}（`{kind}`，{strength or '待确认'}）")
    lines.append("")
    message = (mismatch.get("message") or "").strip()
    if message:
        lines.append(message)
        lines.append("")
    evidence = _render_evidence(mismatch.get("evidence") or [])
    if evidence:
        lines.append("**证据**")
        lines.append("")
        lines.extend(evidence)
        lines.append("")
    if guidance:
        lines.append("**修复指引**")
        lines.append("")
        lines.append(guidance)
        lines.append("")


def _render_evidence(evidence: list[Any]) -> list[str]:
    """把 evidence 渲染成列表项；接口记录会附带其契约（spec 声明 / 代码签名）。"""
    lines: list[str] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if "quote" in item:  # 文档片段
            ref = item.get("ref") or "（未记录文件）"
            lines.append(f"- 文档 `{ref}`：")
            quote = str(item.get("quote") or "").strip()
            if quote:
                lines.extend(f"  > {line}".rstrip() for line in quote.splitlines())
            continue
        # 接口记录（openapi / code 侧）；两侧可能是同一条记录，去重
        ident = (item.get("id"), item.get("method"), item.get("path"))
        if ident in seen:
            continue
        seen.add(ident)
        for source in item.get("sources") or []:
            if not isinstance(source, dict):
                continue
            kind = str(source.get("type") or "?")
            label = SOURCE_LABEL.get(kind, kind)
            ref = source.get("ref") or "（未记录位置）"
            summary = (source.get("summary") or "").strip()
            tail = f" —— `{summary}`" if summary else ""
            lines.append(f"- {label} `{ref}`{tail}")
        contract = _contract_block(item)
        if contract:
            lines.append("")
            lines.append("  ```json")
            lines.extend(f"  {line}".rstrip() for line in contract)
            lines.append("  ```")
    return lines


def _contract_block(endpoint_record: dict[str, Any]) -> list[str]:
    """契约摘要：把 spec 声明 / 代码签名里非空的字段压成一小段 JSON，帮 AI 对齐契约。"""
    contract = {
        key: endpoint_record.get(key)
        for key in _CONTRACT_FIELDS
        if isinstance(endpoint_record.get(key), dict) and endpoint_record.get(key)
    }
    if not contract:
        return []
    import json

    return json.dumps(contract, ensure_ascii=False, indent=2).splitlines()


def _render_verify(lines: list[str], project_id: int | None, base_url: str) -> None:
    lines.append("## 验证方式")
    lines.append("")
    if project_id is None:
        lines.append("修完后在平台「一致性」页点击**重新对照**，确认接口状态变为「一致」。")
        lines.append("")
        return
    api = f"{base_url.rstrip('/')}/api/projects/{project_id}" if base_url else f"/api/projects/{project_id}"
    lines.append(
        "修完后任选一种方式重新对照；**如果改了 spec 文件，先在平台对对应知识源点「重新解析」**"
        "（会自动做 spec diff，并把受影响的用例置为 `needs_review`）："
    )
    lines.append("")
    lines.append("```bash")
    lines.append(f"curl -X POST {api}/consistency/check   # 重算一致性")
    lines.append(f"curl {api}/consistency                 # 查看剩余不一致")
    lines.append("```")
    lines.append("")
    lines.append("也可以在看板「一致性」页点击「重新对照」，或重新打开本页导出最新清单核对进度。")
    lines.append("")


def _priority_of(item: dict[str, Any]) -> tuple[str, str]:
    """任务优先级按 mismatch 的 kind 判定，与对照逻辑的强弱口径保持一致。

    接口可能混有强弱两类 mismatch（state=inconsistent），取最强的作为任务优先级；
    没有明细时退回 state 兜底。
    """
    kinds = {str(m.get("kind") or "") for m in (item.get("mismatches") or [])}
    if kinds & STRONG_KINDS:
        return ("P0", "强信号")
    if not kinds:
        return _PRIORITY_BY_STATE.get(str(item.get("state")), ("P2", "弱信号"))
    return ("P2", "弱信号")
