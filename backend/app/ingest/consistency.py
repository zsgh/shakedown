"""三方一致性对照：OpenAPI × 代码路由 × 文档描述。

对照结果本身就是「可疑点」——很可能是缺陷线索。
"""
from __future__ import annotations

import re
from typing import Any

from app.ingest.docapi import is_dev_log

# 文档里出现的接口：GET /api/users/{id}、POST /api/users、`/api/users`
# 前面加负向断言：避免把 URL / import 路径的后半截当成路径
_DOC_PATH = re.compile(
    r"(?<![\w./-])(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)?\s*[`\"']?(?P<path>/[A-Za-z0-9_\-{}/.:<>]*)",
    re.IGNORECASE,
)
_DOC_KEYWORDS = ("接口", "endpoint", "api", "路由", "请求", "curl")

# 系统路径 / 非接口路径前缀
_SYSTEM_PATH_PREFIXES = (
    "/usr",
    "/etc",
    "/bin",
    "/sbin",
    "/lib",
    "/var",
    "/opt",
    "/home",
    "/Users",
    "/Library",
    "/System",
    "/Applications",
    "/tmp",
    "/private",
    "/dev",
    "/proc",
    "/pkg",
    "/src",
    "/go",
)


def _looks_like_file_path(path: str) -> bool:
    """排除文件路径（/.bash_profile、/etc/hosts、/foo/bar.txt 之类）。"""
    last = path.rstrip("/").split("/")[-1]
    if not last:
        return True
    if last.startswith("."):
        return True
    if "." in last:
        suffix = last.rsplit(".", 1)[-1]
        if suffix.isalpha() and len(suffix) <= 5:
            return True
    return False


def _strip_host(path: str) -> str:
    """把 ``//host:port/api/x`` / ``http://host/api/x`` 还原成 ``/api/x``。"""
    text = path
    if "://" in text:
        text = text.split("://", 1)[1]
        text = "/" + text.split("/", 1)[1] if "/" in text else "/"
    elif text.startswith("//"):
        text = text[2:]
        text = "/" + text.split("/", 1)[1] if "/" in text else "/"
    return text or "/"


def _looks_like_api_path(path: str) -> bool:
    """是否像接口路径：含 /api 段、版本段，或带路径参数。"""
    lowered = path.lower()
    if re.search(r"/(api|apis|v\d+|rest|graphql|openapi)(/|$)", lowered):
        return True
    return "{" in path


def _is_system_path(path: str) -> bool:
    """系统目录（/Users/xxx、/usr/local、/Library/...）不是接口路径。"""
    for prefix in _SYSTEM_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def normalize_path(path: str) -> str:
    if not path:
        return "/"
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    # Starlette 的 {path:path} 与普通 {path} 是同一个路径模板
    path = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^}]+\}", r"{\1}", path)
    # {id} / :id / <id> 统一成 {param}
    path = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", path)
    path = re.sub(r"<([A-Za-z_][A-Za-z0-9_]*)>", r"{\1}", path)
    # Go ServeMux 的根路由 "/{$}" 等价于 "/"
    path = path.replace("/{$}", "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def path_pattern(path: str) -> re.Pattern[str]:
    """把接口路径转成能匹配文档文本的正则（参数段用通配）。"""
    normalized = normalize_path(path)
    parts = [re.escape(p) for p in normalized.split("/") if p]
    pattern = "/" + "/".join(parts)
    pattern = re.sub(r"\\\{[A-Za-z0-9_]+\\\}", lambda _m: r"[^/\s`\"']+", pattern)
    return re.compile(r"(?<![\w/])" + pattern + r"(?![\w])")


def extract_doc_mentions(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从文档切片里找接口描述（过滤文件路径等误报）。"""
    mentions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        content = chunk.get("content") or ""
        locator = chunk.get("locator") or {}
        file_path = locator.get("file_path") or ""
        if is_dev_log(file_path):
            continue  # 开发日志不作为接口规范
        ref = locator.get("file_path") or " > ".join(locator.get("section_path") or []) or f"chunk:{chunk.get('id')}"
        for match in _DOC_PATH.finditer(content):
            raw_path = match.group("path")
            if not raw_path or raw_path.count("/") < 1:
                continue
            # 路径参数写成 <name> 时，正则会在 "<" 处截断 —— 手工补到 ">" 并接上后续路径
            if "<" in raw_path and ">" not in raw_path:
                tail = content[match.end() : match.end() + 60]
                close = tail.find(">")
                if close >= 0:
                    raw_path = raw_path + tail[: close + 1]
                    rest = re.match(r"[A-Za-z0-9_\-{}/.:]*", tail[close + 1 :])
                    if rest:
                        raw_path += rest.group(0)
            method = (match.group("method") or "").upper()
            path = normalize_path(_strip_host(raw_path))
            if len(path) <= 1 or _looks_like_file_path(path) or _is_system_path(path):
                continue
            # 排除 "GET / POST / PUT" 这种把方法名当路径的误报
            segments = [s for s in path.split("/") if s]
            if segments and all(s.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} for s in segments):
                continue
            # 只有出现接口相关上下文、或路径本身像 API，才算「文档提到」
            window = content[max(0, match.start() - 60) : match.end() + 60]
            if not method and not _looks_like_api_path(path):
                if not any(keyword in window.lower() for keyword in ("接口", "endpoint", "路由", "请求", "curl")):
                    continue
            key = (method or "*", path)
            if key in seen:
                continue
            seen.add(key)
            mentions.append(
                {
                    "method": method or "*",
                    "path": path,
                    "ref": ref,
                    "chunk_id": chunk.get("id"),
                    "quote": window.strip()[:200],
                }
            )
    return mentions


def _key(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_path(path)}"


def _is_api_resource(path: str) -> bool:
    """根页面 / 静态资源不是 API，不参与接口一致性对照。"""
    normalized = normalize_path(path)
    if normalized in {"/", "/index", "/index.html"}:
        return False
    if normalized.startswith("/static") or normalized.startswith("/assets") or normalized.startswith("/public"):
        return False
    return True


def check_consistency(
    openapi_endpoints: list[dict[str, Any]],
    code_endpoints: list[dict[str, Any]],
    doc_mentions: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """返回 ``{"METHOD /path": {"state":..., "mismatches":[...], "sources":[...]}}``。

    状态含义：

    - ``consistent``   —— 各来源都覆盖且不冲突
    - ``inconsistent`` —— 强信号（文档提到但没实现 / 与 spec 冲突 / 参数不一致）
    - ``partial``      —— 弱信号（仅单侧存在：例如代码里有，但项目的 spec / 文档没覆盖）
    - ``unknown``      —— 项目只有这一类来源，无从对照
    """
    openapi_map: dict[str, dict[str, Any]] = {}
    for endpoint in openapi_endpoints:
        openapi_map[_key(endpoint.get("method", "GET"), endpoint.get("path", ""))] = endpoint

    code_map: dict[str, dict[str, Any]] = {}
    for endpoint in code_endpoints:
        code_map[_key(endpoint.get("method", "GET"), endpoint.get("path", ""))] = endpoint

    doc_map: dict[str, list[dict[str, Any]]] = {}
    for mention in doc_mentions:
        method = mention.get("method") or "*"
        path = normalize_path(mention.get("path", ""))
        doc_map.setdefault(f"{method} {path}", []).append(mention)
        if method == "*":
            doc_map.setdefault(f"ANY {path}", []).append(mention)

    # 项目里到底存在哪几类来源 —— 决定「缺失」算不算问题
    project_has = {"openapi": bool(openapi_map), "code": bool(code_map), "doc": bool(doc_mentions)}

    # 路径集合（忽略方法）：用来区分「接口不存在」和「只是方法没声明」
    openapi_paths = {normalize_path(e.get("path", "")) for e in openapi_map.values()}
    code_paths = {normalize_path(e.get("path", "")) for e in code_map.values()}
    doc_paths = {normalize_path(m.get("path", "")) for m in doc_mentions}

    all_keys = set(openapi_map) | set(code_map)
    for key in list(doc_map.keys()):
        # "ANY /x"（文档只提路径没说方法）只是辅助匹配项，不单独成键
        if key.startswith("ANY ") or key.startswith("* "):
            continue
        all_keys.add(key)

    report: dict[str, dict[str, Any]] = {}
    for key in sorted(all_keys):
        method, path = key.split(" ", 1)
        in_openapi = key in openapi_map
        in_code = key in code_map
        in_doc = key in doc_map or f"ANY {path}" in doc_map
        present = {"openapi": in_openapi, "code": in_code, "doc": in_doc}
        sources = [name for name, flag in present.items() if flag]

        strong: list[dict[str, Any]] = []
        weak: list[dict[str, Any]] = []

        # 非 API 资源（根页面 / 静态资源）不参与对照
        if not _is_api_resource(path):
            report[key] = {
                "state": "n/a",
                "sources": sources,
                "mismatches": [],
                "method": method,
                "path": path,
            }
            continue

        # 逐个维度看：这个来源项目里存在吗？存在却没覆盖该接口 → 才是缺口
        for dim, flag in present.items():
            if flag or not project_has[dim]:
                continue
            others = [name for name, other in present.items() if other and name != dim]
            where = "、".join(others)
            if dim == "openapi":
                # 路径在 spec 里存在、只是方法没声明 → 弱信号（实现比契约多，不是缺陷）
                if path in openapi_paths:
                    weak.append(
                        {
                            "kind": "method_only",
                            "message": f"{where}提到 {key}，但 OpenAPI spec 只声明了同路径的其它方法（契约未覆盖该方法）",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )
                else:
                    strong.append(
                        {
                            "kind": "code_only",
                            "message": f"{where}提到 {key}，但 OpenAPI spec 中没有（接口契约与实现不一致）",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )
            elif dim == "code":
                if path in code_paths:
                    weak.append(
                        {
                            "kind": "method_only",
                            "message": f"{where}提到 {key}，但代码里只注册了同路径的其它方法",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )
                else:
                    strong.append(
                        {
                            "kind": "spec_only",
                            "message": f"{where}提到 {key}，但代码中未找到对应路由（可能未实现）",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )
            else:  # doc
                if path in doc_paths:
                    weak.append(
                        {
                            "kind": "method_only",
                            "message": f"{where}提到 {key}，但文档只提到了同路径的其它方法",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )
                else:
                    weak.append(
                        {
                            "kind": "doc_missing",
                            "message": f"{key} 未在文档中出现（文档缺口，不算实现问题）",
                            "evidence": _evidence_of(present, openapi_map, code_map, doc_map, key, path),
                        }
                    )

        if in_openapi and in_doc:
            mismatch = _compare_openapi_doc(openapi_map[key], doc_map.get(key, []) + doc_map.get(f"ANY {path}", []))
            if mismatch:
                # 参数没写进文档属于「文档缺口」，是弱信号；真正的实现问题才是强信号
                weak.append(mismatch)

        if strong:
            state = "inconsistent"
        elif weak:
            state = "partial"
        elif len(sources) == 1 and not project_has["openapi"] and not project_has["doc"]:
            state = "unknown"
        else:
            state = "consistent"
        report[key] = {
            "state": state,
            "sources": sources,
            "mismatches": strong + weak,
            "method": method,
            "path": path,
        }
    return report


def _evidence_of(
    present: dict[str, bool],
    openapi_map: dict[str, Any],
    code_map: dict[str, Any],
    doc_map: dict[str, list[dict[str, Any]]],
    key: str,
    path: str,
) -> list[Any]:
    """挑出可作为证据的来源片段。"""
    evidence: list[Any] = []
    if present.get("openapi") and key in openapi_map:
        evidence.append(openapi_map[key])
    if present.get("code") and key in code_map:
        evidence.append(code_map[key])
    if present.get("doc"):
        evidence.extend(doc_map.get(key, [])[:2])
        evidence.extend(doc_map.get(f"ANY {path}", [])[:2])
    return evidence[:3]


def _compare_openapi_doc(openapi_endpoint: dict[str, Any], mentions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """比较 OpenAPI 声明与文档描述的参数差异。

    只比「spec 声明了、文档没提」这一个方向 —— 文档里的 query 多半是**示例值**
    （如 ``?id=...``、``?limit=200``），拿它反过来要求 spec 必须有，会制造大量误报。
    """
    if not mentions:
        return None
    spec_params: set[str] = set()
    for param in (openapi_endpoint.get("params") or {}).get("query", []) or []:
        if isinstance(param, dict) and param.get("name"):
            spec_params.add(str(param["name"]))
    body_props = set(((openapi_endpoint.get("request_body") or {}).get("schema") or {}).get("properties", {}).keys())

    doc_text = " ".join((m.get("quote") or "") for m in mentions)
    if not doc_text:
        return None
    doc_params = set(re.findall(r"[?&]([A-Za-z_][A-Za-z0-9_]*)=", doc_text))
    doc_params |= set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc_text))
    doc_params -= body_props
    missing_in_doc = sorted(spec_params - doc_params)
    if missing_in_doc:
        return {
            "kind": "param_mismatch",
            "message": f"{openapi_endpoint.get('method', 'GET')} {openapi_endpoint.get('path', '')} "
            f"spec 声明了参数 {missing_in_doc}，但文档里没有提到",
            "evidence": mentions[:3],
        }
    return None
