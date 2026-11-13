"""通用 probe 生成：不依赖项目文档，也能对接口发起高风险输入观测。

probe 用例**没有业务期望值**，只挂通用策略检查（不得 5xx、不得泄露堆栈 / SQL），
所以既能发现真实缺陷，又不会把猜测当成断言。适用于文档缺 schema / 鉴权声明的接口。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import collect_assertions
from app.models import Endpoint, Project, TestCase
from app.policies import policy_assertion

PROBE_TAG = "probe"
PROBE_POLICIES = ("POLICY-REL-001", "POLICY-SEC-001", "POLICY-SEC-002")
# 匿名访问已经声明鉴权的接口时，多挂一条「不得返回 2xx」的鉴权策略。
AUTH_PROBE_POLICIES = (*PROBE_POLICIES, "POLICY-AUTH-001")
# 写接口在「鉴权 + 路径参数 + 请求体」组合下会产生 5 类 probe，
# 上限必须覆盖完整组合，否则 fuzz 会被固定截断而结构性失效。
_MAX_PROBES_PER_ENDPOINT = 5

_INJECTION = "' OR '1'='1"
_TRAVERSAL = "../../../../etc/passwd"
_PATH_PARAM = re.compile(r"{([^}/]+)}")
# 健康检查 / 探活类接口不是业务面，不做 probe，避免无意义噪声。
_PUBLIC_PATH_HINTS = ("health", "ping", "metrics", "readiness", "liveness", "actuator")
_PAGINATION_PARAMS = {"limit", "offset", "page", "page_size", "pagesize", "per_page", "size", "cursor"}

# 通用探针模板：label / tag / dimension / 说明
_AUTH_LABEL = "未携带鉴权头访问"
_INJECTION_LABEL = "注入型参数"
_TRAVERSAL_LABEL = "路径穿越参数"
_PAGINATION_LABEL = "负数分页参数"
_BOUNDARY_LABEL = "空值 / 类型混淆请求体"
_FUZZ_LABEL = "Unicode / 超长请求体"

_BODY_METHODS = {"POST", "PUT", "PATCH"}
_FUZZ_PAYLOAD = "😀" + "A" * 512
_TYPE_CONFUSION = {
    "integer": "not-a-number",
    "number": "not-a-number",
    "boolean": "not-a-boolean",
    "array": "not-an-array",
    "object": "not-an-object",
}


def _path_params(path: str) -> list[str]:
    return _PATH_PARAM.findall(path or "")


def _query_names(endpoint: Endpoint) -> list[str]:
    params = endpoint.params if isinstance(endpoint.params, dict) else {}
    names: list[str] = []
    for item in params.get("query") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _has_security(endpoint: Endpoint) -> bool:
    security = endpoint.security
    if isinstance(security, dict):
        return bool(security)
    if isinstance(security, list):
        return bool(security)
    return False


def _body_props(endpoint: Endpoint) -> dict[str, dict[str, Any]]:
    """请求体字段声明；文档没写 schema 时返回空，probe 仍然可用通用 payload。"""
    schema = ((endpoint.request_body or {}).get("schema") or {}) if isinstance(endpoint.request_body, dict) else {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {}
    return {
        str(name): (value if isinstance(value, dict) else {})
        for name, value in props.items()
    }


def _bad_value(schema: dict[str, Any]) -> Any:
    return _TYPE_CONFUSION.get(str(schema.get("type") or "string"), "")


def _boundary_body(endpoint: Endpoint) -> dict[str, Any]:
    props = _body_props(endpoint)
    if not props:
        return {"__probe__": ""}
    return {name: _bad_value(schema) for name, schema in props.items()}


def _fuzz_body(endpoint: Endpoint) -> dict[str, Any]:
    props = _body_props(endpoint)
    if not props:
        return {"__probe__": _FUZZ_PAYLOAD}
    return {
        name: (_FUZZ_PAYLOAD if str(schema.get("type") or "string") in {"string", ""} else _bad_value(schema))
        for name, schema in props.items()
    }


def _probe_spec(
    endpoint: Endpoint,
    *,
    query: dict[str, Any],
    headers: dict[str, Any] | None,
    path: str,
    body: dict[str, Any] | None = None,
    policies: tuple[str, ...] = PROBE_POLICIES,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "request",
        "method": endpoint.method,
        "path": path,
        "assert": [policy_assertion(policy_id) for policy_id in policies],
    }
    if query:
        step["query"] = query
    if headers is not None:
        step["headers"] = headers
    if body is not None:
        step["body"] = body
    spec = {"steps": [step], "assertions": [], "contract_questions": [], "probe": True}
    return spec


def _plan_endpoint(endpoint: Endpoint) -> list[dict[str, Any]]:
    """给单个接口规划 probe；每个接口最多 5 条，避免把用例数量撑爆。"""
    planned: list[dict[str, Any]] = []
    path = endpoint.path or "/"
    if any(hint in path.lower() for hint in _PUBLIC_PATH_HINTS):
        return []
    path_params = _path_params(path)
    method = str(endpoint.method or "").upper()
    body_method = method in _BODY_METHODS

    if _has_security(endpoint):
        planned.append(
            {
                "title": f"[Probe] {endpoint.method} {path} · {_AUTH_LABEL}",
                "tag": "missing_auth",
                "dimension": "security",
                "note": "接口声明了鉴权，去掉凭证后校验不得返回成功状态码 / 不得 5xx / 不得泄露内部信息。",
                "spec": _probe_spec(
                    endpoint, query={}, headers={}, path=path, policies=AUTH_PROBE_POLICIES
                ),
            }
        )

    if path_params:
        injected = _PATH_PARAM.sub(lambda _m: quote(_INJECTION, safe=""), path)
        planned.append(
            {
                "title": f"[Probe] {endpoint.method} {path} · {_INJECTION_LABEL}",
                "tag": "injection",
                "dimension": "security",
                "note": "路径参数注入 payload，校验不得 5xx / 不得泄露堆栈与 SQL。",
                "spec": _probe_spec(endpoint, query={}, headers=None, path=injected),
            }
        )
        traversed = _PATH_PARAM.sub(lambda _m: quote(_TRAVERSAL, safe=""), path)
        planned.append(
            {
                "title": f"[Probe] {endpoint.method} {path} · {_TRAVERSAL_LABEL}",
                "tag": "path_traversal",
                "dimension": "security",
                "note": "路径参数填穿越 payload，校验不得 5xx / 不得泄露文件内容。",
                "spec": _probe_spec(endpoint, query={}, headers=None, path=traversed),
            }
        )
    else:
        query_names = _query_names(endpoint)
        if query_names:
            planned.append(
                {
                    "title": f"[Probe] {endpoint.method} {path} · {_INJECTION_LABEL}",
                    "tag": "injection",
                    "dimension": "security",
                    "note": "查询参数注入 payload，校验不得 5xx / 不得泄露堆栈与 SQL。",
                    "spec": _probe_spec(endpoint, query={query_names[0]: _INJECTION}, headers=None, path=path),
                }
            )
        elif body_method:
            # 没有路径/查询参数时，把注入 payload 放进请求体第一个字段。
            props = _body_props(endpoint)
            field = next(iter(props), "__probe__")
            planned.append(
                {
                    "title": f"[Probe] {endpoint.method} {path} · {_INJECTION_LABEL}",
                    "tag": "injection",
                    "dimension": "security",
                    "note": "请求体字段注入 payload，校验不得 5xx / 不得泄露堆栈与 SQL。",
                    "spec": _probe_spec(
                        endpoint, query={}, headers=None, path=path, body={field: _INJECTION}
                    ),
                }
            )
        if endpoint.method.upper() == "GET" and _PAGINATION_PARAMS & {name.lower() for name in query_names}:
            planned.append(
                {
                    "title": f"[Probe] {endpoint.method} {path} · {_PAGINATION_LABEL}",
                    "tag": "pagination",
                    "dimension": "boundary",
                    "note": "负数分页参数，校验不得 5xx / 不得泄露内部信息。",
                    "spec": _probe_spec(endpoint, query={"limit": -1, "offset": -1}, headers=None, path=path),
                }
            )

    if body_method:
        planned.append(
            {
                "title": f"[Probe] {endpoint.method} {path} · {_BOUNDARY_LABEL}",
                "tag": "boundary",
                "dimension": "boundary",
                "note": "请求体塞空值 / 类型混淆值，校验不得 5xx / 不得泄露内部信息。",
                "spec": _probe_spec(
                    endpoint, query={}, headers=None, path=path, body=_boundary_body(endpoint)
                ),
            }
        )
        planned.append(
            {
                "title": f"[Probe] {endpoint.method} {path} · {_FUZZ_LABEL}",
                "tag": "fuzz",
                "dimension": "boundary",
                "note": "请求体塞 Unicode / 超长字符串，校验不得 5xx / 不得泄露内部信息。",
                "spec": _probe_spec(
                    endpoint, query={}, headers=None, path=path, body=_fuzz_body(endpoint)
                ),
            }
        )
    return planned[:_MAX_PROBES_PER_ENDPOINT]


def _existing_titles(db: Session, project_id: int) -> set[str]:
    rows = db.scalars(select(TestCase.title).where(TestCase.project_id == project_id)).all()
    return set(rows)


def _plan_project(db: Session, project_id: int, *, endpoint_ids: list[int] | None, limit: int) -> dict[str, Any]:
    stmt = select(Endpoint).where(Endpoint.project_id == project_id)
    if endpoint_ids:
        stmt = stmt.where(Endpoint.id.in_(endpoint_ids))
    endpoints = list(db.scalars(stmt.order_by(Endpoint.id)).all())[: max(1, limit)]
    existing = _existing_titles(db, project_id)
    planned: list[dict[str, Any]] = []
    skipped = 0
    for endpoint in endpoints:
        for item in _plan_endpoint(endpoint):
            if item["title"] in existing:
                skipped += 1
                continue
            planned.append({**item, "endpoint_id": endpoint.id})
    return {"endpoints": len(endpoints), "planned": len(planned), "skipped": skipped, "items": planned}


def _brief(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: item[key] for key in ("title", "dimension", "tag", "note")} for item in items]


def preview_probes(db: Session, project: Project, *, endpoint_ids: list[int] | None = None, limit: int = 40) -> dict[str, Any]:
    plan = _plan_project(db, project.id, endpoint_ids=endpoint_ids, limit=limit)
    return {
        "endpoints": plan["endpoints"],
        "planned": plan["planned"],
        "skipped": plan["skipped"],
        "cases": _brief(plan["items"]),
    }


def create_probes(db: Session, project: Project, *, endpoint_ids: list[int] | None = None, limit: int = 40) -> dict[str, Any]:
    plan = _plan_project(db, project.id, endpoint_ids=endpoint_ids, limit=limit)
    created: list[TestCase] = []
    for item in plan["items"]:
        spec = item["spec"]
        policy_ids = list(
            dict.fromkeys(
                str(assertion.get("evidence", {}).get("ref", "")).split("@")[0]
                for assertion in collect_assertions(spec)
                if assertion.get("evidence")
            )
        )
        case = TestCase(
            project_id=project.id,
            title=item["title"][:400],
            dimension=item["dimension"],
            case_type="api",
            steps=spec["steps"],
            spec=spec,
            expected={"assertions": collect_assertions(spec)},
            evidence=[{"source_type": "policy", "ref": policy_id, "note": item["note"]} for policy_id in policy_ids],
            generation="probe",
            status="pending",
            priority=2,
            tags=[PROBE_TAG, item["tag"]],
        )
        db.add(case)
        created.append(case)
    if created:
        db.commit()
    return {
        "endpoints": plan["endpoints"],
        "planned": plan["planned"],
        "skipped": plan["skipped"],
        "created": len(created),
        "case_ids": [case.id for case in created],
        "cases": _brief(plan["items"]),
    }
