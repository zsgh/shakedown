"""代码 / 文档变更影响面：结构快照、结构 diff、用例映射与语义候选。

设计原则：
- 不把「文件变了」直接等于「用例受影响」；
- 先比较路由、代码符号、文档章节等结构；
- 再通过 Endpoint / Feature / TestCase 的现有关系找受影响用例；
- 结构判断不了的业务语义变化，只生成候选，不直接判失败。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Endpoint, Feature, KnowledgeSource, Project, TestCase

MAX_SECTION_TEXT = 4000
MAX_SYMBOLS_PER_FILE = 300
MAX_SEMANTIC_CASES = 40
_MAX_CANDIDATES = 5
_SCHEMA_SYMBOL = re.compile(
    r"(request|response|schema|model|enum|state|status|config|dto|payload|entity)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(text: str) -> str:
    return sha256(str(text or "").encode("utf-8")).hexdigest()


def _normalize_path(path: str) -> str:
    # 延迟导入，避免 app.ingest.__init__ → service → change_impact 的循环。
    from app.ingest.consistency import normalize_path

    return normalize_path(path)


def _route_key(method: str, path: str) -> str:
    return f"{str(method or '').upper()} {_normalize_path(str(path or ''))}"


def _find_symbol(chunks: list[Any], line: int) -> Any | None:
    candidates = [
        chunk
        for chunk in chunks
        if int(getattr(chunk, "start_line", 0) or 0) <= line <= int(getattr(chunk, "end_line", 0) or 0)
    ]
    if not candidates:
        # Python 装饰器和函数体可能不在同一个 chunk；取最近 5 行内的函数块。
        after = [
            chunk
            for chunk in chunks
            if 0 <= int(getattr(chunk, "start_line", 0) or 0) - line <= 5
        ]
        if after:
            return min(after, key=lambda chunk: int(getattr(chunk, "start_line", 0) or 0))
        before = [
            chunk
            for chunk in chunks
            if 0 <= line - int(getattr(chunk, "end_line", 0) or 0) <= 5
        ]
        if before:
            return max(before, key=lambda chunk: int(getattr(chunk, "end_line", 0) or 0))
        return None
    return min(candidates, key=lambda chunk: int(getattr(chunk, "end_line", 0)) - int(getattr(chunk, "start_line", 0)))


def build_code_snapshot(
    version: str,
    routes: list[dict[str, Any]],
    chunks_by_file: dict[str, list[Any]],
) -> dict[str, Any]:
    """代码结构快照：路由、处理函数符号、符号 hash。"""
    files: dict[str, Any] = {}
    symbols: dict[str, Any] = {}
    for file_path, chunks in chunks_by_file.items():
        entries: list[dict[str, Any]] = []
        for chunk in chunks[:MAX_SYMBOLS_PER_FILE]:
            name = str(getattr(chunk, "symbol", "") or "").strip()
            kind = str(getattr(chunk, "kind", "") or "block")
            content = str(getattr(chunk, "content", "") or "")
            entry = {
                "name": name,
                "kind": kind,
                "start": int(getattr(chunk, "start_line", 0) or 0),
                "end": int(getattr(chunk, "end_line", 0) or 0),
                "hash": _hash(content),
            }
            entries.append(entry)
            if name:
                symbols[f"{file_path}::{kind}::{name}"] = {**entry, "file": file_path}
        files[file_path] = {"symbols": entries}

    route_map: dict[str, Any] = {}
    for route in routes:
        key = _route_key(route.get("method", ""), route.get("path", ""))
        file_path = str(route.get("file_path") or "")
        line = int(route.get("line") or 0)
        symbol = _find_symbol(chunks_by_file.get(file_path, []), line)
        symbol_name = str(getattr(symbol, "symbol", "") or "") if symbol else ""
        symbol_hash = _hash(str(getattr(symbol, "content", "") or "")) if symbol else _hash(
            f"{file_path}:{line}:{route.get('snippet') or ''}"
        )
        route_map[key] = {
            "method": str(route.get("method") or "").upper(),
            "path": _normalize_path(str(route.get("path") or "")),
            "file": file_path,
            "line": line,
            "symbol": symbol_name,
            "hash": symbol_hash,
        }
    return {
        "kind": "code",
        "version": str(version or ""),
        "files": files,
        "routes": route_map,
        "symbols": symbols,
    }


def build_markdown_snapshot(
    version: str,
    chunks: list[Any],
    file_roles: dict[str, str],
) -> dict[str, Any]:
    """文档结构快照：文件 / 章节 / 章节 hash / 截断正文。"""
    files: dict[str, Any] = {}
    sections: dict[str, Any] = {}
    for chunk in chunks:
        locator = chunk.locator if isinstance(chunk.locator, dict) else {}
        file_path = str(locator.get("file_path") or "")
        if not file_path:
            continue
        section_path = [str(item) for item in (locator.get("section_path") or [])]
        heading = str(locator.get("heading") or (section_path[-1] if section_path else ""))
        key = f"{file_path}::{(' > '.join(section_path) or '(root)')}"
        if key in sections:
            continue
        entry = {
            "file": file_path,
            "section": section_path,
            "heading": heading,
            "start": int(locator.get("start_line") or 0),
            "end": int(locator.get("end_line") or 0),
            "hash": str(getattr(chunk, "content_hash", "") or ""),
            "text": str(getattr(chunk, "content", "") or "")[:MAX_SECTION_TEXT],
            "role": file_roles.get(file_path, "notes"),
        }
        sections[key] = entry
        files.setdefault(file_path, {"role": entry["role"], "sections": []})
        files[file_path]["sections"].append(
            {
                "path": section_path,
                "heading": heading,
                "start": entry["start"],
                "end": entry["end"],
                "hash": entry["hash"],
            }
        )
    return {
        "kind": "markdown",
        "version": str(version or ""),
        "files": files,
        "sections": sections,
    }


def _diff_code(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_routes = old.get("routes") or {}
    new_routes = new.get("routes") or {}
    old_symbols = old.get("symbols") or {}
    new_symbols = new.get("symbols") or {}
    added_routes = sorted(set(new_routes) - set(old_routes))
    removed_routes = sorted(set(old_routes) - set(new_routes))
    changed_routes = [
        {"key": key, "before": old_routes[key], "after": new_routes[key]}
        for key in sorted(set(old_routes) & set(new_routes))
        if old_routes[key].get("hash") != new_routes[key].get("hash")
        or old_routes[key].get("file") != new_routes[key].get("file")
        or old_routes[key].get("symbol") != new_routes[key].get("symbol")
    ]
    added_symbols = sorted(set(new_symbols) - set(old_symbols))
    removed_symbols = sorted(set(old_symbols) - set(new_symbols))
    changed_symbols = [
        {"key": key, "before": old_symbols[key], "after": new_symbols[key]}
        for key in sorted(set(old_symbols) & set(new_symbols))
        if old_symbols[key].get("hash") != new_symbols[key].get("hash")
    ]
    changed_files = set()
    for key in added_routes:
        changed_files.add((new_routes.get(key) or {}).get("file"))
    for key in removed_routes:
        changed_files.add((old_routes.get(key) or {}).get("file"))
    for item in changed_routes:
        changed_files.add(item["after"].get("file"))
    for item in [*added_symbols, *removed_symbols]:
        changed_files.add((new_symbols.get(item) or old_symbols.get(item) or {}).get("file"))
    for item in changed_symbols:
        changed_files.add(item["after"].get("file"))
    return {
        "kind": "code",
        "old_version": old.get("version", ""),
        "version": new.get("version", ""),
        "added_routes": added_routes,
        "removed_routes": removed_routes,
        "changed_routes": changed_routes,
        "added_symbols": added_symbols,
        "removed_symbols": removed_symbols,
        "changed_symbols": changed_symbols,
        "changed_files": sorted(item for item in changed_files if item),
    }


def _diff_markdown(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    old_sections = old.get("sections") or {}
    new_sections = new.get("sections") or {}
    added = [new_sections[key] for key in sorted(set(new_sections) - set(old_sections))]
    removed = [old_sections[key] for key in sorted(set(old_sections) - set(new_sections))]
    changed = [
        {"before": old_sections[key], "after": new_sections[key]}
        for key in sorted(set(old_sections) & set(new_sections))
        if old_sections[key].get("hash") != new_sections[key].get("hash")
    ]
    changed_files = {item.get("file") for item in [*added, *removed] if item.get("file")}
    changed_files.update(c["after"].get("file") for c in changed if c["after"].get("file"))
    return {
        "kind": "markdown",
        "old_version": old.get("version", ""),
        "version": new.get("version", ""),
        "added_sections": added,
        "removed_sections": removed,
        "changed_sections": changed,
        "changed_files": sorted(changed_files),
    }


def diff_snapshots(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """比较两版结构快照；没有旧快照时返回 baseline。"""
    if not old or not new:
        return {
            "kind": (new or old or {}).get("kind", ""),
            "baseline": True,
            "has_changes": False,
            "changed_files": [],
        }
    if old.get("kind") != new.get("kind"):
        return {
            "kind": new.get("kind", ""),
            "baseline": False,
            "has_changes": True,
            "changed_files": sorted(
                set((old.get("files") or {}).keys()) | set((new.get("files") or {}).keys())
            ),
        }
    result = _diff_code(old, new) if new.get("kind") == "code" else _diff_markdown(old, new)
    result["baseline"] = False
    result["has_changes"] = bool(
        result.get("added_routes")
        or result.get("removed_routes")
        or result.get("changed_routes")
        or result.get("added_sections")
        or result.get("removed_sections")
        or result.get("changed_sections")
        or result.get("added_symbols")
        or result.get("removed_symbols")
        or result.get("changed_symbols")
    )
    return result


def _source_ref_matches(ref: Any, file_path: str, section: list[str] | None = None) -> bool:
    text = str(ref or "")
    if not file_path:
        return False
    name = file_path.replace("\\", "/").rsplit("/", 1)[-1]
    if file_path not in text and not (name and name in text):
        return False
    if not section or "#" not in text:
        return True
    ref_section = text.split("#", 1)[1].strip().lower()
    section_text = " > ".join(section).strip().lower()
    return bool(ref_section) and (ref_section in section_text or section_text in ref_section)


def _endpoint_has_source(endpoint: Endpoint, source: KnowledgeSource) -> bool:
    for item in endpoint.sources or []:
        if not isinstance(item, dict):
            continue
        if item.get("source_id") == source.id:
            return True
        # 老数据可能没有 source_id；同类型才兜底，避免跨来源误伤。
        if item.get("source_id") is None and item.get("type") == source.kind:
            return True
    return False


def _case_step_keys(case: TestCase) -> set[str]:
    keys: set[str] = set()
    steps = (case.spec or {}).get("steps") or case.steps or []
    for step in steps:
        if isinstance(step, dict) and step.get("method") and step.get("path"):
            keys.add(_route_key(step.get("method", ""), step.get("path", "")))
    return keys


def impact_cases(
    session: Session,
    project: Project,
    source: KnowledgeSource,
    diff: dict[str, Any],
) -> dict[str, Any]:
    """把结构 diff 映射到用例；返回 direct / related / 新接口等摘要。"""
    endpoints = list(session.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all())
    features = list(session.scalars(select(Feature).where(Feature.project_id == project.id)).all())
    cases = list(session.scalars(select(TestCase).where(TestCase.project_id == project.id)).all())
    by_key = {_route_key(endpoint.method, endpoint.path): endpoint for endpoint in endpoints}
    endpoint_features: dict[int, list[Feature]] = defaultdict(list)
    for feature in features:
        for endpoint_id in feature.endpoint_ids or []:
            endpoint_features[int(endpoint_id)].append(feature)
    feature_cases: dict[int, list[TestCase]] = defaultdict(list)
    steps_by_case: dict[int, set[str]] = {}
    for case in cases:
        steps_by_case[case.id] = _case_step_keys(case)
        if case.feature_id:
            feature_cases[case.feature_id].append(case)

    levels: dict[int, str] = {}
    reasons: dict[int, list[dict[str, Any]]] = defaultdict(list)

    def add(case: TestCase, level: str, reason: dict[str, Any]) -> None:
        if case.status == "disabled":
            return
        current = levels.get(case.id)
        if current == "direct" and level != "direct":
            return
        if current is None or level == "direct":
            levels[case.id] = level
        reasons[case.id].append(reason)

    def add_key_cases(key: str, level: str, reason: dict[str, Any]) -> None:
        for case in cases:
            if key in steps_by_case.get(case.id, set()):
                add(case, level, {**reason, "endpoint": key})

    def add_endpoint(endpoint: Endpoint | None, level: str, reason: dict[str, Any]) -> None:
        if endpoint is None:
            return
        key = _route_key(endpoint.method, endpoint.path)
        add_key_cases(key, level, reason)
        for feature in endpoint_features.get(endpoint.id, []):
            for case in feature_cases.get(feature.id, []):
                add(case, "related", {**reason, "endpoint": key, "feature": feature.name})

    new_endpoints: list[str] = []
    removed_endpoints: list[str] = []
    if diff.get("kind") == "code":
        for key in diff.get("added_routes") or []:
            new_endpoints.append(key)
            add_key_cases(key, "related", {"kind": "route_added", "detail": f"代码新增路由：{key}"})
        for key in diff.get("removed_routes") or []:
            removed_endpoints.append(key)
            reason = {"kind": "route_removed", "detail": f"代码路由移除：{key}"}
            add_endpoint(by_key.get(key), "direct", reason)
            add_key_cases(key, "direct", reason)
        for item in diff.get("changed_routes") or []:
            key = item.get("key", "")
            add_endpoint(
                by_key.get(key),
                "direct",
                {
                    "kind": "route_changed",
                    "detail": f"代码路由处理函数变化：{key}（{item.get('after', {}).get('symbol') or '未识别符号'}）",
                },
            )
        for item in diff.get("changed_symbols") or []:
            symbol = item.get("after") or {}
            name = str(symbol.get("name") or "")
            if not _SCHEMA_SYMBOL.search(name):
                continue
            file_path = str(symbol.get("file") or "")
            for endpoint in endpoints:
                if not _endpoint_has_source(endpoint, source):
                    continue
                if not any(_source_ref_matches(item.get("ref"), file_path) for item in endpoint.sources or []):
                    continue
                add_endpoint(
                    endpoint,
                    "related",
                    {
                        "kind": "schema_symbol_changed",
                        "detail": f"模型/状态符号变化：{name}（{file_path}）",
                    },
                )
    elif diff.get("kind") == "markdown":
        for item in diff.get("changed_sections") or []:
            before, after = item.get("before") or {}, item.get("after") or {}
            file_path = str(after.get("file") or before.get("file") or "")
            role = str(after.get("role") or before.get("role") or "notes")
            section_path = list(after.get("section") or before.get("section") or [])
            section = " > ".join(section_path) or "(root)"
            if role == "notes":
                continue  # 说明类文档交给语义候选层，不硬标 needs_review
            for endpoint in endpoints:
                if not _endpoint_has_source(endpoint, source):
                    continue
                if any(_source_ref_matches(item.get("ref"), file_path) for item in endpoint.sources or []):
                    add_endpoint(
                        endpoint,
                        "related",
                        {"kind": "doc_section_changed", "detail": f"文档章节变化：{file_path} > {section}"},
                    )
            for feature in features:
                if not any(
                    _source_ref_matches(item.get("ref"), file_path, section_path)
                    for item in feature.source_refs or []
                ):
                    continue
                for case in feature_cases.get(feature.id, []):
                    add(
                        case,
                        "related",
                        {
                            "kind": "doc_section_changed",
                            "detail": f"文档章节变化：{file_path} > {section}，影响功能点 {feature.name}",
                            "feature": feature.name,
                        },
                    )
        for item in diff.get("removed_sections") or []:
            file_path = str(item.get("file") or "")
            role = str(item.get("role") or "notes")
            if role == "notes":
                continue
            section_path = list(item.get("section") or [])
            section = " > ".join(section_path) or "(root)"
            for endpoint in endpoints:
                if not _endpoint_has_source(endpoint, source):
                    continue
                if any(_source_ref_matches(ref.get("ref"), file_path) for ref in endpoint.sources or []):
                    add_endpoint(
                        endpoint,
                        "related",
                        {"kind": "doc_section_removed", "detail": f"文档章节移除：{file_path} > {section}"},
                    )
            for feature in features:
                if not any(
                    _source_ref_matches(ref.get("ref"), file_path, section_path)
                    for ref in feature.source_refs or []
                ):
                    continue
                for case in feature_cases.get(feature.id, []):
                    add(
                        case,
                        "related",
                        {
                            "kind": "doc_section_removed",
                            "detail": f"文档章节移除：{file_path} > {section}，影响功能点 {feature.name}",
                            "feature": feature.name,
                        },
                    )
    affected = [
        {
            "case_id": case_id,
            "level": level,
            "reasons": reasons[case_id][:10],
        }
        for case_id, level in sorted(levels.items())
    ]
    return {
        "kind": diff.get("kind", ""),
        "changed_files": diff.get("changed_files") or [],
        "affected": affected,
        "direct": sum(1 for item in affected if item["level"] == "direct"),
        "related": sum(1 for item in affected if item["level"] == "related"),
        "new_endpoints": new_endpoints,
        "removed_endpoints": removed_endpoints,
    }


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    words.update("".join(chinese[index : index + 2]) for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if len(word) >= 2}


def _case_text(case: TestCase, feature: Feature | None) -> str:
    parts = [case.title or "", case.tags and " ".join(str(item) for item in case.tags) or ""]
    if feature is not None:
        parts.extend([feature.name or "", feature.module or "", feature.description or ""])
    for step in (case.spec or {}).get("steps") or case.steps or []:
        if isinstance(step, dict):
            parts.extend([str(step.get("method") or ""), str(step.get("path") or "")])
    return " ".join(parts)


def _heuristic_candidates(
    changes: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    change_text = " ".join(
        f"{item.get('before') or ''} {item.get('after') or ''} {item.get('section') or ''}"
        for item in changes
    )
    change_tokens = _tokens(change_text)
    if not change_tokens:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    for case in cases:
        overlap = len(change_tokens & _tokens(case.get("text") or ""))
        if not overlap:
            continue
        score = min(1.0, overlap / max(3, len(change_tokens) ** 0.5))
        scored.append((score + 0.2, case))
    scored.sort(key=lambda item: (-item[0], item[1].get("case_id", 0)))
    return [
        {
            "case_id": case["case_id"],
            "confidence": round(min(0.9, score), 2),
            "reason": f"变更文本与用例关键词重合（{', '.join(sorted(change_tokens)[:5])}）",
        }
        for score, case in scored[:5]
    ]


_SEMANTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["case_id", "reason"],
            },
        }
    },
    "required": ["candidates"],
}


def semantic_candidates(
    session: Session,
    project: Project,
    source: KnowledgeSource,
    diff: dict[str, Any],
) -> list[dict[str, Any]]:
    """第三层：对文档业务语义变化生成候选用例，不直接改用例状态。"""
    if diff.get("kind") != "markdown":
        return []
    changes = [
        {
            "file": (item.get("after") or item.get("before") or {}).get("file", ""),
            "section": " > ".join((item.get("after") or item.get("before") or {}).get("section") or []),
            "before": str((item.get("before") or {}).get("text", ""))[:1500],
            "after": str((item.get("after") or {}).get("text", ""))[:1500],
        }
        for item in (diff.get("changed_sections") or [])[:8]
    ]
    changes.extend(
        {
            "file": item.get("file", ""),
            "section": " > ".join(item.get("section") or []),
            "before": str(item.get("text", ""))[:1500],
            "after": "",
        }
        for item in (diff.get("removed_sections") or [])[:4]
    )
    if not changes:
        return []
    features = {
        feature.id: feature
        for feature in session.scalars(select(Feature).where(Feature.project_id == project.id)).all()
    }
    cases = list(session.scalars(select(TestCase).where(TestCase.project_id == project.id)).all())
    payload = [
        {
            "case_id": case.id,
            "title": case.title,
            "feature": features.get(case.feature_id).name if case.feature_id and features.get(case.feature_id) else "",
            "tags": list(case.tags or []),
            "text": _case_text(case, features.get(case.feature_id) if case.feature_id else None),
        }
        for case in cases
        if case.status != "disabled"
    ]
    # 先按关键词重合度挑候选，避免大项目里只把前 40 条送给模型。
    change_tokens = _tokens(
        " ".join(
            f"{item.get('before') or ''} {item.get('after') or ''}"
            for item in changes
        )
    )
    payload.sort(
        key=lambda item: (
            -len(change_tokens & _tokens(item.get("text") or "")),
            item["case_id"],
        )
    )
    payload = payload[:MAX_SEMANTIC_CASES]
    if not payload:
        return []

    try:
        from app.agent.providers import get_provider

        provider = get_provider()
        if getattr(provider, "name", "") == "mock":
            return _heuristic_candidates(changes, payload)
        result = provider.structured(
            "你是测试影响面分析助手。只判断文档业务语义变化可能影响哪些已有用例，"
            "输出候选，不要判断缺陷，不要编造用例 ID。",
            json.dumps({"changes": changes, "cases": payload}, ensure_ascii=False),
            _SEMANTIC_SCHEMA,
            context={"task": "change_impact", "changes": changes, "cases": payload},
        )
        candidates = result.get("candidates") or []
    except Exception:  # noqa: BLE001 —— LLM 不可用时退回确定性关键词候选
        candidates = _heuristic_candidates(changes, payload)
    valid_ids = {case.id for case in cases}
    cleaned: list[dict[str, Any]] = []
    for item in candidates:
        try:
            case_id = int(item.get("case_id"))
        except (TypeError, ValueError):
            continue
        if case_id not in valid_ids:
            continue
        try:
            confidence = float(item.get("confidence") or 0.5)
        except (TypeError, ValueError):
            confidence = 0.5
        cleaned.append(
            {
                "case_id": case_id,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(item.get("reason") or "文档语义变化")[:300],
            }
        )
    cleaned.sort(key=lambda item: (-item["confidence"], item["case_id"]))
    return cleaned[:5]


def apply_change_impact(
    session: Session,
    project: Project,
    source: KnowledgeSource,
    old_snapshot: dict[str, Any] | None,
    new_snapshot: dict[str, Any],
    *,
    semantic: bool = True,
) -> dict[str, Any]:
    """计算并落库变更影响；返回给 ingest 结果使用的摘要。"""
    diff = diff_snapshots(old_snapshot, new_snapshot)
    if not diff.get("has_changes"):
        return {"baseline": bool(diff.get("baseline")), "changed_files": [], "affected": [], "candidates": []}
    impacted = impact_cases(session, project, source, diff)
    now = _now()
    for item in impacted["affected"]:
        case = session.get(TestCase, item["case_id"])
        if case is None or case.status == "disabled":
            continue
        spec = dict(case.spec or {})
        reasons = item["reasons"]
        spec["change_impact"] = {
            "source_id": source.id,
            "source_name": source.name,
            "source_kind": source.kind,
            "version": new_snapshot.get("version", ""),
            "level": item["level"],
            "reasons": reasons,
            "at": now,
        }
        history = list(spec.get("change_impact_history") or [])
        history.append(spec["change_impact"])
        spec["change_impact_history"] = history[-5:]
        case.spec = spec
        tags = list(case.tags or [])
        if "change-impact" not in tags:
            tags.append("change-impact")
            case.tags = tags
        case.status = "needs_review"

    auto_enrich: dict[str, Any] = {}
    automation = project.settings_json.get("automation") if isinstance(project.settings_json, dict) else {}
    automation = automation if isinstance(automation, dict) else {}
    if automation.get("auto_enrich_impacted") and impacted["affected"]:
        try:
            from app.case_enrich import enqueue_case_ids

            auto_enrich = enqueue_case_ids(
                session,
                project,
                [item["case_id"] for item in impacted["affected"]],
                actor="auto",
                note="变更影响自动重新评估",
                limit=int(automation.get("auto_enrich_limit", 50)),
            )
        except Exception:  # noqa: BLE001
            auto_enrich = {"error": "自动入队失败"}

    auto_rerun: dict[str, Any] = {}
    if automation.get("auto_rerun_impacted") and impacted["affected"]:
        affected_ids = [int(item["case_id"]) for item in impacted["affected"]]
        try:
            from app.run_service import create_run_for_project

            run = create_run_for_project(
                session,
                project,
                trigger="change-impact",
                policy={
                    "mode": "impacted",
                    "max_cases": int(automation.get("auto_rerun_limit", 50)),
                    "allow_needs_review": True,
                },
                case_ids=affected_ids,
                note="变更影响自动回归",
                skip_empty=True,
                allow_needs_review=True,
            )
            auto_rerun = {"run_id": run.id if run else None, "created": bool(run)}
        except Exception:  # noqa: BLE001
            auto_rerun = {"error": "自动回归入队失败"}

    candidates: list[dict[str, Any]] = []
    if semantic:
        candidates = semantic_candidates(session, project, source, diff)
        direct_ids = {item["case_id"] for item in impacted["affected"]}
        for item in candidates:
            if item["case_id"] in direct_ids:
                continue
            case = session.get(TestCase, item["case_id"])
            if case is None or case.status == "disabled":
                continue
            spec = dict(case.spec or {})
            existing = list(spec.get("change_candidates") or [])
            existing.append(
                {
                    **item,
                    "source_id": source.id,
                    "source_name": source.name,
                    "version": new_snapshot.get("version", ""),
                    "at": now,
                }
            )
            spec["change_candidates"] = existing[-_MAX_CANDIDATES:]
            case.spec = spec

    return {
        "baseline": bool(diff.get("baseline")),
        "kind": diff.get("kind", ""),
        "old_version": diff.get("old_version", ""),
        "version": diff.get("version", ""),
        "changed_files": diff.get("changed_files") or [],
        "added_routes": diff.get("added_routes") or [],
        "removed_routes": diff.get("removed_routes") or [],
        "changed_routes": diff.get("changed_routes") or [],
        "added_sections": diff.get("added_sections") or [],
        "removed_sections": diff.get("removed_sections") or [],
        "changed_sections": diff.get("changed_sections") or [],
        "affected": impacted["affected"],
        "direct": impacted["direct"],
        "related": impacted["related"],
        "new_endpoints": impacted["new_endpoints"],
        "removed_endpoints": impacted["removed_endpoints"],
        "candidates": candidates,
        "auto_enrich": auto_enrich,
        "auto_rerun": auto_rerun,
    }
