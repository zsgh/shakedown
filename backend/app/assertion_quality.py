"""断言质量：强度评分、oracle mutation 与 oracle 覆盖趋势。

这是静态/回放审计，不重新执行被测系统：
- 强度评分看断言类型、证据、expected 是否具体；
- mutation 审计拿最近成功响应构造“错误响应”，看断言能否杀掉；
- oracle 趋势按最近多次 run 的断言依据来源统计。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import collect_assertions
from app.models import CaseResult, Project, Run, TestCase

# 断言类型的基础强度（0-100）
_KIND_STRENGTH = {
    "status_code": 20,
    "text_contains": 35,
    "json_path": 55,
    "timing": 60,
    "db_state": 80,
    "cross_api": 85,
}
_WEAK_OPERATORS = {"exists", "not_exists"}
_SOURCE_BONUS = {"openapi": 15, "code": 15, "doc": 12, "policy": 18, "project_config": 12, "manual": 0, "none": -10}


def _json_path(data: Any, path: str) -> tuple[bool, Any]:
    if not path or path in {"$", ""}:
        return True, data
    expression = path.strip()
    if expression.startswith("$"):
        expression = expression[1:]
    if expression.startswith("."):
        expression = expression[1:]
    current = data
    for key, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", expression):
        if key:
            if not isinstance(current, dict) or key not in current:
                return False, None
            current = current[key]
        else:
            if not isinstance(current, list) or int(index) >= len(current):
                return False, None
            current = current[int(index)]
    return True, current


def _compare(operator: str, actual: Any, expected: Any) -> bool:
    operator = (operator or "eq").lower()
    try:
        if operator == "eq":
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                return abs(float(actual) - float(expected)) < 1e-6
            return actual == expected
        if operator == "ne": return actual != expected
        if operator == "in": return actual in expected
        if operator == "not_in": return actual not in expected
        if operator == "lt": return float(actual) < float(expected)
        if operator == "le": return float(actual) <= float(expected)
        if operator == "gt": return float(actual) > float(expected)
        if operator == "ge": return float(actual) >= float(expected)
        if operator == "contains": return str(expected) in str(actual)
        if operator == "not_contains": return str(expected) not in str(actual)
        if operator == "exists": return actual is not None
        if operator == "not_exists": return actual is None
        if operator == "matches": return re.search(str(expected), str(actual)) is not None
        if operator == "empty": return actual in (None, "", [], {})
    except (TypeError, ValueError):
        return False
    return False


def assertion_strength(assertion: dict[str, Any]) -> dict[str, Any]:
    kind = str(assertion.get("kind") or "")
    base = _KIND_STRENGTH.get(kind, 0)
    evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
    source = str(evidence.get("source_type") or "none")
    operator = str(assertion.get("operator") or "eq").lower()
    score = base + _SOURCE_BONUS.get(source, 0)
    if operator in _WEAK_OPERATORS:
        score -= 12
    if assertion.get("status") != "confirmed":
        score -= 15
    if assertion.get("expected") in (None, "", [], {}):
        score -= 10
    score = max(0, min(100, score))
    if score >= 70:
        band = "strong"
    elif score >= 40:
        band = "medium"
    else:
        band = "weak"
    return {
        "score": score,
        "band": band,
        "kind": kind,
        "operator": operator,
        "source_type": source,
        "target": assertion.get("target") or "",
        "description": assertion.get("description") or "",
    }


def case_assertion_quality(case: TestCase) -> dict[str, Any]:
    assertions = collect_assertions(case.spec or {})
    scored = [assertion_strength(item) for item in assertions]
    if scored:
        average = round(sum(item["score"] for item in scored) / len(scored), 1)
        strongest = max(scored, key=lambda item: item["score"])
    else:
        average = 0.0
        strongest = None
    return {
        "case_id": case.id,
        "title": case.title,
        "status": case.status,
        "assertions": len(scored),
        "average_score": average,
        "band": "none" if not scored else ("strong" if average >= 70 else ("medium" if average >= 40 else "weak")),
        "strongest": strongest,
        "weakest": min(scored, key=lambda item: item["score"]) if scored else None,
        "details": scored,
    }


def project_assertion_quality(db: Session, project: Project, *, limit: int = 500) -> dict[str, Any]:
    cases = list(
        db.scalars(
            select(TestCase)
            .where(TestCase.project_id == project.id, TestCase.status != "disabled")
            .order_by(TestCase.priority, TestCase.id)
            .limit(max(1, min(limit, 2000)))
        ).all()
    )
    rows = [case_assertion_quality(case) for case in cases]
    total_assertions = sum(row["assertions"] for row in rows)
    weighted = sum(row["average_score"] * row["assertions"] for row in rows)
    return {
        "project_id": project.id,
        "cases": len(rows),
        "assertions": total_assertions,
        "average_score": round(weighted / total_assertions, 1) if total_assertions else 0.0,
        "bands": {
            "strong": sum(1 for row in rows if row["band"] == "strong"),
            "medium": sum(1 for row in rows if row["band"] == "medium"),
            # 只统计「有断言但弱」；无断言单独放在 no_assertions，避免重复计数
            "weak": sum(1 for row in rows if row["band"] == "weak"),
            "no_assertions": sum(1 for row in rows if row["band"] == "none"),
        },
        "probe_cases": sum(1 for case in cases if "probe" in (case.tags or [])),
        "weakest_cases": sorted(
            [row for row in rows if row["assertions"]],
            key=lambda row: (row["average_score"], row["case_id"]),
        )[:20],
        "items": rows[:200],
    }


def _mutate_value(value: Any, expected: Any) -> list[Any]:
    mutations: list[Any] = [None, "", 0, -1, "mutated", [], {}]
    if isinstance(value, bool):
        mutations.append(not value)
    elif isinstance(value, (int, float)):
        mutations.extend([value + 1, value - 1, 999999])
    elif isinstance(value, str):
        mutations.extend([value + "-mutated", value[::-1], "x"])
    elif isinstance(value, list):
        mutations.extend([value[:-1] if value else [1], []])
    elif isinstance(value, dict):
        mutations.extend([{**value, "__mutated__": True}, {}])
    result: list[Any] = []
    for item in mutations:
        try:
            if _compare("eq", item, value):
                continue
        except Exception:  # noqa: BLE001
            pass
        if item not in result:
            result.append(item)
    return result


def _assertion_actual(assertion: dict[str, Any], response: dict[str, Any]) -> tuple[bool, Any]:
    if "actual" in assertion:
        return True, assertion.get("actual")
    kind = assertion.get("kind")
    if kind == "status_code":
        return True, response.get("status_code")
    if kind == "json_path":
        return _json_path(response.get("body"), str(assertion.get("target") or ""))
    if kind == "text_contains":
        return True, response.get("body")
    return False, None


def mutation_audit(db: Session, project: Project, *, limit: int = 100) -> dict[str, Any]:
    """回放最近成功结果，对响应做 mutation，统计断言 kill rate。"""
    rows = list(
        db.scalars(
            select(CaseResult)
            .where(CaseResult.project_id == project.id, CaseResult.status == "pass")
            .order_by(CaseResult.id.desc())
            .limit(max(1, min(limit, 500)))
        ).all()
    )
    mutations = 0
    killed = 0
    assertions_checked = 0
    weak: list[dict[str, Any]] = []
    for row in rows:
        output = row.output if isinstance(row.output, dict) else {}
        response = row.response if isinstance(row.response, dict) else {}
        for assertion in output.get("assertions") or []:
            if not isinstance(assertion, dict) or not assertion.get("passed"):
                continue
            kind = str(assertion.get("kind") or "")
            if kind not in {"status_code", "json_path", "text_contains"}:
                continue
            found, actual = _assertion_actual(assertion, response)
            if not found:
                continue
            expected = assertion.get("expected")
            operator = str(assertion.get("operator") or "eq")
            if not _compare(operator, actual, expected):
                continue
            assertions_checked += 1
            local_mutations = 0
            local_killed = 0
            if kind == "status_code":
                candidates = [200, 201, 400, 404, 500]
                for value in candidates:
                    if _compare("eq", value, actual):
                        continue
                    local_mutations += 1
                    if not _compare(operator, value, expected):
                        local_killed += 1
            elif kind == "text_contains":
                expected_text = str(expected)
                if expected_text and expected_text in str(actual):
                    value = str(actual).replace(expected_text, "", 1)
                    local_mutations += 1
                    if not _compare(operator, value, expected):
                        local_killed += 1
            else:
                for value in _mutate_value(actual, expected):
                    local_mutations += 1
                    if not _compare(operator, value, expected):
                        local_killed += 1
            if local_mutations:
                mutations += local_mutations
                killed += local_killed
                if local_killed < local_mutations:
                    weak.append(
                        {
                            "result_id": row.id,
                            "case_id": row.case_id,
                            "kind": kind,
                            "target": assertion.get("target") or "",
                            "expected": expected,
                            "operator": operator,
                            "mutations": local_mutations,
                            "killed": local_killed,
                        }
                    )
    score = round(killed / mutations, 4) if mutations else 0.0
    return {
        "project_id": project.id,
        "results": len(rows),
        "assertions_checked": assertions_checked,
        "mutations": mutations,
        "killed": killed,
        "mutation_score": score,
        "weak_assertions": weak[:100],
    }


def oracle_trend(db: Session, project: Project, *, limit: int = 20) -> dict[str, Any]:
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.project_id == project.id, Run.status.in_(["completed", "failed"]))
            .order_by(Run.id.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
    )
    runs.reverse()
    points: list[dict[str, Any]] = []
    for run in runs:
        results = list(
            db.scalars(
                select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False))
            ).all()
        )
        mix: Counter[str] = Counter()
        by_source: Counter[str] = Counter()
        assertions = 0
        pending = 0
        for result in results:
            output = result.output if isinstance(result.output, dict) else {}
            for assertion in output.get("assertions") or []:
                if not isinstance(assertion, dict):
                    continue
                assertions += 1
                if assertion.get("pending"):
                    pending += 1
                evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
                source = str(evidence.get("source_type") or "none")
                by_source[source] += 1
                mix["contract" if source in {"openapi", "doc", "code", "project_config"} else
                    "policy" if source == "policy" else
                    "manual" if source == "manual" else "none"] += 1
        points.append(
            {
                "run_id": run.id,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "results": len(results),
                "assertions": assertions,
                "pending": pending,
                "oracle_mix": {key: mix.get(key, 0) for key in ("contract", "policy", "manual", "none")},
                "by_source": dict(by_source),
                "pass_rate": (run.stats or {}).get("pass_rate") if isinstance(run.stats, dict) else None,
            }
        )
    return {"project_id": project.id, "points": points}
