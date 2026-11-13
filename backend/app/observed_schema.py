"""observed 层：从真实响应推断结构基线，只报告「变了」，不单独判「错了」。

契约层回答「按约定应该是什么」，observed 层回答「和上次相比变了什么」。
漂移只能作为风险线索进入 triage，不能直接生成断言或缺陷。

聚合口径：
- 先用项目 ``Endpoint`` 的 method + path 模板匹配真实 URL（``/api/tasks/81``
  → ``/api/tasks/{task_id}``），匹配不到的落到 unregistered 桶；
- 每个「接口模板 + 状态码」独立对比，避免 200/201/204 结构差异互相干扰；
- 只取最近 ``max_results`` 条结果，并用「最新一条 vs 上一条同类响应」做对比。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.consistency import normalize_path
from app.models import CaseResult, Endpoint, Project

_IGNORED_TYPES = {"null"}
_TEMPLATE_PARAM = re.compile(r"\{[^}/]+\}|:[A-Za-z_][A-Za-z0-9_]*|<[^>/]+>")


def _template_regex(path: str) -> re.Pattern[str]:
    pattern = "[^/]+".join(re.escape(part) for part in _TEMPLATE_PARAM.split(path))
    return re.compile(f"^{pattern}/?$")


def _load_templates(db: Session, project_id: int) -> list[tuple[str, str, re.Pattern[str]]]:
    rows = db.execute(
        select(Endpoint.method, Endpoint.path).where(Endpoint.project_id == project_id)
    ).all()
    templates = [
        (str(method or "").upper(), str(path), _template_regex(str(path)))
        for method, path in rows
        if path
    ]
    # 更长的字面量前缀优先，避免通用模板抢走具体路由。
    templates.sort(key=lambda item: -len(item[1]))
    return templates


def _endpoint_key(method: str, path: str, templates: list[tuple[str, str, re.Pattern[str]]]) -> str:
    method = method.upper()
    normalized = path.rstrip("/") or "/"
    for t_method, t_path, regex in templates:
        if t_method == method and regex.match(normalized):
            return f"{method} {t_path}"
    return f"{method} {normalize_path(normalized)} (unregistered)"


def _request_endpoint(request: dict[str, Any]) -> tuple[str, str] | None:
    method = str(request.get("method") or "").strip()
    if not method:
        return None
    url = str(request.get("url") or "")
    path = urlparse(url).path if url else str(request.get("path") or "")
    if not path:
        return None
    return method.upper(), path


def _shape(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"type": "object", "fields": {str(key): _shape(item) for key, item in value.items()}}
    if isinstance(value, list):
        items = [_shape(item) for item in value[:3]]
        return {"type": "array", "item": items[0] if items else {}}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


def _fields(shape: dict[str, Any]) -> dict[str, str]:
    kind = str(shape.get("type") or "")
    if kind == "object":
        return {
            name: str(item.get("type") or "unknown")
            for name, item in (shape.get("fields") or {}).items()
        }
    if kind == "array":
        item = shape.get("item") or {}
        item_type = str(item.get("type") or "")
        if not item_type:
            # 空数组推断不出元素结构，跳过该样本，避免把「空列表」误报成结构缺失。
            return {}
        if item_type == "object":
            return _fields(item)
        # 标量数组没有字段名，用 [] 占位以便检测元素类型漂移。
        return {"[]": item_type}
    return {"$": kind or "unknown"}


def _sample(
    result: CaseResult,
    templates: list[tuple[str, str, re.Pattern[str]]],
) -> tuple[str, int, dict[str, Any]] | None:
    response = result.response if isinstance(result.response, dict) else {}
    status = response.get("status_code")
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        return None
    if not 200 <= status_code < 300:
        return None
    endpoint = _request_endpoint(result.request if isinstance(result.request, dict) else {})
    if endpoint is None:
        return None
    shape = _shape(response.get("body"))
    fields = _fields(shape)
    if not fields:
        return None
    return _endpoint_key(*endpoint, templates), status_code, {
        "root": str(shape.get("type") or ""),
        "fields": fields,
    }


def observed_schema_drift(
    db: Session,
    project: Project,
    *,
    min_samples: int = 2,
    max_results: int = 2000,
) -> dict[str, Any]:
    """对比每个「接口模板 + 状态码」最近一次响应与上一条同类响应。"""
    results = db.scalars(
        select(CaseResult)
        .where(CaseResult.project_id == project.id)
        .order_by(CaseResult.id.desc())
        .limit(max_results)
    ).all()
    results = list(reversed(results))
    templates = _load_templates(db, project.id)

    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for result in results:
        parsed = _sample(result, templates)
        if parsed is None:
            continue
        key, status_code, sample = parsed
        groups.setdefault((key, status_code), []).append(sample)

    drift: list[dict[str, Any]] = []
    compared = 0
    for (key, status_code), samples in groups.items():
        if len(samples) < max(2, min_samples):
            continue
        compared += 1
        previous = samples[-2]
        latest = samples[-1]
        added = sorted(name for name in latest["fields"] if name not in previous["fields"])
        removed = sorted(name for name in previous["fields"] if name not in latest["fields"])
        type_changed = [
            {"field": name, "from": previous["fields"][name], "to": latest["fields"][name]}
            for name in sorted(set(latest["fields"]) & set(previous["fields"]))
            if previous["fields"][name] != latest["fields"][name]
            and previous["fields"][name] not in _IGNORED_TYPES
            and latest["fields"][name] not in _IGNORED_TYPES
        ]
        root_changed = (
            {"from": previous["root"], "to": latest["root"]}
            if previous["root"] != latest["root"]
            else None
        )
        if not (added or removed or type_changed or root_changed):
            continue
        method, path = key.split(" ", 1)
        drift.append(
            {
                "endpoint": key,
                "method": method,
                "path": path,
                "status_code": status_code,
                "samples": len(samples),
                "added": added,
                "removed": removed,
                "type_changed": type_changed,
                "root_changed": root_changed,
            }
        )

    drift.sort(
        key=lambda row: (
            -(len(row["added"]) + len(row["removed"]) + len(row["type_changed"]) + (1 if row["root_changed"] else 0)),
            row["endpoint"],
        )
    )
    if not groups:
        message = "还没有成功响应样本，无法建立 observed 基线。"
    elif compared == 0:
        message = f"已收集 {sum(len(items) for items in groups.values())} 条响应，但每个接口/状态码只有一条样本，暂无法对比。"
    elif not drift:
        message = f"已对比 {compared} 组接口响应，未发现结构漂移。"
    else:
        message = ""
    return {
        "samples": sum(len(items) for items in groups.values()),
        "endpoints": len({key for key, _ in groups}),
        "compared": compared,
        "drift_count": len(drift),
        "drift": drift,
        "message": message,
    }
