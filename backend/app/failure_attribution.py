"""失败归因：先走确定性规则，再交给模型。

定位
----
执行失败有四种完全不同的处置方式，混在一起就会「让用例背平台的锅」：

- ``environment``：请求没到被测系统 / 沙箱没起来。用例保持原状态，等环境恢复。
- ``platform``   ：执行器、断言归一化、变量渲染等平台自身能力不足。不能重写用例，
                   要修平台；能自动降级的先降级并重试一次。
- ``case``       ：期望或步骤确实写错。进入修复队列，由 repair 重写。
- ``defect``     ：被测系统返回 5xx / 泄露堆栈 / 违反显式约束。挂疑似缺陷，不改用例。

此外还有 ``evidence``（缺少契约或入口，根本构造不出可执行用例）和
``unconfirmed``（规则和模型都拿不准）。

本模块只做判定，不改任何状态；动作由调用方（试运行 / 正式归因）决定。
所有结论都带 ``reason_code`` 与可复核的 ``evidence``，便于回放和统计。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.enums import CaseResultStatus

RULES_VERSION = "rules-v1"

CATEGORY_ENVIRONMENT = "environment"
CATEGORY_PLATFORM = "platform"
CATEGORY_CASE = "case"
CATEGORY_DEFECT = "defect"
CATEGORY_EVIDENCE = "evidence"
CATEGORY_UNCONFIRMED = "unconfirmed"

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_ENVIRONMENT: "环境问题",
    CATEGORY_PLATFORM: "平台能力缺口",
    CATEGORY_CASE: "用例问题",
    CATEGORY_DEFECT: "疑似产品缺陷",
    CATEGORY_EVIDENCE: "依据不足",
    CATEGORY_UNCONFIRMED: "归因待确认",
}

REASON_LABELS: dict[str, str] = {
    "connection_refused": "连接被拒绝",
    "network_timeout": "请求超时",
    "dns_failure": "域名解析失败",
    "network_unreachable": "网络不可达",
    "connection_reset": "连接被重置",
    "gateway_unavailable": "网关/上游不可用",
    "sandbox_unavailable": "执行沙箱不可用",
    "command_assertion_kind": "命令断言类型错误",
    "command_assertion_stream": "命令断言查错输出流",
    "header_target_unsupported": "响应头断言不支持",
    "jsonpath_filter_unsupported": "JSONPath 过滤表达式不支持",
    "unresolved_variable": "模板变量未解析",
    "runtime_exception": "执行器内部异常",
    "unsupported_assertion": "断言类型不支持",
    "status_assertion_not_numeric": "状态码断言期望不是数字",
    "enrich_schema_invalid": "补全结果未通过校验",
    "server_error": "被测系统 5xx",
    "error_detail_leak": "错误响应泄露实现细节",
    "open_redirect": "开放重定向",
    "policy_violation": "违反项目策略",
    "invalid_expectation": "断言期望不合法",
    "missing_contract": "缺少可执行契约",
    "missing_endpoint": "缺少可调用入口",
    "missing_capability": "缺少可观测能力",
    "disabled_after_pass": "通过后仍被停用",
    "no_failure_evidence": "没有失败证据",
    "unclassified": "规则未命中",
}

# 断言/步骤的合法枚举，用于识别「归一化失败」而不是业务失败
_ASSERTION_KINDS = {
    "status_code",
    "json_path",
    "db_state",
    "cross_api",
    "timing",
    "text_contains",
    "exit_code",
    "stdout_contains",
    "stderr_contains",
}
_COMMAND_KINDS = {"exit_code", "stdout_contains", "stderr_contains"}

_ENVIRONMENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("connection refused", "connection_refused"),
    ("connecterror", "connection_refused"),
    ("connection error", "connection_refused"),
    ("no route to host", "network_unreachable"),
    ("network is unreachable", "network_unreachable"),
    ("name or service not known", "dns_failure"),
    ("temporary failure in name resolution", "dns_failure"),
    ("nodename nor servname", "dns_failure"),
    ("connection reset", "connection_reset"),
    ("readtimeout", "network_timeout"),
    ("connecttimeout", "network_timeout"),
    ("read timeout", "network_timeout"),
    ("connect timeout", "network_timeout"),
    ("timed out", "network_timeout"),
    ("502 bad gateway", "gateway_unavailable"),
    ("503 service unavailable", "gateway_unavailable"),
    ("504 gateway time-out", "gateway_unavailable"),
    ("limactl", "sandbox_unavailable"),
    ("lima ", "sandbox_unavailable"),
    ("沙箱", "sandbox_unavailable"),
)

_PLATFORM_ERROR_MARKERS: tuple[tuple[str, str], ...] = (
    ("illegal header value", "runtime_exception"),
    ("localprotocolerror", "runtime_exception"),
    ("invalidurl", "runtime_exception"),
    ("unsupported assertion", "unsupported_assertion"),
    ("unknown action", "unsupported_assertion"),
    ("拒绝修改未由当前用例创建的默认项目", "runtime_exception"),
)

_LEAK_MARKERS = (
    "sqlstate",
    "goroutine ",
    "panic:",
    "traceback (most recent call last)",
    "org.springframework",
    "java.lang.",
    "stack trace",
)


def _failed_assertions(result: Any) -> list[dict[str, Any]]:
    output = getattr(result, "output", None) or {}
    return [
        item
        for item in output.get("assertions") or []
        if isinstance(item, dict) and not item.get("passed") and not item.get("pending")
    ]


def _steps(case: Any) -> list[dict[str, Any]]:
    spec = getattr(case, "spec", None) or {}
    return [step for step in spec.get("steps") or [] if isinstance(step, dict)]


def _command_for(result: Any, assertion: dict[str, Any]) -> dict[str, Any] | None:
    output = getattr(result, "output", None) or {}
    commands = [item for item in output.get("commands") or [] if isinstance(item, dict)]
    index = assertion.get("step_index")
    for item in reversed(commands):
        if item.get("step_index") == index:
            return item
    return commands[-1] if commands else None


def _step_of(case: Any, assertion: dict[str, Any]) -> dict[str, Any] | None:
    steps = _steps(case)
    index = assertion.get("step_index")
    if isinstance(index, int) and 0 <= index < len(steps):
        return steps[index]
    return steps[-1] if steps else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    return None


def _error_blob(result: Any) -> str:
    """执行错误文本。

    只在 error 结果里带上 logs（traceback 在里面）；fail 结果的 logs 是断言
    明细，可能包含业务文案，拿去匹配「timed out」之类的环境标记会误判。
    """
    output = getattr(result, "output", None) or {}
    parts = [str(item) for item in output.get("errors") or []]
    if str(getattr(result, "status", "") or "") == CaseResultStatus.ERROR.value:
        parts.append(str(getattr(result, "logs", "") or ""))
    return "\n".join(parts).lower()


def _location(result: Any) -> str:
    response = getattr(result, "response", None) or {}
    headers = response.get("headers") or {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "location" and value:
                return str(value)
    return ""


def _body_text(result: Any) -> str:
    response = getattr(result, "response", None) or {}
    body = response.get("body")
    if body is None:
        return ""
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def _result(case: Any, **kwargs: Any) -> dict[str, Any]:
    reason_code = str(kwargs.pop("reason_code"))
    data: dict[str, Any] = {
        "version": RULES_VERSION,
        "category": kwargs.pop("category"),
        "reason_code": reason_code,
        "reason_label": REASON_LABELS.get(reason_code, reason_code),
        "reason": kwargs.pop("reason"),
        "confidence": kwargs.pop("confidence", 0.9),
        "evidence": kwargs.pop("evidence", []),
        "source": kwargs.pop("source", "rules"),
        "is_defect": False,
        "fixable": False,
        "fix": "",
        "case_id": getattr(case, "id", None),
        **kwargs,
    }
    return data


def _defect(case: Any, reason_code: str, reason: str, evidence: list[dict[str, Any]], confidence: float = 0.95) -> dict[str, Any]:
    data = _result(
        case,
        category=CATEGORY_DEFECT,
        reason_code=reason_code,
        reason=reason,
        confidence=confidence,
        evidence=evidence,
    )
    data["is_defect"] = True
    # 5xx / 泄露这类规则结论足够硬，可以自动确认；其余仍留给人工或模型。
    data["auto_confirm"] = confidence >= 0.9
    return data


def classify_result(case: Any, result: Any) -> dict[str, Any] | None:
    """对一条失败结果做确定性归因；规则未命中返回 None（交给模型）。"""
    status = str(getattr(result, "status", "") or "")
    if status not in {CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value}:
        return None
    failed = _failed_assertions(result)
    errors = _error_blob(result)
    response = getattr(result, "response", None) or {}
    try:
        http_status = int(response.get("status_code") or 0)
    except (TypeError, ValueError):
        http_status = 0

    # ── 1. 环境：请求根本没到被测系统 / 沙箱没起来 ──────────────
    for marker, code in _ENVIRONMENT_MARKERS if status == CaseResultStatus.ERROR.value else ():
        if marker in errors:
            return _result(
                case,
                category=CATEGORY_ENVIRONMENT,
                reason_code=code,
                reason={
                    "connection_refused": "请求未到达被测系统（连接被拒绝），用例本身没有被真正执行。",
                    "network_timeout": "请求在到达被测系统前超时，优先检查网络、端口与服务进程。",
                    "dns_failure": "被测系统域名无法解析，属于环境配置问题。",
                    "network_unreachable": "到被测系统的网络不可达，属于环境问题。",
                    "connection_reset": "连接被对端重置，通常是服务未就绪或代理中断。",
                    "gateway_unavailable": "网关返回 5xx，被测服务可能未启动或正在重启。",
                    "sandbox_unavailable": "执行沙箱不可用，用例没有真正跑起来。",
                }.get(code, "环境不可用，用例没有真正执行。"),
                confidence=0.9,
                evidence=[{"type": "log", "source": "result", "detail": marker}],
            )
    if status == CaseResultStatus.ERROR.value and http_status in {502, 503, 504}:
        return _result(
            case,
            category=CATEGORY_ENVIRONMENT,
            reason_code="gateway_unavailable",
            reason=f"被测服务返回 {http_status}，更像网关/上游不可用，而不是接口行为错误。",
            confidence=0.75,
            evidence=[{"type": "status", "source": "result", "detail": f"HTTP {http_status}"}],
        )

    # ── 2. 平台：执行器/归一化/渲染能力不足 ────────────────────
    for marker, code in _PLATFORM_ERROR_MARKERS:
        if marker in errors:
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code=code,
                reason={
                    "runtime_exception": "用例在执行器内部异常中断，失败原因不在被测系统，需要平台侧修复。",
                    "unresolved_variable": "请求或断言里残留未解析的模板变量，执行器没能发出有效请求。",
                    "unsupported_assertion": "执行器不认识这条断言，无法判定结果。",
                }.get(code, "平台无法正确执行这条用例。"),
                confidence=0.92,
                evidence=[{"type": "log", "source": "result", "detail": marker}],
            )

    if "未解析变量" in errors or "未解析的模板变量" in errors or "仍有未解析" in errors:
        return _result(
            case,
            category=CATEGORY_CASE,
            reason_code="unresolved_variable",
            reason="用例引用了从未保存的模板变量（如 {{admin_cookie}}），执行器无法发出有效请求，需要重写这条用例。",
            confidence=0.9,
            evidence=[{"type": "log", "source": "result", "detail": "未解析模板变量"}],
        )

    for assertion in failed:
        kind = str(assertion.get("kind") or "")
        target = str(assertion.get("target") or "")
        step = _step_of(case, assertion)
        action = str((step or {}).get("action") or "")
        expected = assertion.get("expected")
        detail = str(assertion.get("detail") or "")

        if kind not in _ASSERTION_KINDS:
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="unsupported_assertion",
                reason=f"断言类型 {kind!r} 不在执行器支持范围内。",
                confidence=0.95,
                evidence=[{"type": "assertion", "source": "spec", "detail": kind}],
            )

        if action == "command" and kind == "status_code" and _as_int(expected) is None:
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="command_assertion_kind",
                reason=(
                    "命令步骤的断言被归一化成 status_code，执行器拿退出码 0 去比文本期望"
                    f"「{expected}」，属于平台断言归一化缺陷，不是用例逻辑错误。"
                ),
                confidence=0.97,
                evidence=[
                    {
                        "type": "assertion",
                        "source": "spec",
                        "detail": f"command + status_code + expected={expected!r}",
                    }
                ],
                fixable=True,
                fix="command_assertion_kind",
                retry_after_fix=True,
            )

        if action == "command" and kind == "text_contains":
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="command_assertion_kind",
                reason="命令步骤的输出断言被写成了 text_contains，比较对象应是 stdout 而不是响应体。",
                confidence=0.95,
                evidence=[{"type": "assertion", "source": "spec", "detail": "command + text_contains"}],
                fixable=True,
                fix="command_assertion_kind",
                retry_after_fix=True,
            )

        if action == "command" and kind in {"stdout_contains", "stderr_contains"}:
            command = _command_for(result, assertion)
            if command is not None:
                expected_text = str(expected)
                own = str(command.get("stdout" if kind == "stdout_contains" else "stderr") or "")
                other_stream = "stderr" if kind == "stdout_contains" else "stdout"
                other = str(command.get(other_stream) or "")
                if expected_text and expected_text not in own and expected_text in other:
                    return _result(
                        case,
                        category=CATEGORY_PLATFORM,
                        reason_code="command_assertion_stream",
                        reason=(
                            f"断言在 {'stdout' if kind == 'stdout_contains' else 'stderr'} 里找 "
                            f"「{expected_text}」，但这段文本只出现在 {other_stream}，属于断言输出流写错。"
                        ),
                        confidence=0.96,
                        evidence=[
                            {
                                "type": "command",
                                "source": "result",
                                "detail": f"{kind} 未命中，{other_stream} 命中",
                            }
                        ],
                        fixable=True,
                        fix="command_assertion_stream",
                        retry_after_fix=True,
                    )

        if (
            kind == "text_contains"
            and target.lower().startswith("headers.")
            and assertion.get("target_kind") != "header"
        ):
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="header_target_unsupported",
                reason=f"断言要检查响应头 {target}，但执行器的 text_contains 只比对响应体，响应头实际值为空。",
                confidence=0.95,
                evidence=[
                    {
                        "type": "assertion",
                        "source": "spec",
                        "detail": f"target={target} actual={assertion.get('actual')!r}",
                    }
                ],
                fixable=True,
                fix="header_target",
                retry_after_fix=True,
            )

        if kind == "json_path" and "[?(" in target:
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="jsonpath_filter_unsupported",
                reason=f"断言使用 JSONPath 过滤表达式 {target}，执行器只支持普通路径，无法定位字段。",
                confidence=0.95,
                evidence=[{"type": "assertion", "source": "spec", "detail": target}],
                fixable=False,
            )

        if "{{" in json.dumps([expected, target], ensure_ascii=False, default=str):
            return _result(
                case,
                category=CATEGORY_CASE,
                reason_code="unresolved_variable",
                reason="断言里仍有未渲染的模板变量，判定结果不可信，需要重写这条用例。",
                confidence=0.9,
                evidence=[{"type": "assertion", "source": "spec", "detail": f"{target} / {expected!r}"}],
            )

        if (
            kind == "status_code"
            and str(assertion.get("operator") or "eq") in {"in", "not_in"}
            and isinstance(expected, (list, tuple))
        ):
            # in / not_in 的期望本来就是状态码集合，不是「期望不是数字」。
            continue

        if kind == "status_code" and action != "command" and _as_int(expected) is None:
            convertible = str(assertion.get("operator") or "eq") in {"contains", "not_contains", "matches"}
            return _result(
                case,
                category=CATEGORY_PLATFORM,
                reason_code="status_assertion_not_numeric",
                reason=f"状态码断言的期望值不是数字（{expected!r}），执行器无法做出有意义的判定。",
                confidence=0.95,
                evidence=[{"type": "assertion", "source": "spec", "detail": detail or str(expected)}],
                fixable=convertible,
                fix="status_assertion_kind" if convertible else "",
                retry_after_fix=convertible,
            )

    # ── 3. 缺陷：5xx 或实现细节泄露 ───────────────────────────
    if http_status >= 500:
        return _defect(
            case,
            "server_error",
            f"被测系统返回 {http_status}，服务端异常属于疑似产品缺陷。",
            [{"type": "status", "source": "result", "detail": f"HTTP {http_status}"}],
        )
    for assertion in failed:
        actual = _as_int(assertion.get("actual"))
        if str(assertion.get("kind")) == "status_code" and actual is not None and actual >= 500:
            return _defect(
                case,
                "server_error",
                f"被测系统返回 {actual}，服务端异常属于疑似产品缺陷。",
                [{"type": "status", "source": "result", "detail": f"HTTP {actual}"}],
            )
    location = _location(result)
    if location and (location.startswith("//") or re.match(r"(?i)^https?://", location)):
        return _defect(
            case,
            "open_redirect",
            f"跳转响应把用户带到外部地址 {location!r}，浏览器会离开本站，属于开放重定向。",
            [{"type": "header", "source": "result", "detail": f"Location: {location}"}],
            confidence=0.98,
        )

    body = _body_text(result)
    leak = next((marker for marker in _LEAK_MARKERS if marker in body), "")
    if leak and http_status >= 400:
        return _defect(
            case,
            "error_detail_leak",
            f"错误响应里出现 {leak!r}，疑似泄露堆栈或数据库实现细节。",
            [{"type": "body", "source": "result", "detail": leak}],
            confidence=0.9,
        )

    # 策略断言（policies.py）是项目级通用要求：失败即缺陷，不需要再猜。
    for assertion in failed:
        evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
        if str(evidence.get("source_type") or "") == "policy":
            ref = str(evidence.get("ref") or "").strip()
            return _defect(
                case,
                "policy_violation",
                f"违反项目策略 {ref or '（未命名）'}：{assertion.get('detail') or assertion.get('description') or ''}"[:500],
                [
                    {
                        "type": "policy",
                        "source": "assertion",
                        "detail": ref or str(assertion.get("description") or ""),
                    }
                ],
                confidence=0.95,
            )

    # ── 4. 用例：期望本身不合法（能确定的那种） ───────────────
    for assertion in failed:
        if str(assertion.get("kind")) != "status_code":
            continue
        expected = _as_int(assertion.get("expected"))
        if expected is not None and not (100 <= expected <= 599):
            return _result(
                case,
                category=CATEGORY_CASE,
                reason_code="invalid_expectation",
                reason=f"断言期望状态码 {expected} 不在合法范围，用例本身写错了。",
                confidence=0.9,
                evidence=[{"type": "assertion", "source": "spec", "detail": str(expected)}],
            )

    return None


def classify_static(case: Any) -> dict[str, Any]:
    """没有执行结果时，根据用例自身状态解释「为什么它跑不了」。"""
    tags = {str(tag).lower() for tag in (getattr(case, "tags", None) or [])}
    missing = str(getattr(case, "missing_input", "") or "").strip()
    verification = getattr(case, "verification", None) or {}
    auto = verification.get("auto_validation") if isinstance(verification, dict) else None
    auto_status = str((auto or {}).get("status") or "")

    if auto_status == "pass" and str(getattr(case, "status", "")) == "disabled":
        return _result(
            case,
            category=CATEGORY_UNCONFIRMED,
            reason_code="disabled_after_pass",
            reason="最近一次执行已经通过，用例仍处于停用状态，需要确认是不是人工保留。",
            confidence=0.6,
            source="static",
        )
    if "schema 校验" in missing or "补全结果未通过" in missing:
        return _result(
            case,
            category=CATEGORY_PLATFORM,
            reason_code="enrich_schema_invalid",
            reason="自动补全的结果没有通过用例 schema 校验，平台未能修好这条用例。"
            + (f"（{missing[:160]}）" if missing else ""),
            confidence=0.85,
            source="static",
            evidence=[{"type": "missing_input", "source": "case", "detail": missing[:400]}],
        )
    if {"missing-endpoint", "unmapped"} & tags or any(
        marker in missing for marker in ("无对应接口", "无接口可测", "没有可调用", "缺少 HTTP 接口")
    ):
        return _result(
            case,
            category=CATEGORY_EVIDENCE,
            reason_code="missing_endpoint",
            reason="接口清单里没有可调用的入口，构造不出接口级用例。" + (f"（{missing[:160]}）" if missing else ""),
            confidence=0.9,
            source="static",
            evidence=[{"type": "missing_input", "source": "case", "detail": missing[:400]}],
        )
    if "missing-capability" in tags:
        return _result(
            case,
            category=CATEGORY_EVIDENCE,
            reason_code="missing_capability",
            reason="缺少可观测能力或调试入口，无法在用例内等待/确认结果。" + (f"（{missing[:160]}）" if missing else ""),
            confidence=0.9,
            source="static",
            evidence=[{"type": "missing_input", "source": "case", "detail": missing[:400]}],
        )
    if missing or {"missing-input", "unsupported", "missing-channel"} & tags:
        return _result(
            case,
            category=CATEGORY_EVIDENCE,
            reason_code="missing_contract",
            reason="缺少可执行契约，用例只能停在说明层。" + (f"（{missing[:200]}）" if missing else ""),
            confidence=0.85,
            source="static",
            evidence=[{"type": "missing_input", "source": "case", "detail": missing[:400]}],
        )
    return _result(
        case,
        category=CATEGORY_UNCONFIRMED,
        reason_code="no_failure_evidence",
        reason="没有失败结果，也没有缺口说明，无法判断停用原因。",
        confidence=0.3,
        source="static",
    )


def classify(case: Any, result: Any | None = None) -> dict[str, Any]:
    """优先用失败结果做规则归因，其次用用例自身的缺口说明。"""
    if result is not None:
        verdict = classify_result(case, result)
        if verdict is not None:
            return verdict
        if str(getattr(result, "status", "") or "") in {
            CaseResultStatus.FAIL.value,
            CaseResultStatus.ERROR.value,
        }:
            static = classify_static(case)
            # 缺口说明能解释失败时保留；否则老实标成「规则未命中」，不硬扣帽子。
            if static.get("category") == CATEGORY_EVIDENCE:
                static["confidence"] = min(float(static.get("confidence") or 0.6), 0.6)
                static["reason"] = f"{static['reason']}（该用例执行失败，缺口可能是直接原因）"
                return static
            return _result(
                case,
                category=CATEGORY_UNCONFIRMED,
                reason_code="unclassified",
                reason="规则没有命中已知的环境、平台、缺陷特征，需要模型或人工判断。",
                confidence=0.2,
            )
    return classify_static(case)


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    """按类别汇总，供 run.stats / 报表使用。"""
    summary = {category: 0 for category in CATEGORY_LABELS}
    for row in rows:
        category = str((row or {}).get("category") or CATEGORY_UNCONFIRMED)
        summary[category] = summary.get(category, 0) + 1
    return summary


__all__ = [
    "CATEGORY_CASE",
    "CATEGORY_DEFECT",
    "CATEGORY_ENVIRONMENT",
    "CATEGORY_EVIDENCE",
    "CATEGORY_LABELS",
    "CATEGORY_PLATFORM",
    "CATEGORY_UNCONFIRMED",
    "REASON_LABELS",
    "RULES_VERSION",
    "classify",
    "classify_result",
    "classify_static",
    "summarize",
]
