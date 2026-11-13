"""Shakedown 执行器运行时（被生成到沙箱 /workspace 里执行）。

职责：
- 按统一用例 JSON 执行 HTTP 步骤与浏览器步骤；
- 判定断言（status_code / json_path / db_state / cross_api / timing / text_contains）；
- 「待确认」断言只记录不判失败；
- 把结果写成 JSON，供平台收集。

本文件是**模板**：由 ``app.executor.compiler`` 连同用例一起写进沙箱。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import hashlib
import time
import traceback
import uuid
from typing import Any

import httpx

RESULTS_DIR = os.environ.get("SHAKEDOWN_RESULTS_DIR", "results")
BASE_URL = os.environ.get("SHAKEDOWN_BASE_URL", "http://127.0.0.1:8000")
TARGET_DSN = os.environ.get("SHAKEDOWN_TARGET_DSN", "")
ACCOUNTS = json.loads(os.environ.get("SHAKEDOWN_ACCOUNTS", "{}") or "{}")

# 结果会被落库、进报告、贴进缺陷复现命令：账号口令 / 令牌绝不能原样留下。
_SECRET_VALUES = sorted(
    {
        str(account.get(key) or "")
        for account in ACCOUNTS.values()
        if isinstance(account, dict)
        for key in ("password", "token")
    }
    - {""},
    key=len,
    reverse=True,
)
_SECRET_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "private-token",
    "authorization",
    "cookie",
    "set-cookie",
}


def _redact_payload(value: Any, *, keys: bool = False) -> Any:
    """把请求/响应里的凭据替换成 ***；只影响落库副本，不影响真实请求。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if keys and str(key).lower() in _SECRET_KEYS:
                out[key] = "***"
            else:
                out[key] = _redact_payload(item, keys=keys)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item, keys=keys) for item in value]
    if isinstance(value, str):
        text = value
        for secret in _SECRET_VALUES:
            if secret and secret in text:
                text = text.replace(secret, "***")
        return text
    return value
ARTIFACTS_DIR = os.environ.get("SHAKEDOWN_ARTIFACTS_DIR", "artifacts")
REQUEST_TIMEOUT = float(os.environ.get("SHAKEDOWN_REQUEST_TIMEOUT", "30"))
STREAM_MAX_SECONDS = float(os.environ.get("SHAKEDOWN_STREAM_MAX_SECONDS", "5"))
STREAM_MAX_BYTES = int(os.environ.get("SHAKEDOWN_STREAM_MAX_BYTES", "65536"))

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class _BoundedResponse:
    """最小响应对象：只保留执行器需要的字段。"""

    def __init__(self, *, status_code: int, headers: dict[str, str], text: str, url: str):
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self.url = url

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def _bounded_request(
    client: "httpx.Client",
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: Any,
    json_body: Any,
    data: Any = None,
    content: Any = None,
    timeout: float = 30.0,
) -> _BoundedResponse:
    """发请求并限制读取时长 / 字节数。

    SSE、NDJSON 这类「连接不关、持续推送」的响应如果直接 read，会一直等到
    沙箱 600s 超时，整批用例全部卡死。这里只读一小段就主动断开：
    状态码和首个事件足够做「流是否可用」的判断，也让后续用例继续执行。
    """
    started = time.time()
    with client.stream(
        method, url, headers=headers, params=params, json=json_body, data=data, content=content, timeout=timeout
    ) as response:
        content_type = str(response.headers.get("content-type", "")).lower()
        streaming = "text/event-stream" in content_type or "application/x-ndjson" in content_type
        if streaming:
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= STREAM_MAX_BYTES or time.time() - started >= STREAM_MAX_SECONDS:
                    break
            text = b"".join(chunks).decode("utf-8", errors="replace")
        else:
            encoding = response.encoding or "utf-8"
            text = response.read().decode(encoding, errors="replace")
        return _BoundedResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=text,
            url=str(response.url),
        )


class CaseContext:
    """一次用例执行的上下文。"""

    def __init__(self, case: dict[str, Any]):
        self.case = case
        self.case_id = str(case.get("id", ""))
        self.variables: dict[str, Any] = {
            "run_id": os.environ.get("SHAKEDOWN_RUN_ID", "run"),
            "uuid": uuid.uuid4().hex[:12],
            "timestamp": str(int(time.time())),
        }
        self.variables["base_url"] = BASE_URL
        project_id = os.environ.get("SHAKEDOWN_PROJECT_ID", "").strip()
        if project_id:
            # 未自己创建项目的用例可以直接使用被测项目 ID；如果用例里
            # save 了 project_id，会覆盖这个默认值。
            self.variables["project_id"] = project_id
        for name, value in (case.get("variables") or {}).items():
            if not isinstance(name, str) or not name:
                continue
            self.variables[name] = self.render(str(value)) if isinstance(value, str) else value
        self.default_variables = set(self.variables) - {"run_id", "uuid", "timestamp"}
        self.saved_variables: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        # 步骤索引 → 该步骤产生的请求/响应；失败时顶层结果要指向真正失败的步骤，
        # 不能被 cleanup 的 DELETE 覆盖。
        self.step_requests: dict[int, dict[str, Any]] = {}
        self.step_responses: dict[int, dict[str, Any]] = {}
        self.step_duration_samples: dict[int, list[float]] = {}
        self.last_duration_ms: float = 0.0
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.assertion_failed = False
        # 顶层断言绑定到「带内联断言的那一步」的响应上（而不是最后一步）
        self.assertion_response: dict[str, Any] | None = None
        # 同一条用例内复用 client：登录拿到的 Cookie 后续步骤要能带上
        self.client = httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)
        self.last_command: dict[str, Any] | None = None
        # 所有命令步骤的完整结果：归因要靠它判断「断言查错了输出流」。
        self.commands: list[dict[str, Any]] = []

    def _account_value(self, key: str) -> Any:
        parts = key.split(".")
        if len(parts) < 2:
            return ""
        account = ACCOUNTS.get(parts[1], {})
        field = parts[2] if len(parts) >= 3 else "username"
        return account.get(field, "")

    # ── 变量渲染 ─────────────────────────────────────────
    def render(self, value: Any) -> Any:
        if isinstance(value, str):
            exact = _PLACEHOLDER.fullmatch(value.strip())
            if exact:
                key = exact.group(1)
                if key in self.variables:
                    # 字符串整体就是一个占位符时保留原类型：数组/数字/布尔
                    # 不能先 str()，否则 finding_ids、case_ids、project_id
                    # 会被发给接口成字符串，稳定触发 422。
                    return self.variables[key]
                if key.startswith("account."):
                    return self._account_value(key)

            def repl(match: re.Match[str]) -> str:
                key = match.group(1)
                if key in self.variables:
                    return str(self.variables[key])
                if key.startswith("account."):
                    return str(self._account_value(key))
                return match.group(0)

            return _PLACEHOLDER.sub(repl, value)
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        return value


def _json_path(data: Any, path: str) -> tuple[bool, Any]:
    """支持 ``$.a.b[0].c``、``$[*].id`` 形式的路径。"""
    if not path or path in {"$", ""}:
        return True, data
    expression = path.strip()
    if expression.startswith("$"):
        expression = expression[1:]
    if expression.startswith("."):
        expression = expression[1:]
    tokens = [
        (key or None, index or None)
        for key, index in re.findall(r"([^.\[\]]+)|\[(\d+|\*)\]", expression)
    ]

    def walk(current: Any, remaining: list[tuple[str | None, str | None]]) -> tuple[bool, Any]:
        if not remaining:
            return True, current
        key, index = remaining[0]
        rest = remaining[1:]
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                return False, None
            return walk(current[key], rest)
        if index == "*":
            if isinstance(current, list):
                values = []
                found_any = False
                for item in current:
                    found, value = walk(item, rest)
                    if found:
                        found_any = True
                        values.append(value)
                return True, values if found_any else []
            if isinstance(current, dict):
                values = []
                for item in current.values():
                    found, value = walk(item, rest)
                    if found:
                        values.append(value)
                return True, values
            return False, None
        if not isinstance(current, list) or not index.isdigit() or int(index) >= len(current):
            return False, None
        return walk(current[int(index)], rest)

    return walk(data, tokens)


def _assertion_key(assertion: dict[str, Any]) -> str:
    """断言语义指纹：顶层与步骤内联写同一检查时只判定一次。"""
    semantic = {key: assertion.get(key) for key in ("kind", "target", "operator", "expected")}
    return json.dumps(semantic, sort_keys=True, ensure_ascii=False, default=str)


def _as_comparable(value: Any) -> Any:
    """把模型常见的字符串 ID / 数字 ID 统一成同一种标量。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", text):
            return float(text)
        if text.lower() in {"true", "false"}:
            return text.lower() == "true"
    return value


def _compare(operator: str, actual: Any, expected: Any) -> bool:
    operator = (operator or "eq").lower()
    try:
        if operator in {"eq", "ne"}:
            left, right = _as_comparable(actual), _as_comparable(expected)
            same = (
                abs(float(left) - float(right)) < 1e-6
                if isinstance(left, (int, float))
                and not isinstance(left, bool)
                and isinstance(right, (int, float))
                and not isinstance(right, bool)
                else left == right
            )
            return same if operator == "eq" else not same
        if operator == "in":
            return actual in expected or _as_comparable(actual) in {
                _as_comparable(item) for item in expected
            }
        if operator == "not_in":
            return not (actual in expected or _as_comparable(actual) in {
                _as_comparable(item) for item in expected
            })
        if operator == "lt":
            return float(actual) < float(expected)
        if operator == "le":
            return float(actual) <= float(expected)
        if operator == "gt":
            return float(actual) > float(expected)
        if operator == "ge":
            return float(actual) >= float(expected)
        if operator == "contains":
            return str(expected) in str(actual)
        if operator == "not_contains":
            return str(expected) not in str(actual)
        if operator == "exists":
            return actual is not None
        if operator == "not_exists":
            return actual is None
        if operator == "matches":
            return re.search(str(expected), str(actual)) is not None
        if operator == "empty":
            return actual in (None, "", [], {})
    except (TypeError, ValueError):
        return False
    return False


def _db_fetch(sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    if not TARGET_DSN:
        raise RuntimeError("未配置被测库 DSN，无法执行数据库断言")
    import psycopg  # type: ignore

    with psycopg.connect(TARGET_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return list(cur.fetchall())


def _write_result(result: dict[str, Any]) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"case_{result.get('case_id', 'unknown')}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=str)


def _percentile(values: list[float], percentile: float) -> float:
    """线性插值计算百分位；用于 timing 多次采样的 P95/P99。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _timing_metrics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "min": 0.0}
    return {
        "samples": len(values),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "max": max(values),
        "min": min(values),
    }


def _evaluate_assertion(
    assertion: dict[str, Any],
    ctx: CaseContext,
    response: dict[str, Any] | None = None,
    durations: list[float] | None = None,
) -> dict[str, Any]:
    """返回 ``{"passed": bool, "detail": str, "pending": bool}``。

    断言里的 ``expected`` / ``target`` / ``sql`` 等字段和请求一样支持
    ``{{run_id}}`` / ``{{saved_var}}`` 占位符。必须在**判定前**渲染，
    否则请求体已经替换成 ``MYMAC_TEST_15``，而期望值还停留在
    ``MYMAC_TEST_{{run_id}}``，会稳定误报失败。
    """
    assertion = ctx.render(assertion)
    kind = assertion.get("kind")
    operator = assertion.get("operator", "eq")
    expected = assertion.get("expected")
    status = assertion.get("status", "pending")
    evidence = assertion.get("evidence") or {}
    pending = status != "confirmed" or evidence.get("source_type") in (None, "", "none")

    if response is None:
        response = ctx.assertion_response or (ctx.responses[-1] if ctx.responses else {})

    if kind in {"exit_code", "stdout_contains", "stderr_contains"}:
        command = ctx.last_command or {}
        if kind == "exit_code":
            actual = command.get("exit_code")
            passed = _compare(operator, actual, expected)
            detail = f"命令退出码 {actual}（期望 {expected}）"
        elif kind == "stdout_contains":
            actual = str(command.get("stdout") or "")
            # 历史断言常把 operator 留成 eq，但语义一直是「包含」；只有显式
            # not_contains / matches 才按对应语义判定。
            op = operator if operator in {"not_contains", "matches"} else "contains"
            passed = _compare(op, actual, expected)
            detail = (
                f"stdout 包含 {expected!r}"
                if passed
                else f"stdout 不含 {expected!r}，实际：{actual[-300:]!r}"
            )
            if operator == "not_contains":
                detail = f"stdout 不含 {expected!r}" if passed else f"stdout 仍包含 {expected!r}"
        else:
            actual = str(command.get("stderr") or "")
            op = operator if operator in {"not_contains", "matches"} else "contains"
            passed = _compare(op, actual, expected)
            detail = (
                f"stderr 包含 {expected!r}"
                if passed
                else f"stderr 不含 {expected!r}，实际：{actual[-300:]!r}"
            )
            if operator == "not_contains":
                detail = f"stderr 不含 {expected!r}" if passed else f"stderr 仍包含 {expected!r}"
        return {
            "passed": bool(passed),
            "pending": bool(pending),
            "detail": detail,
            "kind": kind,
            "expected": expected,
            "actual": actual,
            "description": assertion.get("description", ""),
            "evidence": evidence,
            "operator": operator,
            "target": assertion.get("target", ""),
            "metrics": {},
        }

    detail = ""
    passed = False
    actual: Any = None
    metrics: dict[str, Any] | None = None
    target_kind = ""
    try:
        if kind == "status_code":
            actual = response.get("status_code")
            passed = _compare(operator, actual, expected)
            detail = f"实际状态码 {actual}，期望 {expected}"
        elif kind == "json_path":
            body = response.get("body")
            target = assertion.get("target", "")
            found, value = _json_path(body, target)
            # 兼容 FastAPI 默认错误体与项目统一错误体的差异：
            # 生成用例常按 OpenAPI 的 HTTPValidationError 写 $.detail，
            # 实际项目可能返回 {"error": {"code", "message", "details"}}。
            if not found and str(target).startswith("$.detail"):
                suffix = str(target)[len("$.detail") :]
                aliases = ["$.error.details" + suffix] if suffix.startswith("[") else ["$.error" + suffix]
                if isinstance(expected, str) and not suffix:
                    aliases = ["$.error.message", "$.error.details", "$.error"]
                for alias in aliases:
                    alias_found, alias_value = _json_path(body, alias)
                    if alias_found:
                        found, value = True, alias_value
                        break
            if operator == "exists":
                passed = found and value is not None
            elif operator == "not_exists":
                passed = not found or value is None
            else:
                passed = found and _compare(operator, value, expected)
                if (
                    found
                    and not passed
                    and isinstance(value, dict)
                    and not isinstance(expected, (dict, list))
                ):
                    # 模型常把 IsolationConfig / TargetStart 这类对象当成
                    # 字符串断言；优先比较对象里真正承载语义的标量字段。
                    for key in ("mode", "type", "code", "message", "status"):
                        if key in value and _compare(operator, value[key], expected):
                            passed = True
                            break
            detail = f"{assertion.get('target')} → {value!r}（期望 {expected!r}）"
            actual = value if found else None
        elif kind == "text_contains":
            target = str(assertion.get("target") or "")
            if target.lower().startswith("headers."):
                target_kind = "header"
                header_name = target.split(".", 1)[1].strip().lower()
                headers = response.get("headers") or {}
                value = next(
                    (item for key, item in headers.items() if str(key).lower() == header_name),
                    None,
                )
                actual = value
                passed = value is not None and _compare(operator, str(value), expected)
                detail = (
                    f"响应头 {header_name} 包含 {expected!r}"
                    if passed
                    else f"响应头 {header_name}={value!r}，期望包含 {expected!r}"
                )
            else:
                body = response.get("body")
                passed = _compare(operator, str(body), expected)
                actual = body
                detail = f"响应体包含 {expected!r}"
        elif kind == "db_state":
            sql = assertion.get("sql") or ""
            params = [ctx.render(p) for p in (assertion.get("params") or [])]
            rows = _db_fetch(sql, params)
            if not rows:
                passed = operator in {"empty", "not_exists"}
                detail = "查询无结果行"
            else:
                actual = rows[0][0] if len(rows[0]) == 1 else [list(r) for r in rows]
                passed = _compare(operator, actual, expected)
                detail = f"数据库实际值 {actual!r}，期望 {expected!r}"
        elif kind == "timing":
            threshold = float(assertion.get("threshold_ms") or expected or 0)
            samples = list(durations or [ctx.last_duration_ms])
            metrics = _timing_metrics(samples)
            actual = metrics["p95"]
            passed = actual <= threshold
            detail = (
                f"P95 {actual:.1f}ms，阈值 {threshold}ms"
                f"（n={metrics['samples']}, P50={metrics['p50']:.1f}ms, "
                f"P99={metrics['p99']:.1f}ms, max={metrics['max']:.1f}ms）"
            )
        elif kind == "cross_api":
            request = ctx.render(assertion.get("request") or {})
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                response = client.request(
                    request.get("method", "GET"),
                    request.get("path", "/"),
                    headers=request.get("headers") or {},
                    params=request.get("query") or {},
                    json=request.get("body"),
                )
            found, value = _json_path(response.json() if response.content else None, assertion.get("target", ""))
            passed = found and _compare(operator, value, expected)
            actual = value if found else None
            detail = f"跨接口校验 {assertion.get('target')} → {value!r}（期望 {expected!r}）"
        else:
            detail = f"未知断言类型：{kind}"
            passed = False
    except Exception as exc:  # noqa: BLE001
        detail = f"断言执行异常：{type(exc).__name__}: {exc}"
        passed = False

    # policy 断言在失败详情里带上策略 ID，方便复现和审计。
    if evidence.get("source_type") == "policy" and evidence.get("ref"):
        detail = f"[{evidence['ref']}] {detail}"

    return {
        "passed": bool(passed),
        "pending": bool(pending),
        "detail": detail,
        "kind": kind,
        "expected": expected,
        "actual": actual,
        "description": assertion.get("description", ""),
        "evidence": evidence,
        "operator": operator,
        "target": assertion.get("target", ""),
        "target_kind": target_kind,
        "metrics": metrics,
    }


def _annotate_step(outcome: dict[str, Any], step: Any, index: int) -> None:
    """给断言结果补上步骤信息，执行详情里才能区分「第几步的哪个请求」。"""
    outcome["step_index"] = index
    if isinstance(step, dict) and step.get("action") == "request":
        outcome["step_method"] = (step.get("method") or "GET").upper()
        outcome["step_path"] = step.get("path") or ""


def _effective_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {}
        for item in schema["allOf"]:
            merged.update(_effective_schema(item))
        return merged
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for item in branches:
                branch = _effective_schema(item)
                if branch.get("type") != "null":
                    return branch
    return schema


def _schema_type(schema: Any) -> str:
    schema = _effective_schema(schema)
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return ""


_DROP = object()


def _coerce_value(value: Any, schema: Any, *, required: bool = False) -> Any:
    """按端点 schema 纠正渲染后的值；不能安全纠正的可选字段直接丢弃。"""
    if value is None:
        return _DROP if not required else None
    schema = _effective_schema(schema)
    expected_type = _schema_type(schema)

    if expected_type == "object":
        properties = schema.get("properties") or {}
        if isinstance(value, list):
            value = {} if not all(isinstance(item, dict) and len(item) == 1 for item in value) else {
                str(next(iter(item))): next(iter(item.values())) for item in value
            }
        elif isinstance(value, str):
            if value.strip().startswith("{") and value.strip().endswith("}"):
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        value = parsed
                except json.JSONDecodeError:
                    pass
            if not isinstance(value, dict):
                if "mode" in properties:
                    value = {"mode": value}
                elif "type" in properties:
                    value = {"type": value}
                else:
                    value = {}
        if not isinstance(value, dict):
            return {} if not required else value
        output = {}
        required_fields = set(schema.get("required") or [])
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties", True) is not False:
                    output[key] = item
                continue
            normalized = _coerce_value(item, properties[key], required=key in required_fields)
            if normalized is not _DROP:
                output[key] = normalized
        return output

    if expected_type == "array":
        if not isinstance(value, list):
            value = [value]
        output = []
        for item in value:
            normalized = _coerce_value(item, schema.get("items") or {}, required=True)
            if normalized is not _DROP:
                output.append(normalized)
        return output

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return _DROP if not required else value
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str):
        try:
            if re.search(pattern, value) is None:
                return _DROP if not required else value
        except re.error:
            pass

    if expected_type == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
            return int(value.strip())
        return _DROP if not required else value
    if expected_type == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return _DROP
        return _DROP if not required else value
    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        return _DROP if not required else value
    if expected_type == "string" and not isinstance(value, str):
        if isinstance(value, (int, float, bool)):
            return str(value)
        return _DROP if not required else value
    return value


def _normalize_request_payload(value: Any, schema: Any) -> Any:
    normalized = _coerce_value(value, schema or {}, required=True)
    return value if normalized is _DROP else normalized


def _uses_unsaved_default(step: dict[str, Any], ctx: CaseContext) -> bool:
    path = str(step.get("path") or "")
    return any(
        name in ctx.default_variables and name not in ctx.saved_variables
        for name in _PLACEHOLDER.findall(path)
    )


def _service_exec(ref: str, command: list[str], timeout: float) -> dict[str, Any]:
    """在 berth 服务的 namespace 里执行 CLI（沙箱里没有 limactl 时使用）。"""
    base = os.environ.get("SHAKEDOWN_SERVICE_EXEC_URL", "").rstrip("/")
    token = os.environ.get("SHAKEDOWN_SERVICE_EXEC_TOKEN", "")
    if not base:
        raise RuntimeError("未配置 SHAKEDOWN_SERVICE_EXEC_URL")
    started = time.time()
    with httpx.Client(timeout=max(5.0, min(timeout, 65.0))) as client:
        response = client.post(
            f"{base}/services/{ref}/exec",
            json={"cmd": command, "timeout": int(max(1, min(timeout, 60)))},
            headers={"Authorization": f"Bearer {token}", "X-Berth-Token": token},
        )
    if response.status_code != 200:
        raise RuntimeError(f"berth exec 失败：{response.status_code} {response.text[:200]}")
    data = response.json()
    return {
        "command": command,
        "exit_code": int(data.get("exit_code", -1)),
        "stdout": str(data.get("stdout") or ""),
        "stderr": str(data.get("stderr") or ""),
        "duration_ms": float(data.get("duration_ms") or (time.time() - started) * 1000),
    }


def _run_command_step(step: dict[str, Any], ctx: CaseContext) -> dict[str, Any]:
    """执行 CLI 步骤（命令 + 参数），把 exit_code/stdout/stderr 作为响应体。"""
    raw = step.get("command") or []
    command = [str(ctx.render(item)) for item in raw] if isinstance(raw, list) else shlex.split(str(ctx.render(raw)))
    if not command:
        raise RuntimeError("command 步骤缺少 command")
    timeout = float(step.get("timeout_ms") or 120000) / 1000
    cwd = str(ctx.render(step.get("cwd") or "")) or None
    env = {**os.environ}
    for key, value in (step.get("env") or {}).items():
        env[str(key)] = str(ctx.render(value))
    started = time.time()
    service_ref = os.environ.get("SHAKEDOWN_SERVICE_EXEC_REF", "")
    if service_ref and command[:2] == ["limactl", "shell"]:
        inner = command[command.index("--") + 1 :] if "--" in command else command[2:]
        result = _service_exec(service_ref, inner, timeout)
        ctx.last_command = result
        ctx.commands.append(result)
        ctx.responses.append(
            {"status_code": result["exit_code"], "body": result, "duration_ms": result["duration_ms"]}
        )
        ctx.requests.append({"method": "SERVICE-EXEC", "url": " ".join(inner), "headers": {}})
        return result
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": (completed.stdout or "")[-12000:],
        "stderr": (completed.stderr or "")[-12000:],
        "duration_ms": (time.time() - started) * 1000,
    }
    ctx.last_command = result
    ctx.commands.append(result)
    ctx.responses.append({"status_code": result["exit_code"], "body": result, "duration_ms": result["duration_ms"]})
    ctx.requests.append({"method": "CLI", "url": " ".join(command), "headers": {}})
    return result


def _run_parallel_step(step: dict[str, Any], ctx: CaseContext) -> dict[str, Any]:
    """并发发出同一请求，用于验证竞态 / 幂等（例如同时创建同名对象）。

    每个请求用独立 client，不共享 Cookie；结果聚合成
    ``{"statuses": [...], "errors": [...]}``，供 json_path 断言判定。
    """
    from concurrent.futures import ThreadPoolExecutor

    spec = step.get("parallel") or {}
    request = spec.get("request") or {}
    count = max(2, min(int(spec.get("count") or 3), 10))
    method = str(request.get("method") or "POST").upper()
    path = ctx.render(request.get("path") or "/")
    base = str(request.get("base_url") or BASE_URL).rstrip("/")
    url = path if path.startswith("http") else base + path
    headers = {
        str(key): str(value).strip()
        for key, value in (ctx.render(request.get("headers") or {}) or {}).items()
        if value is not None
    }
    body = ctx.render(request.get("body"))
    is_form = "application/x-www-form-urlencoded" in str(
        headers.get("Content-Type") or headers.get("content-type") or ""
    ).lower()
    started = time.time()

    def fire(_index: int) -> tuple[int, str]:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
            try:
                response = client.request(
                    method,
                    url,
                    headers=headers,
                    json=None if is_form else (body if isinstance(body, (dict, list)) else None),
                    data=body if is_form and isinstance(body, dict) else None,
                    content=body if isinstance(body, str) and not is_form else None,
                )
                return response.status_code, response.text[:300]
            except Exception as exc:  # noqa: BLE001
                return 0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(fire, range(count)))
    statuses = sorted(status for status, _ in results)
    payload = {
        "statuses": statuses,
        "errors": [message for status, message in results if status == 0],
    }
    duration_ms = (time.time() - started) * 1000
    ctx.last_duration_ms = duration_ms
    ctx.requests.append({"method": "PARALLEL", "url": url, "headers": {}, "query": {}, "body": body})
    ctx.responses.append(
        {"status_code": 200, "headers": {}, "body": payload, "duration_ms": duration_ms}
    )
    return payload


def _cookie_flags(value: str) -> str:
    """保留 Set-Cookie 的安全属性，丢掉值（值就是会话凭据）。"""
    parts = [item.strip() for item in str(value).split(";") if item.strip()]
    flags = [
        item
        for item in parts[1:]
        if item.lower() in {"httponly", "secure", "samesite=lax", "samesite=strict", "samesite=none"}
    ]
    return "<redacted>" + ("; " + "; ".join(flags) if flags else "")


def _perform_request(step: dict[str, Any], ctx: CaseContext) -> dict[str, Any]:
    method = (step.get("method") or "GET").upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and _uses_unsaved_default(step, ctx):
        raw_path = str(step.get("path") or "")
        # 写操作尤其不能落到自测/生产项目默认 ID 上；尤其是 findings
        # 批量状态/自动验证，会把自测数据写脏，cleanup 也未必能恢复。
        if method == "DELETE" or "/findings" in raw_path:
            raise RuntimeError(
                "拒绝修改未由当前用例创建的默认项目；"
                "请先创建独立测试项目，或显式移除此写操作"
            )
    path = ctx.render(step.get("path") or "/")
    # 路径里残留占位符几乎总是上游 save 失败 / 变量名拼错。
    # 直接报清楚，避免把 {{created_name}} 原样发给被测系统后收到莫名其妙的 404。
    unresolved = sorted(set(_PLACEHOLDER.findall(path)))
    if unresolved:
        raise RuntimeError(
            "请求路径仍有未解析变量："
            + "、".join(unresolved)
            + "（上游步骤可能失败，或 save 的变量名不一致）"
        )
    base_url = step.get("base_url") or BASE_URL
    rendered_headers = ctx.render(step.get("headers") or {})
    headers = {
        str(key): str(value).strip()
        for key, value in (rendered_headers.items() if isinstance(rendered_headers, dict) else [])
        if value is not None
    }
    params = ctx.render(step.get("query") or {})
    body = ctx.render(step.get("body")) if step.get("body") is not None else None
    request_schema = step.get("__request_schema") or {}
    if isinstance(params, dict):
        normalized_params = {}
        for key, value in params.items():
            normalized = _coerce_value(
                value,
                (request_schema.get("params") or {}).get(key) or {},
                required=False,
            )
            if normalized is not _DROP:
                normalized_params[key] = normalized
        params = normalized_params
    if body is not None and request_schema.get("body"):
        body = _normalize_request_payload(body, request_schema["body"])
    auth = step.get("auth") or {}
    if not auth.get("account") and auth.get("username"):
        inline_user = str(ctx.render(auth.get("username")))
        inline_pass = str(ctx.render(auth.get("password") or ""))
        if inline_user and inline_pass:
            raw = f"{inline_user}:{inline_pass}".encode()
            headers.setdefault("Authorization", "Basic " + base64.b64encode(raw).decode())
    if auth.get("account"):
        account = ACCOUNTS.get(auth["account"], {})
        token = str(account.get("token") or "")
        username = str(account.get("username") or "")
        password = str(account.get("password") or "")
        mode = str(auth.get("mode") or "").lower()
        if mode in {"private_token", "private-token", "gitlab"} and token:
            headers.setdefault("PRIVATE-TOKEN", token)
        elif mode in {"basic", "password"} and username:
            raw = f"{username}:{password}".encode()
            headers.setdefault("Authorization", "Basic " + base64.b64encode(raw).decode())
        elif token:
            headers.setdefault("Authorization", f"Bearer {token}")
        elif username and password:
            raw = f"{username}:{password}".encode()
            headers.setdefault("Authorization", "Basic " + base64.b64encode(raw).decode())
        elif username:
            headers.setdefault("X-Test-Account", username)
    # 请求头 / 请求体里残留占位符几乎总是账号或 save 变量没配上；
    # 直接发出去会把字面量 {{token}} 当凭据，最后得到莫名其妙的 401。
    unresolved_values = sorted(
        set(_PLACEHOLDER.findall(json.dumps(headers, ensure_ascii=False, default=str)))
        | set(_PLACEHOLDER.findall(json.dumps(body, ensure_ascii=False, default=str)))
    )
    if unresolved_values:
        raise RuntimeError(
            "请求头/请求体仍有未解析变量："
            + "、".join(unresolved_values)
            + "（检查账号配置或上游 save）"
        )
    url = path if path.startswith("http") else base_url.rstrip("/") + path
    started = time.time()
    timeout = float(step.get("timeout_ms") or REQUEST_TIMEOUT * 1000) / 1000
    # fresh_client：这一步用全新的 cookie 罐，不共享本用例已有的登录态。
    # 验证「改密后旧会话应失效」「管理员身份不应被用户会话覆盖」都需要它。
    temp_client = None
    if step.get("fresh_client"):
        temp_client = httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)
    client = temp_client or ctx.client
    is_form = "application/x-www-form-urlencoded" in str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
    try:
        response = _bounded_request(
            client,
            method,
            url,
            headers=headers,
            params=params,
            json_body=None if is_form else (body if isinstance(body, (dict, list)) else None),
            data=body if is_form and isinstance(body, dict) else None,
            content=body if isinstance(body, str) and not is_form else None,
            timeout=timeout,
        )
        # 生成器有时漏掉 /api 前缀；只在首请求 404 时尝试一次，
        # 不覆盖真实业务 404（此时 path 已带 /api）。
        if (
            response.status_code == 404
            and path.startswith("/")
            and not path.startswith("/api/")
        ):
            retry_path = "/api" + path
            retry_url = base_url.rstrip("/") + retry_path
            retry = _bounded_request(
                client,
                method,
                retry_url,
                headers=headers,
                params=params,
                json_body=None if is_form else (body if isinstance(body, (dict, list)) else None),
                data=body if is_form and isinstance(body, dict) else None,
                content=body if isinstance(body, str) and not is_form else None,
                timeout=timeout,
            )
            if retry.status_code != 404:
                response = retry
                path = retry_path
                url = retry_url
    finally:
        if temp_client is not None:
            temp_client.close()
    duration_ms = (time.time() - started) * 1000
    ctx.last_duration_ms = duration_ms
    raw_body = response.content
    content_type = str(response.headers.get("content-type") or "").lower()
    binary = (
        b"\x00" in raw_body[:4096]
        or content_type.startswith(("application/octet-stream", "application/zip", "application/gzip", "application/x-gzip", "application/x-tar"))
    )
    if binary:
        # 二进制响应不能塞进 Postgres 的 JSON（\u0000 无法转换），只留可核对摘要。
        parsed = {
            "binary": True,
            "size": len(raw_body),
            "sha256": hashlib.sha256(raw_body).hexdigest()[:16],
            "content_type": content_type,
        }
    else:
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            parsed = response.text
    request_payload = _redact_payload(
        {"method": method, "url": url, "headers": headers, "query": params, "body": body},
        keys=True,
    )
    response_payload = _redact_payload(
        {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": parsed,
            "duration_ms": duration_ms,
        }
    )
    if isinstance(response_payload.get("headers"), dict):
        for header_name in list(response_payload["headers"]):
            if str(header_name).lower() == "set-cookie":
                response_payload["headers"][header_name] = _cookie_flags(
                    str(response_payload["headers"][header_name])
                )
    ctx.requests.append(request_payload)
    ctx.responses.append(response_payload)
    # 保存变量
    for name, path_expr in (step.get("save") or {}).items():
        found, value = _json_path(parsed, path_expr)
        if found:
            ctx.variables[name] = value
            if value is not None:
                ctx.saved_variables.add(name)
    return response_payload


def _perform_browser(step: dict[str, Any], ctx: CaseContext) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright  # type: ignore

    browser_steps = step.get("browser") or {}
    actions = browser_steps.get("steps") or []
    base_url = (browser_steps.get("base_url") or BASE_URL).rstrip("/")
    # 登录态复用：把 storage_state 存在 workspace 里，后续用例直接复用，避免每条都登录
    state_file = os.environ.get("SHAKEDOWN_BROWSER_STATE", os.path.join(ARTIFACTS_DIR, "browser_state.json"))
    started = time.time()
    output: dict[str, Any] = {"screenshot": None}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=bool(browser_steps.get("headless", True)))
        context_args: dict[str, Any] = {}
        if os.path.exists(state_file):
            context_args["storage_state"] = state_file
        context = browser.new_context(**context_args)
        page = context.new_page()
        try:
            for action in actions:
                op = (action.get("op") or "").lower()
                if op == "goto":
                    target = ctx.render(action.get("url", "/"))
                    page.goto(target if target.startswith("http") else base_url + target)
                elif op == "fill":
                    page.fill(action.get("selector", ""), str(ctx.render(action.get("value", ""))))
                elif op == "click":
                    page.click(action.get("selector", ""))
                elif op == "press":
                    page.press(action.get("selector", ""), action.get("key", "Enter"))
                elif op == "wait_for":
                    page.wait_for_selector(action.get("selector", ""), timeout=int(action.get("timeout_ms", 10000)))
                elif op == "wait_ms":
                    page.wait_for_timeout(int(action.get("ms", 500)))
                elif op == "text":
                    text = page.inner_text(action.get("selector", ""))
                    ctx.variables[action.get("save_as", "text")] = text
                elif op == "login":
                    account = ACCOUNTS.get(action.get("account", ""), {})
                    page.fill(action.get("username_selector", ""), str(account.get("username", "")))
                    page.fill(action.get("password_selector", ""), str(account.get("password", "")))
                    page.click(action.get("submit_selector", ""))
                    page.wait_for_load_state()
                    # 登录成功后保存登录态，供后续用例复用
                    try:
                        os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
                        context.storage_state(path=state_file)
                    except Exception:  # noqa: BLE001
                        pass
            a11y = page.evaluate(
                """() => {
                  const images = [...document.images].filter((img) => !img.getAttribute('alt')).length;
                  const buttons = [...document.querySelectorAll('button')].filter((button) => {
                    const text = (button.innerText || button.textContent || '').trim();
                    return !text && !button.getAttribute('aria-label') && !button.getAttribute('title');
                  }).length;
                  const inputs = [...document.querySelectorAll('input,select,textarea')].filter((field) => {
                    if (field.getAttribute('aria-label') || field.getAttribute('aria-labelledby') ||
                        field.getAttribute('title') || field.getAttribute('placeholder')) return false;
                    const id = field.id;
                    if (id && document.querySelector(`label[for="${CSS.escape(id)}"]`)) return false;
                    return !field.closest('label');
                  }).length;
                  const links = [...document.querySelectorAll('a')].filter((link) => {
                    const text = (link.innerText || link.textContent || '').trim();
                    return !text && !link.getAttribute('aria-label') && !link.getAttribute('title') &&
                           !link.querySelector('img[alt]:not([alt=""])');
                  }).length;
                  const ids = [...document.querySelectorAll('[id]')].map((node) => node.id);
                  const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
                  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .map((node) => Number(node.tagName.slice(1)));
                  const headingJumps = headings.filter((level, index) =>
                    index > 0 && level > headings[index - 1] + 1
                  ).length;
                  return {
                    images_without_alt: images,
                    buttons_without_label: buttons,
                    inputs_without_label: inputs,
                    links_without_text: links,
                    duplicate_ids: [...new Set(duplicateIds)].slice(0, 20),
                    heading_jumps: headingJumps,
                    has_main_landmark: Boolean(document.querySelector('main,[role="main"]')),
                    has_title: Boolean(document.title),
                    html_lang: document.documentElement.lang || ''
                  };
                }"""
            )
            output["a11y"] = a11y
            ctx.responses.append(
                {
                    "status_code": page.url and 200 or 0,
                    "body": {"url": page.url, "title": page.title(), "a11y": a11y},
                    "duration_ms": 0,
                }
            )
        finally:
            os.makedirs(ARTIFACTS_DIR, exist_ok=True)
            shot = os.path.join(ARTIFACTS_DIR, f"case_{ctx.case_id}.png")
            try:
                page.screenshot(path=shot, full_page=True)
                output["screenshot"] = shot
                with open(shot, "rb") as handle:
                    output["screenshot_sha256"] = hashlib.sha256(handle.read()).hexdigest()
            except Exception:  # noqa: BLE001
                pass
            browser.close()
    ctx.last_duration_ms = (time.time() - started) * 1000
    return output


def run_case(case: dict[str, Any]) -> None:
    """执行一条用例（由 pytest 调用；失败时抛出 AssertionError）。"""
    ctx = CaseContext(case)
    started = time.time()
    status = "pass"
    screenshot: str | None = None
    error: str | None = None
    assertion_results: list[dict[str, Any]] = []
    failure_step_index: int | None = None
    current_step_index: int | None = None

    try:
        # 没有步骤的用例（例如「缺少输入」）→ 标记为 skipped，不判失败
        if not (case.get("steps") or []):
            result = {
                "case_id": case.get("id"),
                "title": case.get("title"),
                "status": "skipped",
                "duration_ms": 0.0,
                "request": {},
                "requests": [],
                "response": {},
                "responses": [],
                "logs": "用例缺少可执行步骤（通常是缺少输入），已跳过",
                "screenshot_path": None,
                "output": {
                    "assertion_failed": False,
                    "errors": [],
                    "assertions": [],
                    "pending_assertions": [],
                    "missing_input": case.get("missing_input", ""),
                },
                "error": None,
            }
            _write_result(result)
            return

        # 前置条件
        for precondition in case.get("preconditions") or []:
            kind = precondition.get("kind")
            if kind == "http_login":
                account = ACCOUNTS.get(precondition.get("account_ref") or "default", {})
                token = account.get("token")
                if token:
                    ctx.variables["auth_token"] = token
                    ctx.requests.append({"method": "LOGIN", "url": "account:" + str(precondition.get("account_ref")), "headers": {}})
            elif kind == "db_fixture" and precondition.get("sql"):
                try:
                    _db_fetch(precondition["sql"], [])
                except Exception as exc:  # noqa: BLE001
                    ctx.errors.append(f"前置 SQL 执行失败：{exc}")

        # 步骤
        inline_assertion_keys: set[str] = set()
        for index, step in enumerate(case.get("steps") or []):
            current_step_index = index
            action = step.get("action")
            requests_before = len(ctx.requests)
            responses_before = len(ctx.responses)
            if action == "request":
                try:
                    samples = max(1, min(int(step.get("samples") or 1), 1000))
                except (TypeError, ValueError):
                    samples = 1
                durations: list[float] = []
                for _sample in range(samples):
                    _perform_request(step, ctx)
                    durations.append(ctx.last_duration_ms)
                ctx.step_duration_samples[index] = durations
            elif action == "browser":
                result = _perform_browser(step, ctx)
                screenshot = result.get("screenshot")
            elif action == "db":
                rows = _db_fetch(ctx.render(step.get("sql", "")), [ctx.render(p) for p in (step.get("params") or [])])
                ctx.responses.append({"status_code": 0, "body": {"rows": [list(r) for r in rows]}, "duration_ms": 0})
            elif action == "sleep":
                time.sleep(float(step.get("seconds") or 1))
            elif action == "command":
                _run_command_step(step, ctx)
            elif action == "parallel":
                _run_parallel_step(step, ctx)
                if ctx.commands:
                    ctx.commands[-1]["step_index"] = index

            if len(ctx.responses) > responses_before:
                ctx.step_responses[index] = ctx.responses[-1]
            if len(ctx.requests) > requests_before:
                ctx.step_requests[index] = ctx.requests[-1]

            for assertion in step.get("assert") or []:
                inline_assertion_keys.add(_assertion_key(assertion))
                step_response = ctx.responses[-1] if ctx.responses else {}
                outcome = _evaluate_assertion(
                    assertion,
                    ctx,
                    step_response,
                    ctx.step_duration_samples.get(index),
                )
                _annotate_step(outcome, step, index)
                assertion_results.append(outcome)
                if not outcome["passed"] and not outcome["pending"]:
                    ctx.assertion_failed = True
                    if failure_step_index is None:
                        failure_step_index = index
                    ctx.errors.append(f"断言失败：{outcome['detail']}")
                if ctx.assertion_response is None:
                    # 顶层断言绑定到「第一个带内联断言的步骤」的响应
                    ctx.assertion_response = step_response

        # 顶层断言：优先用 step_index 绑定到对应步骤的响应；
        # 与步骤内联重复的同一检查不再重复判定。
        for assertion in case.get("assertions") or []:
            if _assertion_key(assertion) in inline_assertion_keys:
                continue
            step_index = assertion.get("step_index")
            bound = ctx.step_responses.get(step_index) if isinstance(step_index, int) else None
            bound_durations = ctx.step_duration_samples.get(step_index) if isinstance(step_index, int) else None
            outcome = _evaluate_assertion(assertion, ctx, bound, bound_durations)
            if isinstance(step_index, int):
                steps = case.get("steps") or []
                step = steps[step_index] if 0 <= step_index < len(steps) else None
                _annotate_step(outcome, step, step_index)
            assertion_results.append(outcome)
            if not outcome["passed"] and not outcome["pending"]:
                ctx.assertion_failed = True
                if failure_step_index is None and isinstance(step_index, int):
                    failure_step_index = step_index
                ctx.errors.append(f"断言失败：{outcome['detail']}")

        if ctx.assertion_failed:
            status = "fail"
        else:
            status = "pass"
    except AssertionError as exc:
        status = "fail"
        error = str(exc)
        if failure_step_index is None:
            failure_step_index = current_step_index
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        if failure_step_index is None:
            failure_step_index = current_step_index
        ctx.errors.append(traceback.format_exc(limit=5))

    # 日志里给一条执行结论，避免用例通过时「日志」页完全空白、无法判断是否正常
    pending_count = sum(1 for item in assertion_results if item.get("pending"))
    if status == "pass":
        if assertion_results:
            summary = f"用例通过：{len(assertion_results)} 条断言通过"
            if pending_count:
                summary += f"，{pending_count} 条待确认（未判失败）"
        else:
            summary = "用例通过：没有断言记录（仅执行步骤）"
        ctx.notes.append(summary)
    elif status == "fail":
        failed_count = sum(
            1 for item in assertion_results if not item.get("passed") and not item.get("pending")
        )
        ctx.notes.append(f"用例失败：{failed_count}/{len(assertion_results)} 条断言失败")

    # 顶层 request/response 必须指向真正失败的步骤；cleanup 的 DELETE 不能覆盖它。
    if status in {"fail", "error"} and failure_step_index is not None:
        top_request = ctx.step_requests.get(failure_step_index, ctx.requests[-1] if ctx.requests else {})
        top_response = ctx.step_responses.get(failure_step_index, ctx.responses[-1] if ctx.responses else {})
    else:
        top_request = ctx.requests[-1] if ctx.requests else {}
        top_response = ctx.responses[-1] if ctx.responses else {}

    # 清理步骤（失败也要执行）
    for step in case.get("cleanup") or []:
        try:
            if step.get("action") == "request":
                if _uses_unsaved_default(step, ctx) and str(step.get("method") or "").upper() == "DELETE":
                    ctx.notes.append("跳过清理：未创建当前用例自己的项目，不能删除默认项目")
                    continue
                _perform_request(step, ctx)
            elif step.get("action") == "db" and step.get("sql"):
                _db_fetch(ctx.render(step["sql"]), [ctx.render(p) for p in (step.get("params") or [])])
        except Exception as exc:  # noqa: BLE001
            ctx.errors.append(f"清理步骤失败：{exc}")

    try:
        ctx.client.close()
    except Exception:  # noqa: BLE001
        pass

    result = {
        "case_id": case.get("id"),
        "title": case.get("title"),
        "status": status,
        "duration_ms": (time.time() - started) * 1000,
        "request": top_request,
        "requests": ctx.requests,
        "response": top_response,
        "responses": ctx.responses,
        "failed_step_index": failure_step_index,
        "logs": "\n".join(ctx.notes + ctx.errors),
        "screenshot_path": screenshot,
        "output": {
            "assertion_failed": ctx.assertion_failed,
            "errors": ctx.errors,
            "assertions": assertion_results,
            "pending_assertions": [a for a in assertion_results if a.get("pending")],
            "commands": ctx.commands[-8:],
            "requests": ctx.requests,
            "responses": ctx.responses,
            "failed_step_index": failure_step_index,
        },
        "error": error,
    }
    _write_result(result)

    if status == "fail":
        raise AssertionError("; ".join(ctx.errors) or "断言失败")
    if status == "error":
        raise RuntimeError(error or "执行异常")
