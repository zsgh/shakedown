"""AI 缺陷分诊：把大量白盒/黑盒 finding 聚类成可处理的组。

设计红线：
- 只写派生字段（cluster_key / triage / triage_status），不覆盖原始 title / description /
  evidence / code_location，也不删除 finding；
- 人工已确认 / 已忽略的 finding 不参与重分诊；
- AI 失败时保留原始 finding，按「文件 + 风险类别 + 行窗口」做确定性分组，标为未分诊；
- 所有分组都能展开回原始条目。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.agents import record_trace
from app.agent.providers.base import LLMError, LLMProvider
from app.models import Finding, KnowledgeSource, Project

TRIAGE_VERSION = "triage-v1"

VERDICTS = {"real_bug", "false_positive", "needs_verification", "duplicate"}
ACTIONS = {"fix_code", "add_test", "ignore", "merge", "review"}
SEVERITIES = {"critical", "high", "medium", "low"}
CLASSIFICATIONS = {"product_defect", "test_issue", "environment"}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theme_key": {"type": "string"},
                    "title": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["real_bug", "false_positive", "needs_verification", "duplicate"],
                    },
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "classification": {
                        "type": "string",
                        "enum": ["product_defect", "test_issue", "environment"],
                    },
                    "recommended_action": {
                        "type": "string",
                        "enum": ["fix_code", "add_test", "ignore", "merge", "review"],
                    },
                    "finding_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["theme_key", "title", "verdict", "reason", "finding_ids"],
            },
        }
    },
    "required": ["groups"],
}

RISK_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("越权", "鉴权", "权限", "认证", "csrf", "ssrf"), "auth", "鉴权 / 越权"),
    (("注入", "xss", "路径", "文件", "校验", "边界", "参数", "输入", "枚举", "长度"), "input", "输入 / 路径校验"),
    (("并发", "竞态", "重入", "锁", "重复提交", "tom", "toct"), "concurrency", "并发 / 竞态"),
    (("事务", "原子", "一致性"), "transaction", "事务 / 一致性"),
    (("异常", "吞没", "错误", "panic", "空指针"), "exception", "异常处理"),
    (("敏感", "日志", "泄露", "明文", "token", "secret"), "sensitive", "敏感信息"),
    (("超时", "可用", "资源", "泄漏", "内存", "挂起"), "availability", "可用性 / 资源"),
    (("逻辑", "冗余", "状态", "时序", "竞态"), "logic", "逻辑 / 状态"),
]


def normalize_risk_type(risk_type: str) -> tuple[str, str]:
    text = str(risk_type or "").lower()
    for keywords, category, label in RISK_RULES:
        if any(keyword in text for keyword in keywords):
            return category, label
    return "other", (risk_type or "其他")


def _module_of(finding: Finding) -> str:
    location = finding.code_location or {}
    path = str(location.get("file_path") or finding.module or "unknown")
    return path.split("/")[0] if "/" in path else path


def _code_roots(session: Session, project: Project) -> list[Path]:
    roots: list[Path] = []
    for source in session.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project.id,
            KnowledgeSource.kind == "code",
        )
    ).all():
        path = Path(source.location).expanduser()
        if path.exists():
            roots.append(path.resolve())
    return roots


def _snippet(roots: list[Path], file_path: str, line: int, radius: int = 6) -> str:
    if not file_path:
        return ""
    for root in roots:
        candidate = (root / file_path).resolve()
        if candidate != root and root not in candidate.parents:
            continue
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = max(0, line - radius - 1)
        end = min(len(lines), line + radius)
        return "\n".join(f"{index + 1}: {lines[index]}" for index in range(start, end))
    return ""


def classify_triage(
    *,
    verdict: str,
    action: str,
    risk_type: str = "",
    reason: str = "",
    classification: str | None = None,
) -> str:
    """三分类：产品缺陷 / 测试侧问题 / 环境问题。"""
    value = str(classification or "").strip().lower()
    if value in CLASSIFICATIONS:
        return value
    text = f"{risk_type} {reason}".lower()
    if any(marker in text for marker in ("环境", "网络", "依赖", "端口", "数据库连接", "sandbox", "超时", "服务未启动")):
        return "environment"
    if verdict == "false_positive" or action in {"ignore", "add_test", "merge"}:
        return "test_issue"
    if any(marker in text for marker in ("用例", "断言", "误报", "重复", "flaky", "测试数据")):
        return "test_issue"
    if verdict in {"real_bug", "duplicate"} or action == "fix_code":
        return "product_defect"
    return "test_issue"


def _fallback_triage(finding: Finding) -> dict[str, Any]:
    _, label = normalize_risk_type(finding.risk_type)
    return {
        "title": label,
        "verdict": "needs_verification",
        "severity": finding.severity or "medium",
        "confidence": 0.0,
        "reason": "AI 分诊没有覆盖这条发现，已保留原始内容，等待人工复核。",
        "recommended_action": "review",
        "model": "",
        "version": TRIAGE_VERSION,
        "triaged_at": datetime.now(timezone.utc).isoformat(),
    }


def _clean_key(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", str(value or "group")).strip("-")
    return (text or "group")[:80]


def triage_findings(
    session: Session,
    project: Project,
    provider: LLMProvider,
    *,
    source: str = "whitebox",
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, int]:
    """按模块让 AI 聚类 + 分诊。只写派生字段，不删除任何 finding。"""
    findings = list(
        session.scalars(
            select(Finding)
            .where(
                Finding.project_id == project.id,
                Finding.status == "pending",
                *([] if source == "all" else [Finding.source == source]),
            )
            .order_by(Finding.id)
        ).all()
    )
    if not findings:
        return {"findings": 0, "clusters": 0, "ai_groups": 0}

    roots = _code_roots(session, project)
    by_module: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_module[_module_of(finding)].append(finding)

    modules = sorted(by_module)
    total = len(modules)
    cluster_keys: set[str] = set()
    ai_groups = 0
    for index, module in enumerate(modules):
        if on_progress:
            on_progress(index, total, module)
        items = by_module[module]
        used: set[int] = set()
        system = (
            "你是代码审查分诊专家。你的任务不是重新找 bug，而是把给定 finding 按**同一根因**分组，"
            "并判断每条组是真实缺陷、误报、需要运行时验证还是重复。"
            "同时给出三分类 classification：product_defect=被测系统缺陷，"
            "test_issue=测试用例/断言/环境准备等测试侧问题，environment=被测环境/依赖问题。"
            "只能使用给定 finding 和代码片段作为依据；不确定时选 needs_verification，不要武断判误报。"
            "每个 finding id 必须出现在且只出现在一个组里。只输出 JSON。"
        )
        # 单次分诊太多条会触发输出截断，按 10 条一批；每批都必须覆盖本批所有 id
        for chunk_index, start in enumerate(range(0, len(items), 10)):
            chunk = items[start : start + 10]
            payload = []
            for finding in chunk:
                location = finding.code_location or {}
                payload.append(
                    {
                        "id": finding.id,
                        "title": finding.title,
                        "severity": finding.severity,
                        "risk_type": finding.risk_type,
                        "file": location.get("file_path", ""),
                        "line": location.get("start_line"),
                        "description": (finding.description or "")[:500],
                        "snippet": _snippet(
                            roots,
                            str(location.get("file_path") or ""),
                            int(location.get("start_line") or 0),
                        ),
                    }
                )
            user = (
                f"对模块 {module} 的本批 {len(chunk)} 条 finding 做无损分诊。"
                "同一根因（同文件相邻代码 / 同一类设计问题）合并成组；"
                "不要遗漏任何 finding_id。"
            )
            data: dict[str, Any] = {}
            try:
                data = provider.structured(
                    system,
                    user,
                    TRIAGE_SCHEMA,
                    context={"task": "finding_triage", "module": module, "findings": payload},
                    reasoning_effort="low",
                    max_tokens=8192,
                )
            except LLMError as exc:
                record_trace(
                    session,
                    project_id=project.id,
                    kind="triage",
                    step=f"triage:{module}:{chunk_index + 1}",
                    payload={"input": {"module": module, "findings": len(chunk)}, "error": str(exc)},
                )

            by_id = {finding.id: finding for finding in chunk}
            groups = data.get("groups") if isinstance(data.get("groups"), list) else []
            for group_index, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                ids = [
                    int(value)
                    for value in (group.get("finding_ids") or [])
                    if str(value).isdigit() and int(value) in by_id and int(value) not in used
                ]
                if not ids:
                    continue
                verdict = str(group.get("verdict") or "needs_verification")
                if verdict not in VERDICTS:
                    verdict = "needs_verification"
                action = str(group.get("recommended_action") or "review")
                if action not in ACTIONS:
                    action = "review"
                severity = str(group.get("severity") or "medium")
                if severity not in SEVERITIES:
                    severity = "medium"
                title = str(group.get("title") or f"{module} 风险组")
                key = f"{module}:{_clean_key(group.get('theme_key') or title)}:{chunk_index}:{group_index}"
                triage = {
                    "title": title,
                    "verdict": verdict,
                    "severity": severity,
                    "confidence": max(0.0, min(1.0, float(group.get("confidence") or 0.0))),
                    "reason": str(group.get("reason") or "")[:2000],
                    "recommended_action": action,
                    "classification": classify_triage(
                        verdict=verdict,
                        action=action,
                        risk_type=str(by_id[ids[0]].risk_type or ""),
                        reason=str(group.get("reason") or ""),
                        classification=group.get("classification") if isinstance(group.get("classification"), str) else None,
                    ),
                    "theme_key": _clean_key(group.get("theme_key") or title),
                    "model": getattr(provider, "model", ""),
                    "version": TRIAGE_VERSION,
                    "triaged_at": datetime.now(timezone.utc).isoformat(),
                }
                if len(ids) == 1:
                    category, category_label = normalize_risk_type(by_id[ids[0]].risk_type)
                    # 单条发现按「模块 + 类别 + 判定」合组，避免几十个 1 条组继续刷屏
                    key = f"{module}:single:{verdict}:{category}"
                    triage["group_title"] = f"{category_label} · 同类单条发现"
                canonical = ids[0]
                for finding_id in ids:
                    finding = by_id[finding_id]
                    finding.cluster_key = key
                    finding.triage = triage
                    finding.triage_status = "ai_triaged"
                    finding.duplicate_of = canonical if verdict == "duplicate" and finding_id != canonical else None
                    used.add(finding_id)
                cluster_keys.add(key)
                ai_groups += 1

            record_trace(
                session,
                project_id=project.id,
                kind="triage",
                step=f"triage:{module}:{chunk_index + 1}",
                payload={
                    "input": {"module": module, "findings": len(chunk)},
                    "output": {"groups": len(groups), "covered": len(used)},
                },
            )

        # 未被 AI 覆盖的条目不丢弃，按确定性规则挂到本地组，并标记未分诊
        for finding in items:
            if finding.id in used:
                continue
            category, category_label = normalize_risk_type(finding.risk_type)
            key = f"{module}:untriaged:{category}"
            finding.cluster_key = key
            fallback = _fallback_triage(finding)
            fallback["group_title"] = f"{category_label} · 未分诊"
            finding.triage = fallback
            finding.triage_status = "untriaged"
            finding.duplicate_of = None
            cluster_keys.add(key)

        if on_progress:
            on_progress(index + 1, total, module)

    session.flush()
    return {
        "findings": len(findings),
        "clusters": len(cluster_keys),
        "ai_groups": ai_groups,
    }
