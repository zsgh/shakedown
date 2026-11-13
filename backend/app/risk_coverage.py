"""风险类别覆盖率：区分「契约覆盖」与「风险覆盖」。

契约覆盖回答「文档写的行为测了吗」；风险覆盖回答「即使没有文档，
这些高风险类别有没有用例 / 策略 / 探针去覆盖」。未覆盖的类别应当显示为测试债。
"""
from __future__ import annotations

from typing import Any, Iterable

from app.executor.case_schema import collect_assertions, collect_contract_questions

RISK_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("auth", "鉴权与越权", ("鉴权", "越权", "未授权", "未鉴权", "权限", "401", "403", "securityschemes")),
    ("injection", "注入与路径穿越", ("注入", "sql", "xss", "命令注入", "路径穿越", "traversal")),
    ("error_handling", "错误处理与信息泄露", ("5xx", "500", "异常", "错误响应", "堆栈", "traceback", "sqlstate")),
    ("idempotency", "幂等与重复提交", ("幂等", "重复提交", "重复创建", "重复取消", "重复执行")),
    ("concurrency", "并发与竞态", ("并发", "竞态", "同时", "race")),
    ("pagination", "分页、排序与过滤", ("分页", "limit", "offset", "排序", "page")),
    ("isolation", "数据隔离", ("隔离", "跨项目", "跨用户", "横向越权", "租户")),
    ("sensitive_data", "敏感信息", ("敏感", "密码", "token", "泄露", "secret")),
    ("boundary", "边界与健壮性", ("边界", "超长", "空值", "非法类型", "unicode", "缺字段", "maxlength")),
)

_DIMENSION_HINTS = {
    "security": {"auth", "injection", "isolation", "sensitive_data"},
    "performance": {"concurrency"},
}


def _case_blob(case: Any) -> str:
    spec = case.spec or {}
    parts: list[str] = [case.title or "", getattr(case, "description", "") or "", case.missing_input or ""]
    parts.extend(str(tag) for tag in spec.get("tags") or [])
    for assertion in collect_assertions(spec):
        parts.extend(
            str(assertion.get(key) or "") for key in ("kind", "target", "description", "expected")
        )
        evidence = assertion.get("evidence") or {}
        if isinstance(evidence, dict):
            parts.extend(str(evidence.get(key) or "") for key in ("ref", "quote", "note"))
    for question in collect_contract_questions(spec):
        parts.extend(str(question.get(key) or "") for key in ("question", "missing_contract"))
    return " ".join(parts).lower()


def risk_coverage(cases: Iterable[Any]) -> dict[str, Any]:
    rows = [case for case in cases if getattr(case, "status", "") != "disabled"]
    blobs = {case.id: _case_blob(case) for case in rows}
    categories: list[dict[str, Any]] = []
    covered_count = 0
    for key, label, keywords in RISK_CATEGORIES:
        matched = [
            case
            for case in rows
            if (_DIMENSION_HINTS.get(case.dimension or "", set()) & {key})
            or any(keyword in blobs[case.id] for keyword in keywords)
        ]
        if matched:
            covered_count += 1
        probe_cases = [case.id for case in matched if (case.spec or {}).get("probe")]
        policy_cases = [
            case.id
            for case in matched
            if any(
                (assertion.get("evidence") or {}).get("source_type") == "policy"
                for assertion in collect_assertions(case.spec or {})
            )
        ]
        categories.append(
            {
                "key": key,
                "label": label,
                "covered": bool(matched),
                "cases": len(matched),
                "probe_cases": len(probe_cases),
                "policy_cases": len(policy_cases),
                "examples": matched[:3] and [case.id for case in matched[:3]],
            }
        )
    return {
        "covered": covered_count,
        "total": len(RISK_CATEGORIES),
        "coverage_rate": round(covered_count / len(RISK_CATEGORIES), 4) if RISK_CATEGORIES else 0.0,
        "categories": categories,
    }
