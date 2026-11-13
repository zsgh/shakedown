"""测试数据生成：schema + 字段名语义 + 约束/关联/分布策略。

生成器只做确定性、可复现的数据构造，不访问网络、不写库。执行链路通过
``app.test_data_service`` 把数据集行渲染成 ``{{data.*}}`` 变量。
"""
from __future__ import annotations

import hashlib
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models import Endpoint, Project

# 字段名命中这些语义时，按 PII 处理（仅标记，不阻止生成）。
_PII_HINTS = ("email", "phone", "mobile", "name", "address", "id_card", "ssn", "passport", "bank", "card")
_AMOUNT_HINTS = ("amount", "price", "total", "balance", "cost", "fee", "salary")
_DATE_FORMATS = {"date": "%Y-%m-%d", "date-time": "%Y-%m-%dT%H:%M:%SZ", "time": "%H:%M:%S"}


def _field_type(field: dict[str, Any]) -> str:
    kind = field.get("type") or "string"
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    return str(kind)


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """解析本地 ``$ref``（#/$defs/X、#/components/schemas/X）；循环引用返回空 schema。"""
    if depth > 8 or not isinstance(schema, dict):
        return schema if isinstance(schema, dict) else {}
    ref = schema.get("$ref")
    if not ref or not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    if not isinstance(node, dict):
        return {}
    merged = {**node, **{k: v for k, v in schema.items() if k != "$ref"}}
    return _resolve_ref(merged, root, depth + 1)


def _merge_all_of(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    variants = schema.get("allOf")
    if not isinstance(variants, list):
        return schema
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    for item in variants:
        resolved = _resolve_ref(item if isinstance(item, dict) else {}, root)
        for key, value in resolved.items():
            if key == "properties" and isinstance(value, dict):
                merged.setdefault("properties", {})
                merged["properties"].update(value)
            elif key == "required" and isinstance(value, list):
                merged["required"] = list(dict.fromkeys([*(merged.get("required") or []), *value]))
            else:
                merged.setdefault(key, value)
    return merged


def _pick_variant(schema: dict[str, Any], index: int, root: dict[str, Any]) -> dict[str, Any]:
    schema = _resolve_ref(schema, root)
    schema = _merge_all_of(schema, root)
    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            chosen = _resolve_ref(variants[index % len(variants)], root)
            merged = {k: v for k, v in schema.items() if k not in {"oneOf", "anyOf"}}
            merged.update(chosen)
            return merged
    return schema


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_number(field: dict[str, Any], rng: random.Random, distribution: str) -> Any:
    minimum = field.get("minimum", 1)
    maximum = field.get("maximum", 1000)
    low, high = _as_int(minimum, 1), _as_int(maximum, 1000)
    if high < low:
        low, high = high, low
    if field.get("exclusiveMinimum") is not None:
        low = max(low, _as_int(field.get("exclusiveMinimum"), low) + 1)
    if field.get("exclusiveMaximum") is not None:
        high = min(high, _as_int(field.get("exclusiveMaximum"), high) - 1)
    if high < low:
        high = low
    if distribution == "normal":
        mean = (low + high) / 2
        sigma = max(0.1, (high - low) / 6)
        value = max(low, min(high, int(rng.gauss(mean, sigma))))
    else:
        value = rng.randint(low, high)
    multiple = field.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple:
        value = int(round(value / multiple) * multiple)
        value = max(low, min(high, value))
    if _field_type(field) == "number":
        decimals = field.get("decimals", 2)
        return round(float(value), int(decimals))
    return int(value)


def _string_value(field: dict[str, Any], index: int, rng: random.Random, *, namespace: str) -> str:
    name = str(field.get("name") or "").lower()
    fmt = str(field.get("format") or "").lower()
    prefix = namespace or "{{run_id}}"

    if name == "uuid" or fmt == "uuid":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{prefix}:{index}:{name}"))
    if "email" in name or fmt == "email":
        return f"shakedown-{prefix}-{index}@example.com"
    if "phone" in name or "mobile" in name:
        return f"138{rng.randint(10000000, 99999999)}"
    if "url" in name or "link" in name or fmt in {"uri", "url"}:
        return f"https://example.com/items/{index}"
    if fmt in _DATE_FORMATS:
        if field.get("relative_days") is not None:
            day = datetime.now(timezone.utc) + timedelta(days=_as_int(field.get("relative_days"), 0))
            return day.strftime(_DATE_FORMATS[fmt])
        return "2026-01-01T00:00:00Z" if fmt == "date-time" else "2026-01-01"
    if "date" in name or name.endswith("_at") or "time" in name:
        return "2026-01-01T00:00:00Z"
    if "status" in name or "state" in name:
        return str(field.get("default") or "active")
    if "name" in name or "title" in name:
        base = f"shakedown-{prefix}-{name}-{index}"
        return base
    if "description" in name or "remark" in name or "note" in name:
        return f"Shakedown test data {index}"
    if "address" in name:
        return f"{index} Shakedown Test Road"
    if field.get("pattern"):
        # 只对常见简单 pattern 做确定性采样；复杂 pattern 交给 minimum length 兜底。
        pattern = str(field["pattern"])
        simple = re.fullmatch(r"\^\[([A-Za-z0-9_-]+)\]\{(\d+)(?:,(\d+))?\}\$", pattern)
        if simple:
            alphabet = simple.group(1)
            length = _as_int(simple.group(2), 1)
            return "".join(alphabet[(index + offset) % len(alphabet)] for offset in range(length))
        return "x" * max(1, _as_int(field.get("minLength"), 1))
    result = f"shakedown-{prefix}-{name or 'value'}-{index}"
    minimum_length = _as_int(field.get("minLength"), 0)
    maximum_length = _as_int(field.get("maxLength"), 0)
    if minimum_length and len(result) < minimum_length:
        result += "x" * (minimum_length - len(result))
    if maximum_length and len(result) > maximum_length:
        result = result[:maximum_length]
    return result


def _value(
    field: dict[str, Any],
    index: int,
    rng: random.Random,
    *,
    unicode: bool = False,
    distribution: str = "uniform",
    root: dict[str, Any] | None = None,
    namespace: str = "",
    negative: bool = False,
    depth: int = 0,
) -> Any:
    root = root or field
    field = _pick_variant(field, index, root)
    name = str(field.get("name") or "").lower()
    kind = _field_type(field)
    enum = field.get("enum")
    if isinstance(enum, list) and enum:
        return enum[index % len(enum)]
    if "const" in field:
        return field.get("const")
    if field.get("nullable") and index % 7 == 3:
        return None

    if negative and index % 3 == 0:
        if kind in {"integer", "number"}:
            return -1 if not field.get("minimum") or _as_int(field.get("minimum"), 0) <= 0 else -abs(_as_int(field.get("minimum"), 1)) - 1
        if kind == "string" and ("name" in name or "email" in name or not name):
            return "x" * (max(600, _as_int(field.get("maxLength"), 0) + 50))
        if kind == "boolean":
            return "not-a-boolean"

    if kind == "boolean":
        return index % 2 == 0
    if kind in {"integer", "number"}:
        if "page" in name:
            return 1
        if "size" in name or "limit" in name:
            return 20
        if any(hint in name for hint in _AMOUNT_HINTS) and "maximum" not in field:
            field = {**field, "minimum": 100, "maximum": 10000}
        return _bounded_number(field, rng, distribution)
    if kind == "array":
        items = field.get("items") if isinstance(field.get("items"), dict) else {"type": "string"}
        minimum = max(0, _as_int(field.get("minItems"), 1))
        maximum = max(minimum, min(_as_int(field.get("maxItems"), max(minimum, 3)), 10))
        count = max(minimum, min(maximum, 1 + (index % max(1, maximum - minimum + 1))))
        return [
            _value(
                {**items, "name": f"{name}_item"},
                index * 10 + offset,
                rng,
                unicode=unicode,
                distribution=distribution,
                root=root,
                namespace=namespace,
                negative=negative,
                depth=depth + 1,
            )
            for offset in range(count)
        ]
    if kind == "object" or isinstance(field.get("properties"), dict):
        properties = field.get("properties") if isinstance(field.get("properties"), dict) else {}
        required = set(field.get("required") or [])
        result: dict[str, Any] = {}
        for prop_name, prop_schema in properties.items():
            if prop_name not in required and index % 4 == 1:
                continue
            nested = {
                **(prop_schema if isinstance(prop_schema, dict) else {}),
                "name": prop_name,
            }
            result[prop_name] = _value(
                nested,
                index,
                rng,
                unicode=unicode,
                distribution=distribution,
                root=root,
                namespace=namespace,
                negative=negative,
                depth=depth + 1,
            )
        return result
    result = _string_value(field, index, rng, namespace=namespace)
    if unicode and ("name" in name or "description" in name or not name):
        result += " 😀"
    return result


def _fields_from_endpoint(endpoint: Endpoint) -> list[dict[str, Any]]:
    """从 OpenAPI request_body 推断字段；保留 required / 嵌套 schema。"""
    schema = ((endpoint.request_body or {}).get("schema") or {}) if isinstance(endpoint.request_body, dict) else {}
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not props:
        return []
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, value in props.items():
        item = dict(value) if isinstance(value, dict) else {"type": "string"}
        item["name"] = name
        item["required"] = name in required
        fields.append(item)
    return fields


def _apply_relations(rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    """应用简单的跨字段关系：lt/lte/gt/gte/eq/ne。"""
    relations = schema.get("relations") if isinstance(schema.get("relations"), list) else []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        left, right, kind = relation.get("left"), relation.get("right"), relation.get("kind", "lt")
        if not left or not right:
            continue
        for row in rows:
            if left not in row or right not in row:
                continue
            a, b = row[left], row[right]
            try:
                if kind in {"lt", "lte"} and not (a < b):
                    row[right] = a + (1 if isinstance(a, int) else 1.0)
                elif kind in {"gt", "gte"} and not (a > b):
                    row[right] = a - (1 if isinstance(a, int) else 1.0)
                elif kind == "eq" and a != b:
                    row[right] = a
                elif kind == "ne" and a == b:
                    row[right] = f"{b}-2"
            except TypeError:
                continue


def _ensure_unique(rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    for field in fields:
        if not isinstance(field, dict) or not field.get("unique"):
            continue
        name = str(field.get("name") or "")
        if not name:
            continue
        seen: set[str] = set()
        for index, row in enumerate(rows):
            value = row.get(name)
            key = str(value)
            if key in seen:
                suffix = f"-{index}"
                row[name] = f"{value}{suffix}" if isinstance(value, str) else value
                key = str(row[name])
            seen.add(key)


def generate_rows(
    project: Project | None = None,
    *,
    fields: list[dict[str, Any]] | None = None,
    endpoint: Endpoint | None = None,
    count: int = 1,
    seed: int | None = None,
    unicode: bool = False,
    distribution: str = "uniform",
    namespace: str = "",
    negative: bool = False,
    schema: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """生成确定性数据行。

    ``namespace`` 会替换 ``{{run_id}}``；不传时保留占位符，由执行器渲染。
    ``schema`` 可携带 fields / relations / constraints，便于数据集持久化。
    """
    schema = schema if isinstance(schema, dict) else {}
    resolved = fields or schema.get("fields") or (_fields_from_endpoint(endpoint) if endpoint is not None else [])
    if not isinstance(resolved, list):
        resolved = []
    resolved = [item if isinstance(item, dict) else {"name": "value", "type": "string"} for item in resolved]
    if not resolved:
        resolved = [{"name": "value", "type": "string"}]
    root = {"fields": resolved, **{k: v for k, v in schema.items() if k != "fields"}}
    rng = random.Random(seed if seed is not None else 42)
    rows: list[dict[str, Any]] = []
    for index in range(max(1, min(int(count), 1000))):
        row: dict[str, Any] = {}
        for field in resolved:
            name = str(field.get("name") or "value")
            row[name] = _value(
                field,
                index,
                rng,
                unicode=unicode,
                distribution=distribution,
                root=root,
                namespace=namespace,
                negative=negative,
            )
        rows.append(row)
    _apply_relations(rows, root)
    _ensure_unique(rows, root)
    return rows


def pii_fields(fields: list[dict[str, Any]]) -> list[str]:
    """按字段名/格式标记潜在 PII；用于数据集 UI 和生成器提示。"""
    result: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").lower()
        fmt = str(field.get("format") or "").lower()
        if field.get("pii") or fmt in {"email", "phone"} or any(hint in name for hint in _PII_HINTS):
            result.append(str(field.get("name") or ""))
    return sorted({name for name in result if name})


def schema_for_endpoint(endpoint: Endpoint) -> dict[str, Any]:
    """把 endpoint request_body 转成可持久化的 dataset schema。"""
    fields = _fields_from_endpoint(endpoint)
    return {
        "fields": fields,
        "source": "endpoint",
        "endpoint_id": endpoint.id,
        "pii_fields": pii_fields(fields),
    }


def infer_schema_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """从 JSON Schema 顶层对象推断字段列表（用于内存 schema）。"""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    return [
        {**(value if isinstance(value, dict) else {}), "name": name, "required": name in required}
        for name, value in props.items()
    ]


def stable_hash(value: Any) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "generate_rows",
    "pii_fields",
    "schema_for_endpoint",
    "infer_schema_fields",
    "stable_hash",
]
