"""离线 provider（mock / 确定性启发式）。

作用有两个：
1. 没有配置 LLM 时，整个平台（生成 → 执行 → 分析 → 看板）仍然能跑通；
2. 作为回归测试的确定性基线 —— 同样的输入永远得到同样的输出。

它实现的是「规则版 Agent」：直接消费调用方传下来的结构化素材，
按任务类型（``context["task"]``）产出结果，不经过网络。
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.providers.base import LLMError, LLMProvider, LLMResponse, TokenUsage
from app.executor.case_schema import SCHEMA_VERSION

_PUNCT = re.compile(r"[^\w一-鿿]+")

_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


def normalize(path: str) -> str:
    """路径归一化（与 ingest 层保持一致）。"""
    if not path:
        return "/"
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", path)
    path = re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", r"{\1}", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _slug(text: str) -> str:
    return _PUNCT.sub("-", text.strip().lower()).strip("-")[:60] or "case"


def _body_schema(endpoint: dict[str, Any]) -> dict[str, Any]:
    return ((endpoint.get("request_body") or {}).get("schema") or {})


def _body_fields(endpoint: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    schema = _body_schema(endpoint)
    result: list[tuple[str, dict[str, Any]]] = []
    for name, prop in (schema.get("properties") or {}).items():
        result.append((name, prop if isinstance(prop, dict) else {}))
    return result


def _field_type(prop: dict[str, Any]) -> str:
    prop_type = prop.get("type")
    if isinstance(prop_type, list):
        prop_type = next((t for t in prop_type if t != "null"), "string")
    if prop_type:
        return str(prop_type)
    if prop.get("properties"):
        return "object"
    if prop.get("oneOf") or prop.get("anyOf"):
        return "string"
    return "string"


def _sample_value(name: str, prop: dict[str, Any]) -> Any:
    """生成带 run_id 前缀的唯一测试数据（避免冲突）。"""
    kind = _field_type(prop)
    lowered = name.lower()
    if kind in {"integer", "number"}:
        if "page" in lowered:
            return 1
        if "size" in lowered or "limit" in lowered:
            return 20
        if "amount" in lowered or "price" in lowered or "total" in lowered:
            return 1000
        if "id" in lowered:
            return 1
        return 100
    if "email" in lowered:
        return "shakedown-{{run_id}}@example.com"
    if "role" in lowered:
        enum = prop.get("enum")
        if enum:
            return enum[0]
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return f"shakedown-{{{{run_id}}}}-{name}"


def _param_names(endpoint: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for group in ("path", "query", "header"):
        for param in endpoint.get("params", {}).get(group, []) or []:
            if isinstance(param, dict) and param.get("name"):
                names.append(param["name"])
    return names


def _has_path_param(endpoint: dict[str, Any]) -> bool:
    if endpoint.get("params", {}).get("path"):
        return True
    return bool(re.search(r"\{[A-Za-z0-9_]+\}", endpoint.get("path") or ""))


def _response_fields(endpoint: dict[str, Any]) -> list[str]:
    responses = endpoint.get("responses") or {}
    for code in ("200", "201", "202", "default"):
        resp = responses.get(code)
        if isinstance(resp, dict):
            schema = resp.get("schema") or {}
            props = schema.get("properties") or {}
            if props:
                return list(props.keys())
    return []


def _pick_endpoint(
    feature: dict[str, Any],
    endpoints: list[dict[str, Any]],
    method: str | None = None,
    *,
    prefer_no_path_param: bool = False,
) -> dict[str, Any] | None:
    candidates = [e for e in endpoints if e.get("id") in set(feature.get("endpoint_ids") or [])]
    if not candidates:
        candidates = endpoints
    # 只提到路径、没说方法的接口（method="*"）不参与用例生成
    candidates = [e for e in candidates if (e.get("method") or "").upper() in _HTTP_METHODS]
    if not candidates:
        return None
    if prefer_no_path_param:
        simple = [e for e in candidates if not _has_path_param(e)]
        if simple:
            candidates = simple
    if method:
        for endpoint in candidates:
            if endpoint.get("method", "").upper() == method.upper():
                return endpoint
    for endpoint in candidates:
        if endpoint.get("method", "").upper() in {"POST", "PUT", "PATCH"}:
            return endpoint
    return candidates[0]


def _evidence_from_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    """从接口的来源里找依据。

    兼容两种来源格式：
    - 完整格式：``[{"type": "openapi", "ref": "#/paths/..."}]``
    - 精简格式（功能点抽取用）：``["openapi", "code"]``（只有类型名，没有定位串）
    """
    sources = endpoint.get("sources") or []
    for source in sources:
        if isinstance(source, str):
            if source in {"openapi", "code", "doc"}:
                return {"source_type": source, "ref": "", "endpoint_id": endpoint.get("id")}
            continue
        if source.get("type") == "openapi" and source.get("ref"):
            return {"source_type": "openapi", "ref": source["ref"], "endpoint_id": endpoint.get("id")}
    for source in sources:
        if isinstance(source, str):
            continue
        if source.get("type") == "code" and source.get("ref"):
            return {"source_type": "code", "ref": source["ref"], "endpoint_id": endpoint.get("id")}
    for source in sources:
        if isinstance(source, str):
            continue
        if source.get("type") == "doc" and source.get("ref"):
            return {"source_type": "doc", "ref": source["ref"], "endpoint_id": endpoint.get("id")}
    return {"source_type": "none", "ref": "", "note": "该接口没有可追溯的来源，需人工确认"}


def _evidence_from_docs(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """从文档里找依据。

    优先用 role="spec" 的接口规范；设计笔记（notes）只能作为「待确认」的参考，
    不能当断言依据（它记的是过程，不是契约）。
    """
    for doc in docs:
        if doc.get("role") == "notes":
            continue
        locator = doc.get("locator") or {}
        ref = locator.get("file_path") or " > ".join(locator.get("section_path") or []) or ""
        if ref:
            return {
                "source_type": "doc",
                "ref": str(ref),
                "quote": (doc.get("content") or "")[:200],
                "chunk_id": doc.get("id"),
            }
    # 没有规范可用 → 退回设计笔记，但标成「待确认」
    for doc in docs:
        locator = doc.get("locator") or {}
        ref = locator.get("file_path") or " > ".join(locator.get("section_path") or []) or ""
        if ref:
            return {
                "source_type": "none",
                "ref": str(ref),
                "quote": (doc.get("content") or "")[:200],
                "chunk_id": doc.get("id"),
                "note": "依据来自设计笔记（非接口规范），需人工确认",
            }
    return {"source_type": "none", "ref": "", "note": "文档中未找到对应说明，需人工确认"}


def _base_step(
    endpoint: dict[str, Any],
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "action": "request",
        "name": f"{method} {path}",
        "method": method,
        "path": path,
        "headers": {"Content-Type": "application/json"},
    }
    if body is not None:
        step["body"] = body
    if query:
        step["query"] = query
    return step


def _query_params(endpoint: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for param in endpoint.get("params", {}).get("query", []) or []:
        if not isinstance(param, dict) or not param.get("name"):
            continue
        schema = param.get("schema") or {}
        values[param["name"]] = _sample_value(param["name"], schema if isinstance(schema, dict) else {})
    return values


def _normalize_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    """把「精简版端点」还原成 mock 期望的完整形状。

    精简版（真实模型用的上下文）：
    ``params: ["query:page"]`` / ``body: ["name"]`` / ``responses: ["200:['id']"]``
    完整版：``params: {"query": [{"name": "page"}]}`` 等。
    """
    if not isinstance(endpoint, dict):
        return {}
    result = dict(endpoint)

    params = result.get("params")
    if isinstance(params, list):
        grouped: dict[str, list[dict[str, Any]]] = {"path": [], "query": [], "header": []}
        for item in params:
            if not isinstance(item, str) or ":" not in item:
                continue
            group, name = item.split(":", 1)
            grouped.setdefault(group, []).append({"name": name})
        result["params"] = grouped

    body = result.get("body")
    if isinstance(body, list):
        result["request_body"] = {"schema": {"properties": {name: {} for name in body}}}

    responses = result.get("responses")
    if isinstance(responses, list):
        parsed: dict[str, Any] = {}
        for item in responses:
            if not isinstance(item, str) or ":" not in item:
                continue
            code, fields = item.split(":", 1)
            names = [f for f in fields.strip("[]'\" ").split(",") if f]
            parsed[code] = {"schema": {"properties": {name: {} for name in names}}}
        result["responses"] = parsed
    return result


class MockProvider(LLMProvider):
    """确定性离线 provider。

    注意：它**不会主动发起工具调用**（`supports_tools = False`），
    所以涉及「Agent 自己查资料」的场景会自动回退到「切片进上下文」的方式。
    """

    name = "mock"
    # 离线 provider 不支持工具调用循环
    supports_tools = False

    def __init__(self, model: str = "shakedown-mock"):
        self.model = model

    # ── chat：mock 不做工具调用 ───────────────────────────
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        last = messages[-1]["content"] if messages else ""
        return LLMResponse(
            content=f"[mock] 已收到 {len(messages)} 条消息，末条长度 {len(str(last))}。离线 provider 不执行工具调用。",
            usage=TokenUsage(input_tokens=sum(len(str(m.get('content') or '')) for m in messages) // 4, output_tokens=8),
            model=self.model,
        )

    # ── structured：按任务类型分发 ─────────────────────────
    def structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        context = context or {}
        task = context.get("task", "")
        handler = {
            "feature_extraction": self._feature_extraction,
            "case_generation": self._case_generation,
            "failure_attribution": self._failure_attribution,
            "whitebox_review": self._whitebox_review,
            "doc_endpoint_extraction": self._doc_endpoint_extraction,
        }.get(task)
        if handler is None:
            raise LLMError(f"mock provider 不支持的任务类型：{task!r}")
        return handler(context)

    # ── 从文档抽接口（离线：宽松规则）──────────────────────
    def _doc_endpoint_extraction(self, context: dict[str, Any]) -> dict[str, Any]:
        from app.agent.docextract import rule_based_extraction

        chunks = context.get("chunks") or []
        payload = [
            {
                "id": index,
                "content": chunk.get("content", ""),
                "locator": {"file_path": chunk.get("ref", ""), "section_path": []},
            }
            for index, chunk in enumerate(chunks)
        ]
        return {"endpoints": rule_based_extraction(payload)}

    # ── 功能点抽取 ─────────────────────────────────────────
    def _feature_extraction(self, context: dict[str, Any]) -> dict[str, Any]:
        endpoints: list[dict[str, Any]] = context.get("endpoints") or []
        docs: list[dict[str, Any]] = context.get("doc_chunks") or []
        features: list[dict[str, Any]] = []

        groups: dict[str, list[dict[str, Any]]] = {}
        for endpoint in endpoints:
            path = endpoint.get("path") or "/"
            parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
            module = parts[0] if parts else "root"
            groups.setdefault(module, []).append(endpoint)

        for module, group in sorted(groups.items()):
            for endpoint in group:
                evidence = _evidence_from_endpoint(endpoint)
                doc_ref = None
                keyword = (endpoint.get("path") or "").strip("/").split("/")[-1].lower()
                for doc in docs:
                    if keyword and keyword in (doc.get("content") or "").lower():
                        doc_ref = doc
                        break
                summary = endpoint.get("summary") or ""
                if summary.startswith("@") or summary.startswith("app."):
                    summary = ""
                name = summary or f"{module} {endpoint.get('method')} {endpoint.get('path')}"
                feature = {
                    "name": name.strip()[:280],
                    "module": module,
                    "description": f"接口 {endpoint.get('method')} {endpoint.get('path')}".strip(),
                    "source_refs": ([evidence] if evidence.get("source_type") != "none" else [])
                    + ([_evidence_from_docs([doc_ref])] if doc_ref else []),
                    "endpoint_ids": [endpoint.get("id")],
                }
                features.append(feature)

        # 文档里提到但没有对应接口的功能点 —— 标记为未映射
        for doc in docs:
            content = (doc.get("content") or "").strip()
            if len(content) < 60:
                continue
            title = content.splitlines()[0].lstrip("# ").strip()
            features.append(
                {
                    "name": f"文档功能点：{title[:60]}",
                    "module": "doc",
                    "description": content[:400],
                    "source_refs": [_evidence_from_docs([doc])],
                    "endpoint_ids": [],
                }
            )
            break
        return {"features": features}

    # ── 用例生成 ───────────────────────────────────────────
    def _case_generation(self, context: dict[str, Any]) -> dict[str, Any]:
        dimension = context.get("dimension", "functional")
        endpoints: list[dict[str, Any]] = context.get("endpoints") or []
        docs: list[dict[str, Any]] = context.get("doc_chunks") or []
        project = context.get("project") or {}
        sla = context.get("sla") or {}
        base_url = project.get("base_url", "")

        builder = {
            "functional": self._functional_cases,
            "boundary": self._boundary_cases,
            "security": self._security_cases,
            "performance": self._performance_cases,
        }.get(dimension, self._functional_cases)

        # 支持一批多个功能点（真实模型走批量；离线也兼容）
        features = context.get("features") or []
        if not features:
            features = [context.get("feature") or {}]

        cases: list[dict[str, Any]] = []
        normalized = [_normalize_endpoint(e) for e in endpoints]
        normalized_all = [_normalize_endpoint(e) for e in (context.get("all_endpoints") or endpoints)]
        for feature in features:
            if not feature:
                continue
            produced = builder(
                feature,
                normalized,
                docs,
                base_url,
                sla,
                normalized_all,
            )
            for case in produced:
                case.setdefault("schema_version", SCHEMA_VERSION)
                case["feature"] = {"id": feature.get("id"), "name": feature.get("name", "")}
            cases.extend(produced)
        return {"cases": cases}

    # ── 正常流 ─────────────────────────────────────────────
    def _functional_cases(
        self,
        feature: dict[str, Any],
        endpoints: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        base_url: str,
        sla: dict[str, Any],
        all_endpoints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = _pick_endpoint(feature, endpoints)
        if endpoint is None:
            return []
        all_endpoints = all_endpoints or endpoints
        method = (endpoint.get("method") or "GET").upper()
        path = normalize(endpoint.get("path") or "/")
        evidence = _evidence_from_endpoint(endpoint)
        doc_evidence = _evidence_from_docs(docs)
        if evidence.get("source_type") == "none" and doc_evidence.get("source_type") != "none":
            evidence = doc_evidence

        body_fields = _body_fields(endpoint)
        body = {name: _sample_value(name, prop) for name, prop in body_fields}
        query = _query_params(endpoint)
        response_fields = _response_fields(endpoint)

        steps: list[dict[str, Any]] = []
        assertions: list[dict[str, Any]] = []

        # 路径带参数时：先造数据再回读（正常流的正确做法）
        create_endpoint = self._find_create_endpoint(endpoint, all_endpoints)
        if _has_path_param(endpoint) and create_endpoint is not None:
            create_body = {name: _sample_value(name, prop) for name, prop in _body_fields(create_endpoint)}
            create_step = _base_step(create_endpoint, (create_endpoint.get("method") or "POST").upper(), normalize(create_endpoint.get("path") or "/"), create_body)
            if "id" in _response_fields(create_endpoint):
                create_step["save"] = {"entity_id": "$.id"}
            steps.append(create_step)
            path = re.sub(r"\{[A-Za-z0-9_]+\}", "{{entity_id}}", path)

        step = _base_step(endpoint, method, path, body if body else None, query or None)
        if "id" in response_fields:
            step["save"] = {"entity_id": "$.id"}
        expected_status = "201" if method == "POST" else "200"
        assertions.append(
            {
                "kind": "status_code",
                "operator": "eq",
                "expected": int(expected_status),
                "status": "confirmed" if evidence.get("source_type") != "none" else "pending",
                "evidence": evidence,
                "description": "业务请求返回成功状态码",
            }
        )
        if response_fields:
            assertions.append(
                {
                    "kind": "json_path",
                    "target": f"$.{response_fields[0]}",
                    "operator": "exists",
                    "expected": True,
                    "status": "confirmed",
                    "evidence": evidence,
                    "description": f"响应体包含业务字段 {response_fields[0]}（不只是状态码）",
                }
            )
        else:
            assertions.append(
                {
                    "kind": "json_path",
                    "target": "$.data",
                    "operator": "exists",
                    "expected": True,
                    "status": "pending",
                    "evidence": {"source_type": "none", "note": "接口定义未给出响应字段，业务结果断言待人工确认"},
                    "description": "业务结果断言（待确认）",
                }
            )
        step["assert"] = assertions
        steps.append(step)

        # 回读校验（写操作之后）
        read_endpoint = self._find_readback(endpoint, all_endpoints)
        if read_endpoint is not None and method in {"POST", "PUT", "PATCH"}:
            read_path = normalize(read_endpoint.get("path") or path)
            read_path = re.sub(r"\{[A-Za-z0-9_]+\}", "{{entity_id}}", read_path)
            read_fields = _response_fields(read_endpoint)
            read_assertions = [
                {
                    "kind": "status_code",
                    "operator": "eq",
                    "expected": 200,
                    "status": "confirmed" if evidence.get("source_type") != "none" else "pending",
                    "evidence": _evidence_from_endpoint(read_endpoint),
                    "description": "回读资源成功（数据落库）",
                }
            ]
            if read_fields:
                read_assertions.append(
                    {
                        "kind": "json_path",
                        "target": f"$.{read_fields[0]}",
                        "operator": "exists",
                        "expected": True,
                        "status": "confirmed",
                        "evidence": _evidence_from_endpoint(read_endpoint),
                        "description": f"回读结果包含业务字段 {read_fields[0]}",
                    }
                )
            steps.append(
                {
                    "action": "request",
                    "name": f"回读 GET {read_path}",
                    "method": "GET",
                    "path": read_path,
                    "headers": {},
                    "assert": read_assertions,
                }
            )

        return [
            {
                "title": f"[正常流] {feature.get('name', '')} - {method} {endpoint.get('path')}".strip(),
                "dimension": "functional",
                "type": "api",
                "description": "按文档描述完成一次正常业务操作，校验业务结果（状态码 + 业务字段 + 回读落库），不能只看状态码。",
                "preconditions": [{"kind": "http_login", "name": "default", "account_ref": "default", "note": "使用项目测试账号登录"}],
                "steps": steps,
                "assertions": assertions,
                "evidence": [evidence] if evidence.get("source_type") != "none" else [],
                "tags": ["normal-flow"],
            }
        ]

    @staticmethod
    def _find_create_endpoint(endpoint: dict[str, Any], endpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
        """为带路径参数的接口找一个「创建资源」的接口。"""
        base = normalize(endpoint.get("path") or "").split("{")[0].rstrip("/")
        for candidate in endpoints:
            if candidate.get("method", "").upper() not in {"POST", "PUT"}:
                continue
            candidate_path = normalize(candidate.get("path") or "")
            if candidate_path == base:
                return candidate
        return None

    @staticmethod
    def _find_readback(endpoint: dict[str, Any], endpoints: list[dict[str, Any]]) -> dict[str, Any] | None:
        """为写操作找一个回读用的 GET 接口（路径前缀匹配）。"""
        path = normalize(endpoint.get("path") or "")
        base = path.split("{")[0].rstrip("/")
        for candidate in endpoints:
            if candidate.get("method", "").upper() != "GET":
                continue
            candidate_path = normalize(candidate.get("path") or "")
            if candidate_path == base or (base and candidate_path.startswith(base)):
                return candidate
        return None

    # ── 边界 ───────────────────────────────────────────────
    def _boundary_cases(
        self,
        feature: dict[str, Any],
        endpoints: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        base_url: str,
        sla: dict[str, Any],
        all_endpoints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = _pick_endpoint(feature, endpoints, prefer_no_path_param=True)
        if endpoint is None:
            return []
        all_endpoints = all_endpoints or endpoints
        method = (endpoint.get("method") or "GET").upper()
        path = normalize(endpoint.get("path") or "/")
        evidence = _evidence_from_endpoint(endpoint)
        schema = _body_schema(endpoint)
        required_fields = set(schema.get("required") or [])
        body_fields = _body_fields(endpoint)
        query = _query_params(endpoint)
        required_query = [
            param
            for param in (endpoint.get("params") or {}).get("query", []) or []
            if isinstance(param, dict) and param.get("required")
        ]

        variants: list[tuple[str, str, Any, str]] = []  # (字段, 标签, 值, 依据说明)
        for name, prop in body_fields:
            kind = _field_type(prop)
            if kind in {"integer", "number"} and ("minimum" in prop or "exclusiveMinimum" in prop):
                variants.append((name, "负数", -1, f"spec 声明 minimum={prop.get('minimum')}"))
                variants.append((name, "零", 0, f"spec 声明 minimum={prop.get('minimum')}"))
            elif kind == "string" and ("minLength" in prop or name in required_fields):
                variants.append((name, "空值", "", f"spec 声明 minLength={prop.get('minLength', 1)}"))
            elif prop.get("enum"):
                variants.append((name, "非法枚举值", "__invalid_enum__", f"spec 声明 enum={prop.get('enum')}"))
            elif "maxLength" in prop:
                variants.append((name, "超长字符串", "x" * 1025, f"spec 声明 maxLength={prop.get('maxLength')}"))

        if not variants and required_query:
            variants.append(("", "缺少必填参数", None, f"spec 声明必填参数 {[p.get('name') for p in required_query]}"))

        if not variants:
            return [
                {
                    "title": f"[边界] {feature.get('name', '')} - 缺少输入".strip(),
                    "dimension": "boundary",
                    "type": "api",
                    "description": "接口定义未给出可校验的边界约束（minimum/maxLength/enum/必填），按约定不硬凑用例。",
                    "preconditions": [{"kind": "none"}],
                    "steps": [],
                    "assertions": [],
                    "evidence": [],
                    "tags": ["boundary"],
                    "missing_input": "缺少输入：接口定义没有可依据的边界约束，需要人工补充规则",
                }
            ]

        cases: list[dict[str, Any]] = []
        for field_name, label, value, basis in variants[:4]:
            body: dict[str, Any] = {name: _sample_value(name, prop) for name, prop in body_fields}
            query_copy = dict(query)
            if field_name:
                body[field_name] = value
                note = f"字段 {field_name} 传「{label}」（{basis}）"
            else:
                dropped = required_query[0].get("name")
                query_copy.pop(dropped, None)
                note = f"缺少必填参数 {dropped}（{basis}）"
            step = _base_step(endpoint, method, path, body if body else None, query_copy or None)
            assertion = {
                "kind": "status_code",
                "operator": "in",
                "expected": [400, 422],
                "status": "confirmed" if evidence.get("source_type") != "none" else "pending",
                "evidence": evidence
                if evidence.get("source_type") != "none"
                else {"source_type": "none", "note": "缺少边界行为的依据，待确认"},
                "description": note,
            }
            step["assert"] = [assertion]
            cases.append(
                {
                    "title": f"[边界] {feature.get('name', '')} - {field_name or '请求'} {label}".strip(),
                    "dimension": "boundary",
                    "type": "api",
                    "description": f"异常输入场景：{note}。期望系统校验输入并给出明确错误，而不是 500 或脏数据落库。",
                    "preconditions": [{"kind": "http_login", "name": "default", "account_ref": "default"}],
                    "steps": [step],
                    "assertions": [assertion],
                    "evidence": [],
                    "tags": ["boundary"],
                }
            )
        return cases

    # ── 安全 ───────────────────────────────────────────────
    def _security_cases(
        self,
        feature: dict[str, Any],
        endpoints: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        base_url: str,
        sla: dict[str, Any],
        all_endpoints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = _pick_endpoint(feature, endpoints, prefer_no_path_param=True)
        if endpoint is None:
            return []
        all_endpoints = all_endpoints or endpoints
        method = (endpoint.get("method") or "GET").upper()
        path = normalize(endpoint.get("path") or "/")
        evidence = _evidence_from_endpoint(endpoint)
        secured = bool((endpoint.get("security") or {}).get("required"))
        body = {name: _sample_value(name, prop) for name, prop in _body_fields(endpoint)}

        steps: list[dict[str, Any]] = []
        if _has_path_param(endpoint):
            create_endpoint = self._find_create_endpoint(endpoint, all_endpoints)
            if create_endpoint is not None:
                create_body = {name: _sample_value(name, prop) for name, prop in _body_fields(create_endpoint)}
                create_step = _base_step(
                    create_endpoint,
                    (create_endpoint.get("method") or "POST").upper(),
                    normalize(create_endpoint.get("path") or "/"),
                    create_body,
                )
                if "id" in _response_fields(create_endpoint):
                    create_step["save"] = {"entity_id": "$.id"}
                steps.append(create_step)
                path = re.sub(r"\{[A-Za-z0-9_]+\}", "{{entity_id}}", path)

        unauth = {
            "kind": "status_code",
            "operator": "in",
            "expected": [401, 403],
            "status": "confirmed" if secured else "pending",
            "evidence": evidence
            if secured
            else {"source_type": "none", "note": "接口未声明鉴权要求，越权期望待确认"},
            "description": "未携带任何凭据访问受保护接口，应被拒绝",
        }
        step = _base_step(endpoint, method, path, body if body else None)
        step["headers"] = {}
        step["assert"] = [unauth]
        cases = [
            {
                "title": f"[安全] {feature.get('name', '')} - 未鉴权访问".strip(),
                "dimension": "security",
                "type": "api",
                "description": "不带 token 访问接口，验证服务端做了鉴权。",
                "preconditions": [{"kind": "none"}],
                "steps": steps + [step],
                "assertions": [unauth],
                "evidence": [],
                "tags": ["security", "auth"],
            }
        ]

        # 有路径参数的接口再补一条越权访问（用别人的 id）
        id_endpoint = _pick_endpoint(feature, endpoints)
        if id_endpoint is not None and _has_path_param(id_endpoint):
            id_path = normalize(id_endpoint.get("path") or "")
            other_path = re.sub(r"\{[A-Za-z0-9_]+\}", "999999", id_path)
            cross = {
                "kind": "status_code",
                "operator": "in",
                "expected": [401, 403, 404],
                "status": "confirmed" if secured else "pending",
                "evidence": evidence
                if secured
                else {"source_type": "none", "note": "接口未声明鉴权要求，越权期望待确认"},
                "description": "访问他人资源（id=999999）应被拒绝",
            }
            cross_step = {
                "action": "request",
                "name": f"越权访问 {other_path}",
                "method": (id_endpoint.get("method") or "GET").upper(),
                "path": other_path,
                "headers": {},
                "assert": [cross],
            }
            cases.append(
                {
                    "title": f"[安全] {feature.get('name', '')} - 越权访问他人数据".strip(),
                    "dimension": "security",
                    "type": "api",
                    "description": "使用 A 的身份访问 B 的资源，验证服务端做了归属校验。",
                    "preconditions": [{"kind": "http_login", "name": "attacker", "account_ref": "attacker"}],
                    "steps": [cross_step],
                    "assertions": [cross],
                    "evidence": [],
                    "tags": ["security", "idempotency"],
                }
            )
        return cases

    # ── 性能 ───────────────────────────────────────────────
    def _performance_cases(
        self,
        feature: dict[str, Any],
        endpoints: list[dict[str, Any]],
        docs: list[dict[str, Any]],
        base_url: str,
        sla: dict[str, Any],
        all_endpoints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        endpoint = _pick_endpoint(feature, endpoints, prefer_no_path_param=True)
        if endpoint is None:
            return []
        if not sla or not sla.get("threshold_ms"):
            return [
                {
                    "title": f"[性能] {feature.get('name', '')} - 缺少 SLA 阈值".strip(),
                    "dimension": "performance",
                    "type": "api",
                    "description": "文档中未给出响应时间 SLA，按约定不硬编阈值，标记缺少输入。",
                    "preconditions": [{"kind": "none"}],
                    "steps": [],
                    "assertions": [],
                    "evidence": [],
                    "tags": ["performance"],
                    "missing_input": "缺少输入：文档未提供该接口的响应时间 SLA 阈值",
                }
            ]
        method = (endpoint.get("method") or "GET").upper()
        path = normalize(endpoint.get("path") or "/")
        threshold = float(sla["threshold_ms"])
        assertion = {
            "kind": "timing",
            "operator": "lt",
            "expected": threshold,
            "threshold_ms": threshold,
            "status": "confirmed",
            "evidence": sla.get("evidence") or {"source_type": "doc", "ref": sla.get("ref", "")},
            "description": f"响应时间应小于 {threshold} ms（依据：{sla.get('ref', 'SLA')}）",
        }
        body = {name: _sample_value(name, prop) for name, prop in _body_fields(endpoint)}
        step = _base_step(endpoint, method, path, body if body else None)
        step["assert"] = [assertion]
        return [
            {
                "title": f"[性能] {feature.get('name', '')} - 响应时间基线".strip(),
                "dimension": "performance",
                "type": "api",
                "description": "接口级响应时间基线（第一版不做压测）。",
                "preconditions": [{"kind": "none"}],
                "steps": [step],
                "assertions": [assertion],
                "evidence": [assertion["evidence"]],
                "tags": ["performance", "baseline"],
            }
        ]

    # ── 失败归因 ───────────────────────────────────────────
    def _failure_attribution(self, context: dict[str, Any]) -> dict[str, Any]:
        result = context.get("result") or {}
        case = context.get("case") or {}
        response = result.get("response") or {}
        status = response.get("status_code")
        body = response.get("body")
        errors = result.get("errors") or []
        evidence: list[dict[str, Any]] = []

        failed_assertions = [
            a for a in (result.get("assertions") or []) if not a.get("passed") and not a.get("pending")
        ]
        if status is not None and int(status) >= 500:
            category = "environment"
            reason = f"服务返回 {status}，倾向环境/服务端异常：{str(body)[:200]}"
        elif any("连接" in str(e) or "timeout" in str(e).lower() or "refused" in str(e).lower() for e in errors):
            category = "environment"
            reason = f"请求未到达被测系统或超时：{errors}"
        elif failed_assertions and case.get("expected"):
            category = "defect"
            reason = "被测系统的实际响应与有依据的预期不一致，属于疑似真实缺陷"
            evidence = case.get("evidence") or []
        else:
            category = "case"
            reason = "失败更可能来自用例本身（数据、断言或前置条件），缺少被测系统异常证据"

        return {
            "category": category,
            "reason": reason,
            "confidence": 0.6 if category != "defect" else 0.75,
            "evidence": evidence,
            "is_defect": category == "defect",
        }

    # ── 白盒走查 ───────────────────────────────────────────
    _RISK_RULES: list[tuple[str, str, str]] = [
        (r"except\s*:\s*\n\s*pass", "exception_swallowed", "异常被静默吞没，错误无法感知"),
        (r"except\s+Exception\s*:\s*\n\s*(pass|continue|return\b)", "exception_swallowed", "宽泛异常被吞没"),
        (r"print\s*\(\s*(password|pwd|token|secret)", "sensitive_log", "敏感信息被打印到日志"),
        (r"logger?\.(info|debug|warning)\s*\([^)]*\b(password|token|secret|id_card|phone)\b", "sensitive_log", "日志中可能泄露敏感字段"),
        (r"execute\s*\(\s*f?[\"'].*\{.*\}", "sql_injection", "SQL 使用字符串拼接，存在注入风险"),
        (r"eval\s*\(", "dangerous_eval", "使用 eval 执行动态字符串"),
        (r"exec\s*\(", "dangerous_exec", "使用 exec 执行动态代码"),
        (r"verify\s*=\s*False", "auth_disabled", "显式关闭了证书/签名校验"),
        (r"allow_origins\s*=\s*\[\s*[\"']\*[\"']", "cors_wildcard", "CORS 允许任意来源"),
    ]

    def _whitebox_review(self, context: dict[str, Any]) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = context.get("code_chunks") or []
        findings: list[dict[str, Any]] = []
        for chunk in chunks:
            content = chunk.get("content") or ""
            locator = chunk.get("locator") or {}
            file_path = locator.get("file_path") or ""
            start_line = int(locator.get("start_line") or 1)
            lines = content.splitlines()
            for pattern, risk_type, description in self._RISK_RULES:
                for match in re.finditer(pattern, content, flags=re.MULTILINE):
                    line_no = start_line + content[: match.start()].count("\n")
                    snippet = lines[min(line_no - start_line, len(lines) - 1)] if lines else ""
                    findings.append(
                        {
                            "title": f"{description}（{risk_type}）",
                            "description": f"文件 {file_path} 第 {line_no} 行附近存在风险：{description}。\n代码片段：{snippet.strip()[:200]}",
                            "severity": "high" if risk_type in {"sql_injection", "dangerous_eval", "auth_disabled"} else "medium",
                            "risk_type": risk_type,
                            "module": file_path.split("/")[0] if "/" in file_path else file_path,
                            "code_location": {
                                "file_path": file_path,
                                "start_line": line_no,
                                "end_line": line_no,
                                "snippet": snippet.strip()[:300],
                            },
                            "evidence": [{"source_type": "code", "ref": f"{file_path}:{line_no}", "quote": snippet.strip()[:200]}],
                        }
                    )
        return {"findings": findings}
