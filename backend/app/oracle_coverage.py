"""覆盖率分层：除了「有正常流用例」，还要看断言依据的类型与强度。

统计口径：
- contract：openapi / doc / code，能直接判失败的契约断言；
- policy：通用安全 / 可靠性策略（POLICY-*），可判失败且可审计；
- manual：人工假设（必须有作者或理由）；
- none：无依据，正常不应再产生，只用于暴露存量问题。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from app.executor.case_schema import collect_assertions, evidence_is_backed
from app.models import TestCase

_SOURCE_GROUP = {"openapi": "contract", "doc": "contract", "code": "contract", "project_config": "contract",
                 "policy": "policy", "manual": "manual"}


def _is_probe(case: TestCase) -> bool:
    spec = case.spec or {}
    return bool(spec.get("probe")) or "probe" in (case.tags or [])


def oracle_coverage(cases: list[TestCase]) -> dict[str, Any]:
    active = [case for case in cases if case.status != "disabled"]
    by_source: Counter[str] = Counter()
    mix: Counter[str] = Counter()
    assertions = 0
    pending = 0
    unbacked = 0
    cases_with_oracle = 0
    probe_cases = 0
    for case in active:
        if _is_probe(case):
            probe_cases += 1
        own = collect_assertions(case.spec or {})
        if own:
            cases_with_oracle += 1
        for assertion in own:
            evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
            source = str(evidence.get("source_type") or "none")
            by_source[source] += 1
            mix[_SOURCE_GROUP.get(source, "none")] += 1
            assertions += 1
            if assertion.get("status") != "confirmed":
                pending += 1
            if not evidence_is_backed(assertion) and assertion.get("status") != "confirmed":
                unbacked += 1
    total_cases = len(active)
    return {
        "cases": total_cases,
        "cases_with_oracle": cases_with_oracle,
        "probe_cases": probe_cases,
        "assertions": assertions,
        "by_source": dict(by_source),
        "oracle_mix": {key: mix.get(key, 0) for key in ("contract", "policy", "manual", "none")},
        "pending_assertions": pending,
        "unbacked_assertions": unbacked,
        "avg_assertions_per_case": round(assertions / total_cases, 2) if total_cases else 0.0,
        "case_oracle_rate": round(cases_with_oracle / total_cases, 4) if total_cases else 0.0,
    }
