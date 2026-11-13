"""OpenAPI / Swagger 解析。

支持：
- Swagger 2.0 与 OpenAPI 3.0 / 3.1；
- 本地 JSON/YAML 文件、URL、Swagger UI 页面地址（自动探测背后的 spec 地址）；
- ``$ref`` 展开（带循环引用保护）、``allOf`` / ``oneOf`` / ``anyOf`` 处理；
- 归一化为内部 endpoint 模型；
- 快照 + 内容 hash + 重新拉取后的 diff。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.logging import get_logger

logger = get_logger("shakedown.ingest.openapi")

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options", "trace"}

# Swagger UI 常见 spec 地址
SPEC_CANDIDATES = [
    "/v3/api-docs",
    "/v3/api-docs.yaml",
    "/openapi.json",
    "/swagger.json",
    "/api-docs",
    "/api-docs.json",
    "/swagger/v1/swagger.json",
    "/docs/openapi.json",
    "/openapi.yaml",
    "/swagger.yaml",
]


class OpenAPIError(Exception):
    """spec 解析失败（错误信息必须可读）。"""


@dataclass
class NormalizedEndpoint:
    method: str
    path: str
    summary: str = ""
    description: str = ""
    params: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"path": [], "query": [], "header": []})
    request_body: dict[str, Any] = field(default_factory=dict)
    responses: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    examples: dict[str, Any] = field(default_factory=dict)
    source_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "description": self.description,
            "params": self.params,
            "request_body": self.request_body,
            "responses": self.responses,
            "security": self.security,
            "examples": self.examples,
            "source_ref": self.source_ref,
        }


# ── 载入 ────────────────────────────────────────────────────
def _parse_yaml_or_json(text: str, origin: str) -> dict[str, Any]:
    stripped = text.lstrip()
    try:
        if stripped.startswith("{") or stripped.startswith("["):
            data = json.loads(text)
        else:
            data = yaml.safe_load(text)
    except json.JSONDecodeError as exc:
        raise OpenAPIError(f"{origin}: JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark else "未知位置"
        raise OpenAPIError(f"{origin}: YAML 解析失败（{where}）：{exc}") from exc
    if not isinstance(data, dict):
        raise OpenAPIError(f"{origin}: spec 顶层必须是对象，实际是 {type(data).__name__}")
    return data


def _looks_like_spec(data: Any) -> bool:
    return isinstance(data, dict) and ("swagger" in data or "openapi" in data or "paths" in data)


def _extract_spec_url_from_html(html: str, base_url: str) -> str | None:
    """从「接口文档页」里找 spec 地址。

    支持两类写法：
    1. Swagger UI / Redoc 惯例：``url: "..."``、``specUrl``、``data-url``、``swagger-ui.js?url=...``；
    2. 自研页面：脚本里 ``fetch("/api/openapi.json")`` 之类。
    """
    patterns = [
        # Swagger UI / Redoc 惯例
        r'"url"\s*:\s*"([^"]+)"',
        r"url\s*:\s*['\"]([^'\"]+)['\"]",
        r'specUrl\s*[:=]\s*["\']([^"\']+)["\']',
        r'spec-url\s*=\s*["\']([^"\']+)["\']',
        r'data-url\s*=\s*["\']([^"\']+)["\']',
        r'swagger-ui[^"\']*\?url=([^"\'&]+)',
        # 自研页面：脚本里 fetch / axios 拉 spec
        r'fetch\(\s*["\']([^"\']+\.(?:json|ya?ml))["\']',
        r'fetch\(\s*["\'](/[^"\']+)["\']',
        r'(?:axios|get)\(\s*["\'](/[^"\']+\.(?:json|ya?ml))["\']',
        r'<script[^>]+src=["\']([^"\']*(?:openapi|swagger)[^"\']*)["\']',
        r'href=["\']([^"\']*(?:openapi|swagger)[^"\']*\.json)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            candidate = match.group(1).strip()
            if not candidate:
                continue
            if candidate.startswith(("http://", "https://")):
                return candidate
            if candidate.startswith("/"):
                from urllib.parse import urljoin

                return urljoin(base_url, candidate)
            # 相对路径（如 "openapi.json"）→ 相对当前页面解析
            from urllib.parse import urljoin

            return urljoin(base_url, candidate)
    return None


def _page_relative_spec_candidates(base_url: str) -> list[str]:
    """猜测 spec 放在文档页同目录下的常见文件名。"""
    from urllib.parse import urljoin

    directory = base_url.rsplit("/", 1)[0] + "/"
    names = ["openapi.json", "swagger.json", "api-docs.json", "openapi.yaml", "swagger.yaml", "api-docs"]
    return [urljoin(directory, name) for name in names]


def _fetch(url: str, timeout: float = 20.0, retries: int = 3) -> tuple[str, str]:
    """拉取 URL（带重试，应对偶发网络抖动）。"""
    import time

    last_error = ""
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url, headers={"Accept": "application/json, application/yaml, text/html;q=0.9"})
            if resp.status_code >= 400:
                raise OpenAPIError(f"拉取 spec 失败 {url}：HTTP {resp.status_code}")
            return resp.text, resp.headers.get("content-type", "")
        except OpenAPIError:
            raise
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise OpenAPIError(f"拉取 spec 失败 {url}（重试 {retries} 次）：{last_error}")


def load_spec(location: str, *, timeout: float = 20.0) -> tuple[dict[str, Any], str, str]:
    """载入 spec。

    返回 ``(spec, 原始文本, 实际来源)``。
    """
    location = location.strip()
    if location.startswith(("http://", "https://")):
        return _load_spec_from_url(location, timeout=timeout)
    path = Path(location).expanduser()
    if not path.exists():
        raise OpenAPIError(f"spec 文件不存在：{location}")
    text = path.read_text(encoding="utf-8", errors="replace")
    spec = _parse_yaml_or_json(text, str(path))
    if not _looks_like_spec(spec):
        raise OpenAPIError(f"{path}: 不是 OpenAPI/Swagger 文档（缺少 openapi/swagger/paths 字段）")
    _validate_or_raise(spec, str(path))
    return spec, text, str(path)


def _load_spec_from_url(url: str, *, timeout: float = 20.0) -> tuple[dict[str, Any], str, str]:
    text, content_type = _fetch(url, timeout=timeout)
    if "html" in content_type.lower() or text.lstrip().startswith("<"):
        spec_url = _extract_spec_url_from_html(text, url)
        candidates: list[str] = []
        if spec_url:
            candidates.append(spec_url)
        from urllib.parse import urljoin, urlparse

        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        candidates.extend(urljoin(base, c) for c in SPEC_CANDIDATES)
        # 文档页同目录下的常见 spec 文件名（如 /api/docs → /api/openapi.json）
        candidates.extend(_page_relative_spec_candidates(url))
        # 去重并保序
        seen_candidates: set[str] = set()
        ordered_candidates = [c for c in candidates if not (c in seen_candidates or seen_candidates.add(c))]
        errors: list[str] = []
        for candidate in ordered_candidates:
            try:
                cand_text, cand_type = _fetch(candidate, timeout=timeout)
            except OpenAPIError as exc:
                errors.append(str(exc))
                continue
            if "html" in cand_type.lower() or cand_text.lstrip().startswith("<"):
                continue
            spec = _parse_yaml_or_json(cand_text, candidate)
            if _looks_like_spec(spec):
                return spec, cand_text, candidate
            errors.append(f"{candidate}: 不像 spec")
        raise OpenAPIError(
            "无法从接口文档页定位 spec 地址，已尝试：\n- " + "\n- ".join(errors or ordered_candidates)
        )
    spec = _parse_yaml_or_json(text, url)
    if not _looks_like_spec(spec):
        raise OpenAPIError(f"{url}: 不是 OpenAPI/Swagger 文档")
    _validate_or_raise(spec, url)
    return spec, text, url


def _validate_or_raise(spec: dict[str, Any], origin: str) -> None:
    errors = validate_spec(spec)
    if errors:
        raise OpenAPIError(f"{origin}: spec 结构校验失败：\n- " + "\n- ".join(errors[:10]))


# ── 版本 / 校验 ────────────────────────────────────────────
def detect_version(spec: dict[str, Any]) -> str:
    if "swagger" in spec:
        return "2.0"
    version = str(spec.get("openapi", ""))
    if version.startswith("3.1"):
        return "3.1"
    if version.startswith("3.0"):
        return "3.0"
    if version.startswith("3"):
        return "3.x"
    raise OpenAPIError("无法识别 spec 版本：缺少 openapi / swagger 字段")


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """结构级自检（不依赖外部库也能给出可读错误）。"""
    errors: list[str] = []
    version = detect_version(spec)
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        errors.append("缺少 paths 对象")
        return errors
    for path, item in paths.items():
        if not path.startswith("/"):
            errors.append(f"路径 {path} 必须以 / 开头")
        if not isinstance(item, dict):
            errors.append(f"路径 {path} 的定义必须是对象")
            continue
        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                errors.append(f"{method.upper()} {path}: 操作定义必须是对象")
                continue
            params = operation.get("parameters")
            if params is not None and not isinstance(params, list):
                errors.append(f"{method.upper()} {path}: parameters 必须是数组")
            responses = operation.get("responses")
            if version == "2.0" and not isinstance(responses, dict):
                errors.append(f"{method.upper()} {path}: Swagger 2.0 缺少 responses")
            if version.startswith("3") and not isinstance(responses, dict):
                errors.append(f"{method.upper()} {path}: 缺少 responses")
    return errors


# ── $ref 解析 ───────────────────────────────────────────────
def _pointer_get(root: Any, pointer: str) -> Any:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/") if p]
    node = root
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise OpenAPIError(f"$ref 指向不存在的节点：#{pointer}")
    return node


def resolve_refs(spec: dict[str, Any]) -> dict[str, Any]:
    """展开 ``$ref``（循环引用保护：遇到环就保留原样并标记）。"""
    resolved: dict[str, Any] = {}

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                if ref in stack:
                    return {"$ref": ref, "x-circular": True}
                try:
                    target = _pointer_get(spec, ref[1:])
                except OpenAPIError as exc:
                    raise OpenAPIError(f"解析 $ref 失败：{exc}") from exc
                merged = walk(copy.deepcopy(target), stack + (ref,))
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                if siblings and isinstance(merged, dict):
                    merged = {**merged, **walk(siblings, stack)}
                return merged
            return {k: walk(v, stack) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, stack) for v in node]
        return node

    resolved = walk(spec, ())
    return resolved


# ── schema 归一化 ──────────────────────────────────────────
def merge_all_of(schema: dict[str, Any]) -> dict[str, Any]:
    """合并 allOf：属性并集，required 并集，子 schema 保留。"""
    if not isinstance(schema, dict):
        return {}
    if "allOf" not in schema:
        return schema
    merged: dict[str, Any] = {k: v for k, v in schema.items() if k != "allOf"}
    properties: dict[str, Any] = dict(merged.get("properties") or {})
    required: list[str] = list(merged.get("required") or [])
    for sub in schema.get("allOf") or []:
        sub_merged = merge_all_of(sub if isinstance(sub, dict) else {})
        for key, value in (sub_merged.get("properties") or {}).items():
            properties.setdefault(key, value)
        for key in sub_merged.get("required") or []:
            if key not in required:
                required.append(key)
        for key, value in sub_merged.items():
            if key not in {"properties", "required"}:
                merged.setdefault(key, value)
    merged["properties"] = properties
    if required:
        merged["required"] = required
    merged.pop("allOf", None)
    return merged


def flatten_variants(schema: dict[str, Any]) -> dict[str, Any]:
    """处理 oneOf / anyOf：保留变体，同时给出属性并集方便断言。"""
    if not isinstance(schema, dict):
        return {}
    out = dict(schema)
    for key in ("oneOf", "anyOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        props: dict[str, Any] = {}
        for variant in variants:
            merged = merge_all_of(variant if isinstance(variant, dict) else {})
            for prop_name, prop in (merged.get("properties") or {}).items():
                props.setdefault(prop_name, prop)
        if props:
            out.setdefault("properties", {})
            for prop_name, prop in props.items():
                out["properties"].setdefault(prop_name, prop)
            out["x_variant_properties"] = sorted(props.keys())
    return out


def normalize_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    merged = merge_all_of(schema)
    return flatten_variants(merged)


# ── 鉴权 ────────────────────────────────────────────────────
def _security_schemes(spec: dict[str, Any], version: str) -> dict[str, dict[str, Any]]:
    if version == "2.0":
        raw = spec.get("securityDefinitions") or {}
    else:
        raw = ((spec.get("components") or {}).get("securitySchemes")) or {}
    schemes: dict[str, dict[str, Any]] = {}
    for name, definition in (raw or {}).items():
        definition = definition or {}
        schemes[name] = {
            "id": name,
            "type": definition.get("type", ""),
            "scheme": definition.get("scheme", ""),
            "in": definition.get("in", ""),
            "name": definition.get("name", ""),
            "flows": definition.get("flows", {}),
            "description": definition.get("description", ""),
        }
    return schemes


def _normalize_security(requirements: Any, schemes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result: list[dict[str, Any]] = []
    if isinstance(requirements, list):
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            for scheme_name, scopes in requirement.items():
                scheme = schemes.get(scheme_name, {"id": scheme_name, "type": "unknown"})
                result.append({**scheme, "scopes": scopes or []})
    return {"schemes": result, "required": bool(result)}


# ── 归一化 ──────────────────────────────────────────────────
def _collect_parameters(operation: dict[str, Any], path_item: dict[str, Any], version: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"path": [], "query": [], "header": [], "cookie": []}
    raw_params: list[dict[str, Any]] = []
    raw_params.extend(path_item.get("parameters") or [])
    raw_params.extend(operation.get("parameters") or [])
    for param in raw_params:
        if not isinstance(param, dict):
            continue
        location = param.get("in", "query")
        schema = normalize_schema(param.get("schema") or {"type": param.get("type"), "format": param.get("format")})
        entry = {
            "name": param.get("name", ""),
            "in": location,
            "required": bool(param.get("required", location == "path")),
            "description": param.get("description", ""),
            "schema": schema,
            "example": param.get("example"),
        }
        grouped.setdefault(location, []).append(entry)
    return grouped


def _request_body(operation: dict[str, Any], version: str) -> dict[str, Any]:
    if version == "2.0":
        for param in operation.get("parameters") or []:
            if isinstance(param, dict) and param.get("in") == "body":
                schema = normalize_schema(param.get("schema") or {})
                return {
                    "required": bool(param.get("required")),
                    "schema": schema,
                    "media_type": "application/json",
                    "description": param.get("description", ""),
                }
        return {}
    body = operation.get("requestBody") or {}
    if not isinstance(body, dict) or not body:
        return {}
    content = body.get("content") or {}
    media_type = "application/json"
    for candidate in ("application/json", "application/x-www-form-urlencoded", "multipart/form-data"):
        if candidate in content:
            media_type = candidate
            break
    media = content.get(media_type) or {}
    schema = normalize_schema(media.get("schema") or {})
    return {
        "required": bool(body.get("required")),
        "schema": schema,
        "media_type": media_type,
        "description": body.get("description", ""),
        "content_types": sorted(content.keys()),
    }


def _responses(operation: dict[str, Any], version: str) -> dict[str, Any]:
    raw = operation.get("responses") or {}
    out: dict[str, Any] = {}
    for status, definition in raw.items():
        if not isinstance(definition, dict):
            continue
        entry: dict[str, Any] = {"description": definition.get("description", "")}
        if version == "2.0":
            schema = normalize_schema(definition.get("schema") or {})
            if schema:
                entry["schema"] = schema
            if definition.get("headers"):
                entry["headers"] = definition["headers"]
        else:
            content = definition.get("content") or {}
            media_type = "application/json" if "application/json" in content else (next(iter(content), ""))
            media = content.get(media_type) or {}
            schema = normalize_schema(media.get("schema") or {})
            if schema:
                entry["schema"] = schema
                entry["media_type"] = media_type
            if definition.get("headers"):
                entry["headers"] = definition["headers"]
        out[str(status)] = entry
    return out


def _examples(operation: dict[str, Any], params: dict[str, list[dict[str, Any]]], version: str) -> dict[str, Any]:
    examples: dict[str, Any] = {}
    for group in params.values():
        for param in group:
            if param.get("example") is not None:
                examples.setdefault("parameters", {})[param["name"]] = param["example"]
    if version == "2.0":
        for status, definition in (operation.get("responses") or {}).items():
            if isinstance(definition, dict) and definition.get("examples"):
                examples.setdefault("responses", {})[str(status)] = definition["examples"]
    else:
        body = operation.get("requestBody") or {}
        for media_type, media in (body.get("content") or {}).items():
            if isinstance(media, dict) and media.get("example"):
                examples.setdefault("request", {})[media_type] = media["example"]
            if isinstance(media, dict) and media.get("examples"):
                examples.setdefault("request", {})[media_type] = media["examples"]
        for status, definition in (operation.get("responses") or {}).items():
            if not isinstance(definition, dict):
                continue
            for media_type, media in (definition.get("content") or {}).items():
                if isinstance(media, dict) and (media.get("example") or media.get("examples")):
                    examples.setdefault("responses", {})[f"{status}:{media_type}"] = media.get("example") or media.get("examples")
    return examples


def normalize(spec: dict[str, Any]) -> list[NormalizedEndpoint]:
    """把 spec 归一化为内部 endpoint 列表。"""
    version = detect_version(spec)
    resolved = resolve_refs(spec)
    schemes = _security_schemes(spec, version)
    global_security = resolved.get("security")
    endpoints: list[NormalizedEndpoint] = []

    for path, path_item in (resolved.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_params = path_item.get("parameters") or []
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            params = _collect_parameters(operation, {"parameters": path_params}, version)
            requirements = operation.get("security", global_security)
            endpoint = NormalizedEndpoint(
                method=method.upper(),
                path=path,
                summary=operation.get("summary", "") or operation.get("operationId", ""),
                description=operation.get("description", ""),
                params=params,
                request_body=_request_body(operation, version),
                responses=_responses(operation, version),
                security=_normalize_security(requirements, schemes),
                examples=_examples(operation, params, version),
                source_ref=f"#/paths/{path.replace('/', '~1')}/{method.lower()}",
            )
            endpoints.append(endpoint)
    return endpoints


# ── hash / diff ────────────────────────────────────────────
def content_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _endpoint_fingerprint(endpoint: dict[str, Any]) -> str:
    payload = {
        "params": {k: [p.get("name") for p in v] for k, v in (endpoint.get("params") or {}).items()},
        "body": sorted(((endpoint.get("request_body") or {}).get("schema") or {}).get("properties", {}).keys()),
        "responses": sorted(((endpoint.get("responses") or {}).get("200", {}).get("schema") or {}).get("properties", {}).keys()),
        "security": bool((endpoint.get("security") or {}).get("required")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def diff_specs(old_endpoints: list[dict[str, Any]], new_endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    """对比两份 endpoint 清单，输出新增 / 删除 / 变更。"""
    old_map = {(e["method"], e["path"]): e for e in old_endpoints}
    new_map = {(e["method"], e["path"]): e for e in new_endpoints}
    added = [
        {"method": m, "path": p, "summary": new_map[(m, p)].get("summary", "")}
        for (m, p) in sorted(set(new_map) - set(old_map))
    ]
    removed = [
        {"method": m, "path": p, "summary": old_map[(m, p)].get("summary", "")}
        for (m, p) in sorted(set(old_map) - set(new_map))
    ]
    changed: list[dict[str, Any]] = []
    for key in sorted(set(old_map) & set(new_map)):
        old_ep, new_ep = old_map[key], new_map[key]
        old_fp = _endpoint_fingerprint(old_ep)
        new_fp = _endpoint_fingerprint(new_ep)
        if old_fp == new_fp:
            continue
        details: list[str] = []
        old_params = {k: [p.get("name") for p in v] for k, v in (old_ep.get("params") or {}).items()}
        new_params = {k: [p.get("name") for p in v] for k, v in (new_ep.get("params") or {}).items()}
        if old_params != new_params:
            details.append(f"参数变化：{old_params} -> {new_params}")
        old_body = sorted(((old_ep.get("request_body") or {}).get("schema") or {}).get("properties", {}).keys())
        new_body = sorted(((new_ep.get("request_body") or {}).get("schema") or {}).get("properties", {}).keys())
        if old_body != new_body:
            details.append(f"请求体字段变化：{old_body} -> {new_body}")
        old_resp = sorted(((old_ep.get("responses") or {}).get("200", {}).get("schema") or {}).get("properties", {}).keys())
        new_resp = sorted(((new_ep.get("responses") or {}).get("200", {}).get("schema") or {}).get("properties", {}).keys())
        if old_resp != new_resp:
            details.append(f"响应字段变化：{old_resp} -> {new_resp}")
        changed.append({"method": key[0], "path": key[1], "details": details, "old_hash": old_fp, "new_hash": new_fp})
    return {"added": added, "removed": removed, "changed": changed}
