"""测试数据生成：schema + 字段名语义 + 通用数据策略。"""
from __future__ import annotations

import random
import uuid
from typing import Any

from app.models import Endpoint, Project


def _field_type(field: dict[str, Any]) -> str:
    kind = field.get("type") or "string"
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    return str(kind)


def _value(
    field: dict[str, Any],
    index: int,
    rng: random.Random,
    *,
    unicode: bool = False,
    distribution: str = "uniform",
) -> Any:
    name = str(field.get("name") or "").lower()
    kind = _field_type(field)
    enum = field.get("enum")
    if enum:
        return enum[index % len(enum)]
    if kind == "boolean":
        return index % 2 == 0
    if kind in {"integer", "number"}:
        minimum = field.get("minimum", 1)
        maximum = field.get("maximum", 1000)
        try:
            low, high = int(minimum), int(maximum)
        except (TypeError, ValueError):
            low, high = 1, 1000
        if ("amount" in name or "price" in name or "total" in name) and "maximum" not in field:
            low, high = max(low, 100), max(high, 10000)
        if "page" in name:
            return 1
        if "size" in name or "limit" in name:
            return 20
        if distribution == "normal":
            mean = (low + high) / 2
            sigma = max(0.1, (high - low) / 6)
            return max(low, min(high, int(rng.gauss(mean, sigma))))
        return rng.randint(low, high)
    if kind == "array":
        return [f"item-{index}-{rng.randint(1, 100)}"]
    if kind == "object":
        return {"key": f"value-{index}"}
    if "email" in name:
        return f"shakedown-{{{{run_id}}}}-{index}@example.com"
    if "phone" in name or "mobile" in name:
        return f"138{rng.randint(10000000, 99999999)}"
    if "url" in name or "link" in name:
        return f"https://example.com/items/{index}"
    if "date" in name or name.endswith("_at") or "time" in name:
        return "2026-01-01T00:00:00Z"
    if "status" in name or "state" in name:
        return field.get("default") or "active"
    if "name" in name or "title" in name:
        base = f"shakedown-{{{{run_id}}}}-{name}-{index}"
        return base + (" 😀" if unicode else "")
    if "description" in name or "remark" in name or "note" in name:
        return ("Shakedown 测试数据 " + "A" * 80) if unicode else f"Shakedown test data {index}"
    return f"shakedown-{{{{run_id}}}}-{name or 'value'}-{index}"


def _fields_from_endpoint(endpoint: Endpoint) -> list[dict[str, Any]]:
    schema = ((endpoint.request_body or {}).get("schema") or {}) if isinstance(endpoint.request_body, dict) else {}
    props = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(props, dict):
        return []
    return [{"name": name, **(value if isinstance(value, dict) else {})} for name, value in props.items()]


def generate_rows(
    project: Project,
    *,
    fields: list[dict[str, Any]] | None = None,
    endpoint: Endpoint | None = None,
    count: int = 1,
    seed: int | None = None,
    unicode: bool = False,
    distribution: str = "uniform",
) -> list[dict[str, Any]]:
    resolved = fields or (_fields_from_endpoint(endpoint) if endpoint is not None else [])
    if not resolved:
        resolved = [{"name": "value", "type": "string"}]
    rng = random.Random(seed if seed is not None else 42)
    rows: list[dict[str, Any]] = []
    for index in range(max(1, min(count, 200))):
        row: dict[str, Any] = {}
        for field in resolved:
            name = str(field.get("name") or "value")
            row[name] = _value(
                field,
                index,
                rng,
                unicode=unicode,
                distribution=distribution,
            )
        rows.append(row)
    return rows
