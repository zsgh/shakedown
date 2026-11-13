"""探索式执行层：差分 / 变形 / fuzz 的独立观测入口。

只对项目 base_url 发请求，不允许任意绝对 URL；写方法需要显式 allow_write。
返回 observed 差异 / 5xx / 泄露信号，不直接生成硬断言或 confirmed finding。
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project

_LEAK_MARKERS = ("traceback", "sqlstate", "panic:", "stack trace", "stacktrace", "exception:")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _url(project: Project, path: str) -> str:
    value = str(path or "").strip()
    if value.startswith(("http://", "https://")):
        raise ValueError("探索式执行不允许绝对 URL，只能使用项目 base_url + path")
    if not value.startswith("/"):
        value = "/" + value
    return str(project.base_url or "").rstrip("/") + value


def _request(project: Project, method: str, path: str, *, query: dict | None = None, body: Any = None, headers: dict | None = None, timeout: float = 10.0) -> dict[str, Any]:
    import httpx

    started = time.perf_counter()
    response = httpx.request(
        method.upper(),
        _url(project, path),
        params=query or None,
        json=body if body is not None else None,
        headers=headers or None,
        timeout=timeout,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001
        parsed = response.text
    return {
        "status_code": response.status_code,
        "body": parsed,
        "text": response.text[:4000] if isinstance(parsed, str) else "",
        "headers": dict(response.headers),
        "duration_ms": elapsed_ms,
    }


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return ["list"] if value else []
    return type(value).__name__


def _difference(a: dict[str, Any], b: dict[str, Any], *, left: str, right: str) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    if a.get("status_code") != b.get("status_code"):
        differences.append({"kind": "status_code", "left": a.get("status_code"), "right": b.get("status_code")})
    if isinstance(a.get("body"), dict) and isinstance(b.get("body"), dict):
        left_keys = set(a["body"].keys())
        right_keys = set(b["body"].keys())
        if left_keys != right_keys:
            differences.append({"kind": "body_keys", "left": sorted(left_keys), "right": sorted(right_keys)})
        for key in left_keys & right_keys:
            if _shape(a["body"][key]) != _shape(b["body"][key]):
                differences.append({"kind": "field_shape", "field": key, "left": _shape(a["body"][key]), "right": _shape(b["body"][key])})
    elif _shape(a.get("body")) != _shape(b.get("body")):
        differences.append({"kind": "body_shape", "left": _shape(a.get("body")), "right": _shape(b.get("body"))})
    return [{"between": [left, right], **item} for item in differences]


def run_differential(project: Project, *, method: str, path: str, variants: list[dict[str, Any]], headers: dict | None = None) -> dict[str, Any]:
    if len(variants) < 2:
        raise ValueError("differential 至少需要两个 variants")
    observations: list[dict[str, Any]] = []
    for index, variant in enumerate(variants[:10]):
        item = variant if isinstance(variant, dict) else {}
        observations.append(
            {
                "name": str(item.get("name") or f"variant-{index + 1}"),
                "query": item.get("query") or {},
                "response": _request(
                    project,
                    method,
                    path,
                    query=item.get("query") or {},
                    body=item.get("body"),
                    headers={**(headers or {}), **(item.get("headers") or {})},
                ),
            }
        )
    differences: list[dict[str, Any]] = []
    for left in range(len(observations)):
        for right in range(left + 1, len(observations)):
            differences.extend(
                _difference(
                    observations[left]["response"],
                    observations[right]["response"],
                    left=observations[left]["name"],
                    right=observations[right]["name"],
                )
            )
    return {
        "mode": "differential",
        "method": method.upper(),
        "path": path,
        "observations": [
            {"name": item["name"], "status_code": item["response"]["status_code"], "body_shape": _shape(item["response"]["body"])}
            for item in observations
        ],
        "differences": differences,
    }


def run_fuzz(
    project: Project,
    *,
    method: str,
    path: str,
    payloads: list[Any],
    headers: dict | None = None,
) -> dict[str, Any]:
    if method.upper() not in _SAFE_METHODS and not payloads:
        raise ValueError("fuzz 写方法需要显式 payloads")
    observations: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for index, payload in enumerate(payloads[:30]):
        response = _request(project, method, path, body=payload, headers=headers)
        text = (response.get("text") or "").lower()
        leaked = [marker for marker in _LEAK_MARKERS if marker in text]
        observation = {
            "index": index,
            "status_code": response["status_code"],
            "body_shape": _shape(response.get("body")),
            "leak_markers": leaked,
            "server_error": int(response["status_code"]) >= 500,
        }
        observations.append(observation)
        if observation["server_error"]:
            signals.append({**observation, "signal": "5xx"})
        if leaked:
            signals.append({**observation, "signal": "possible_information_leak"})
    return {
        "mode": "fuzz",
        "method": method.upper(),
        "path": path,
        "observations": observations,
        "signals": signals,
    }


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _get_path(body: Any, path: str) -> Any:
    current = body
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def _eval_property(prop: dict[str, Any], observation: dict[str, Any]) -> tuple[bool, Any]:
    kind = str(prop.get("kind") or prop.get("type") or "").strip()
    body = observation.get("body")
    if kind in {"status_in", "status_code_in"}:
        allowed = [int(item) for item in (prop.get("values") or prop.get("value") or [])]
        return observation["status_code"] in allowed, {"allowed": allowed, "actual": observation["status_code"]}
    if kind == "status_between":
        low, high = int(prop.get("min", 200)), int(prop.get("max", 299))
        return low <= int(observation["status_code"]) <= high, {"min": low, "max": high, "actual": observation["status_code"]}
    if kind == "body_has_keys":
        keys = [str(item) for item in (prop.get("keys") or [])]
        missing = [key for key in keys if not isinstance(body, dict) or key not in body]
        return not missing, {"keys": keys, "missing": missing}
    if kind == "body_missing_keys":
        keys = [str(item) for item in (prop.get("keys") or [])]
        present = [key for key in keys if isinstance(body, dict) and key in body]
        return not present, {"keys": keys, "present": present}
    if kind in {"body_type", "json_path_type"}:
        field = str(prop.get("field") or prop.get("path") or "")
        expected = str(prop.get("expected") or prop.get("json_type") or "")
        actual = _json_type(_get_path(body, field))
        return actual == expected, {"field": field, "expected": expected, "actual": actual}
    if kind in {"json_path_equals", "field_equals"}:
        field = str(prop.get("field") or prop.get("path") or "")
        expected = prop.get("expected")
        actual = _get_path(body, field)
        return actual == expected, {"field": field, "expected": expected, "actual": actual}
    if kind in {"latency_le", "max_latency_ms"}:
        limit = float(prop.get("limit_ms") or prop.get("value") or 0)
        actual = float(observation.get("duration_ms") or 0)
        return actual <= limit, {"limit_ms": limit, "actual_ms": actual}
    if kind in {"no_server_error", "not_5xx"}:
        return int(observation["status_code"]) < 500, {"actual": observation["status_code"]}
    if kind == "repeat_equal":
        # 由 run_property 单独比较，这里只占位。
        return True, {}
    return False, {"reason": f"未知 property kind：{kind}"}


def run_property(
    project: Project,
    *,
    method: str,
    path: str,
    properties: list[dict[str, Any]] | None = None,
    cases: list[dict[str, Any]] | None = None,
    headers: dict | None = None,
    generator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """属性测试：对一组输入执行 HTTP 请求，并检查显式属性定义。

    支持 ``status_in`` / ``body_has_keys`` / ``body_type`` /
    ``json_path_equals`` / ``latency_le`` / ``no_server_error`` /
    ``repeat_equal``。失败会返回反例，不做模糊断言。
    """
    from app.test_data import generate_rows

    definitions = [item for item in (properties or []) if isinstance(item, dict)]
    if not definitions:
        raw = (project.settings_json or {}).get("exploratory_properties")
        if isinstance(raw, list):
            definitions = [item for item in raw if isinstance(item, dict)]
    if not definitions:
        raise ValueError("property 模式需要 properties 或项目 settings.exploratory_properties")

    case_list = [item for item in (cases or []) if isinstance(item, dict)]
    if generator:
        fields = generator.get("fields") if isinstance(generator.get("fields"), list) else None
        rows = generate_rows(
            project,
            fields=fields,
            count=int(generator.get("count", 5)),
            seed=int(generator["seed"]) if generator.get("seed") is not None else None,
            unicode=bool(generator.get("unicode", False)),
            distribution=str(generator.get("distribution") or "uniform"),
        )
        target = str(generator.get("target") or "body")
        for index, row in enumerate(rows):
            case_list.append(
                {
                    "name": f"generated-{index + 1}",
                    "query": row if target == "query" else {},
                    "body": row if target != "query" else None,
                }
            )
    if not case_list:
        case_list = [{"name": "default"}]
    case_list = case_list[:20]

    observations: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, case in enumerate(case_list):
        name = str(case.get("name") or f"case-{index + 1}")
        case_headers = {**(headers or {}), **(case.get("headers") or {})}
        first = _request(
            project,
            method,
            path,
            query=case.get("query") or None,
            body=case.get("body"),
            headers=case_headers,
        )
        observation = {
            "name": name,
            "status_code": first["status_code"],
            "body": first["body"],
            "body_shape": _shape(first["body"]),
            "duration_ms": first["duration_ms"],
        }
        observations.append(observation)
        for prop in definitions[:20]:
            prop_name = str(prop.get("name") or prop.get("kind") or "property")
            if str(prop.get("kind") or prop.get("type")) == "repeat_equal":
                times = max(2, min(int(prop.get("times", 2)), 5))
                repeated = [
                    _request(
                        project,
                        method,
                        path,
                        query=case.get("query") or None,
                        body=case.get("body"),
                        headers=case_headers,
                    )
                    for _ in range(times - 1)
                ]
                expected = (first["status_code"], first["body"])
                mismatch = [
                    {"status_code": item["status_code"], "body": item["body"]}
                    for item in repeated
                    if (item["status_code"], item["body"]) != expected
                ]
                passed = not mismatch
                actual = {"expected": {"status_code": expected[0], "body": expected[1]}, "mismatch": mismatch}
            else:
                passed, actual = _eval_property(prop, observation)
            if not passed:
                failures.append(
                    {
                        "case": name,
                        "property": prop_name,
                        "kind": str(prop.get("kind") or prop.get("type") or ""),
                        "actual": actual,
                    }
                )
    return {
        "mode": "property",
        "method": method.upper(),
        "path": path,
        "property_count": len(definitions),
        "case_count": len(case_list),
        "passed": not failures,
        "failures": failures[:200],
        "observations": observations,
        "definitions": definitions,
    }


def run_metamorphic(
    project: Project,
    *,
    method: str,
    path: str,
    body: Any,
    read_path: str,
    id_field: str = "id",
    compare_fields: list[str] | None = None,
    headers: dict | None = None,
) -> dict[str, Any]:
    if "{id}" not in read_path:
        raise ValueError("metamorphic 的 read_path 必须包含 {id}")
    create = _request(project, method, path, body=body, headers=headers)
    create_body = create.get("body") if isinstance(create.get("body"), dict) else {}
    identifier = create_body.get(id_field)
    if identifier is None:
        return {
            "mode": "metamorphic",
            "create": {"status_code": create["status_code"], "body_shape": _shape(create.get("body"))},
            "read": None,
            "differences": [{"kind": "missing_id", "field": id_field}],
        }
    read = _request(project, "GET", read_path.replace("{id}", str(identifier)), headers=headers)
    read_body = read.get("body") if isinstance(read.get("body"), dict) else {}
    fields = compare_fields or [key for key in create_body.keys() if key != id_field][:10]
    differences: list[dict[str, Any]] = []
    for field in fields:
        if create_body.get(field) != read_body.get(field):
            differences.append(
                {
                    "kind": "field_mismatch",
                    "field": field,
                    "create": create_body.get(field),
                    "read": read_body.get(field),
                }
            )
    return {
        "mode": "metamorphic",
        "create": {"status_code": create["status_code"], "body_shape": _shape(create.get("body"))},
        "read": {"status_code": read["status_code"], "body_shape": _shape(read.get("body"))},
        "identifier": identifier,
        "compared_fields": fields,
        "differences": differences,
    }
