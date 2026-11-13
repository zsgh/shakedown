"""知识源导入编排：把代码 / 文档 / spec 变成 chunk + endpoint。"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.enums import ConsistencyState, EndpointSource, KnowledgeKind, ParseStatus
from app.ingest.code import chunk_code_file, extract_go_helpers, extract_routes, iter_source_files, repo_version
from app.ingest.consistency import check_consistency, extract_doc_mentions, normalize_path, path_pattern
from app.change_impact import apply_change_impact, build_code_snapshot, build_markdown_snapshot
from app.ingest.docapi import extract_doc_endpoints, is_dev_log
from app.ingest.embeddings import Embedder
from app.ingest.markdown import MarkdownSourceError, resolve_markdown_inputs, split_markdown
from app.ingest.openapi import (
    OpenAPIError,
    content_hash as spec_content_hash,
    diff_specs,
    load_spec,
    normalize,
)
from app.logging import get_logger
from app.models import Chunk, Endpoint, KnowledgeSource, SpecSnapshot, TestCase

logger = get_logger("shakedown.ingest.service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_change_impact(session: Session, project: Any, source: KnowledgeSource, old_snapshot: Any, new_snapshot: Any) -> dict[str, Any]:
    """影响面分析是增强能力，分析失败不能让知识源导入失败。"""
    try:
        return apply_change_impact(session, project, source, old_snapshot, new_snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.warning("变更影响面分析失败（source=%s）：%s", source.id, exc)
        return {"error": str(exc), "changed_files": [], "affected": [], "candidates": []}


def _fingerprint(method: str, path: str) -> str:
    return hashlib.sha256(f"{method.upper()} {normalize_path(path)}".encode()).hexdigest()


# ── endpoint 合并 ───────────────────────────────────────────
def upsert_endpoint(
    session: Session,
    project_id: int,
    method: str,
    path: str,
    source_type: str,
    *,
    summary: str = "",
    params: dict[str, Any] | None = None,
    request_body: dict[str, Any] | None = None,
    responses: dict[str, Any] | None = None,
    security: dict[str, Any] | None = None,
    examples: dict[str, Any] | None = None,
    ref: str = "",
    evidence: dict[str, Any] | None = None,
    source_id: int | None = None,
) -> Endpoint:
    method = method.upper()
    path = normalize_path(path)
    endpoint = session.scalar(
        select(Endpoint).where(Endpoint.project_id == project_id, Endpoint.method == method, Endpoint.path == path)
    )
    if endpoint is None:
        endpoint = Endpoint(
            project_id=project_id,
            method=method,
            path=path,
            summary=summary,
            params=params or {},
            request_body=request_body or {},
            responses=responses or {},
            security=security or {},
            examples=examples or {},
            sources=[],
            consistency={"state": ConsistencyState.UNKNOWN.value, "mismatches": []},
            fingerprint=_fingerprint(method, path),
        )
        session.add(endpoint)
        session.flush()  # 让后续同路径的 upsert 能查到这一条（唯一约束）
    else:
        if summary:
            if source_type == EndpointSource.OPENAPI.value or not endpoint.summary:
                endpoint.summary = summary
        for attr, value in (
            ("params", params),
            ("request_body", request_body),
            ("responses", responses),
            ("security", security),
            ("examples", examples),
        ):
            if value:
                if not getattr(endpoint, attr):
                    setattr(endpoint, attr, value)
                elif source_type == EndpointSource.OPENAPI.value:
                    setattr(endpoint, attr, value)

    sources = list(endpoint.sources or [])
    existing = next((s for s in sources if s.get("type") == source_type and s.get("ref") == ref), None)
    if existing is None:
        existing = {"type": source_type, "ref": ref, "summary": summary}
        sources.append(existing)
    elif summary and not existing.get("summary"):
        existing["summary"] = summary
    if evidence:
        existing["evidence"] = evidence
    if source_id is not None:
        existing["source_id"] = source_id
    endpoint.sources = sources
    return endpoint


# ── 各类型导入 ─────────────────────────────────────────────
def drop_source_endpoints(session: Session, project_id: int, source_id: int) -> int:
    """删除知识源时，同步清掉它贡献的接口（否则会留下残骸）。"""
    endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all()
    removed = 0
    for endpoint in endpoints:
        sources = list(endpoint.sources or [])
        mine = [item for item in sources if item.get("source_id") == source_id]
        if not mine:
            continue
        remaining = [item for item in sources if item not in mine]
        if not remaining:
            session.delete(endpoint)
            removed += 1
        else:
            endpoint.sources = remaining
    session.flush()
    return removed


def prune_endpoints(
    session: Session,
    project_id: int,
    source_type: str,
    keep_keys: set[tuple[str, str]],
    *,
    source_id: int | None = None,
) -> int:
    """清理「本来源曾提供、现在不再提供」的接口记录。

    场景：代码重构后重新导入，旧接口（如 helper 展开出来的多余方法）必须消失，
    否则它们会以「幽灵接口」的形式污染接口清单与三方对照。

    按来源 id 精确清理；同一项目有多个同类型来源时不会互相误删。
    """
    endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all()
    removed = 0
    for endpoint in endpoints:
        sources = list(endpoint.sources or [])
        if not any(item.get("type") == source_type for item in sources):
            continue
        if source_id is not None:
            mine = [
                item
                for item in sources
                if item.get("type") == source_type
                and (item.get("source_id") == source_id or item.get("source_id") is None)
            ]
        else:
            mine = [item for item in sources if item.get("type") == source_type]
        if not mine:
            continue
        key = (endpoint.method.upper(), normalize_path(endpoint.path))
        if key in keep_keys:
            continue
        remaining = [item for item in sources if item not in mine]
        if not remaining:
            session.delete(endpoint)
            removed += 1
        else:
            endpoint.sources = remaining
    session.flush()
    return removed


def _router_prefix(content: str) -> str:
    match = re.search(r"APIRouter\s*\([^)]*?prefix\s*=\s*[\"']([^\"']+)[\"']", content)
    return match.group(1).rstrip("/") if match else ""


def _route_shape(path: str) -> str:
    normalized = normalize_path(path)
    parts = [("{}" if part.startswith("{") and part.endswith("}") else part) for part in normalized.split("/")]
    return "/".join(parts) or "/"


def _canonicalize_code_routes(
    session: Session,
    project_id: int,
    routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 FastAPI 装饰器的局部路径对回 OpenAPI 的挂载路径。

    代码抽取只看到 ``@router.get("/{project_id}")``，看不到
    ``APIRouter(prefix="/api")`` 和 ``projects.router`` 的挂载前缀。
    少一层 /api 会让同一个接口变成两条强信号。这里按方法 + 唯一后缀
    映射回 OpenAPI 路径；无法唯一匹配的保持原样，避免误吞真正代码独有接口。
    """
    openapi_endpoints = session.scalars(
        select(Endpoint).where(Endpoint.project_id == project_id)
    ).all()
    openapi_paths: dict[str, list[str]] = {}
    for endpoint in openapi_endpoints:
        source_types = {item.get("type") for item in (endpoint.sources or [])}
        if EndpointSource.OPENAPI.value not in source_types:
            continue
        openapi_paths.setdefault(endpoint.method.upper(), []).append(endpoint.path)

    for route in routes:
        method = route["method"].upper()
        shape = _route_shape(route["path"])
        if shape == "/":
            continue
        candidates = [
            path
            for path in openapi_paths.get(method, [])
            if _route_shape(path) == shape or _route_shape(path).endswith(shape)
        ]
        if candidates:
            candidates.sort(key=len)
            if len(candidates) == 1 or len(candidates[0]) < len(candidates[1]):
                route["path"] = candidates[0]
    return routes


def _ingest_code(session: Session, project: Any, source: KnowledgeSource, embedder: Embedder) -> dict[str, Any]:
    location = str(source.location).strip()
    if location.lower().startswith(("http://", "https://")):
        raise ValueError(
            "代码库知识源只支持本地已 clone 的目录（不支持 URL）。"
            f"请先 git clone 仓库，再把本地路径填进来；当前填的是：{location}"
        )
    root = Path(location).expanduser()
    if not root.exists():
        raise FileNotFoundError(
            f"代码目录不存在：{root}。请填写本地已 clone 仓库的绝对路径（例如 /Users/you/github/mymac）。"
        )
    if root.is_file():
        raise ValueError(f"代码知识源需要指向目录（当前是文件）：{root}")
    root = root.resolve()
    files = iter_source_files(root)
    version = repo_version(root)
    old_snapshot = (source.meta_json or {}).get("_structure_snapshot")

    session.execute(delete(Chunk).where(Chunk.source_id == source.id))
    session.flush()

    chunks: list[Chunk] = []
    chunks_by_file: dict[str, list[Any]] = {}
    routes: list[dict[str, Any]] = []
    # 先扫一遍全仓库，收集 Go 风格的「helper 批量注册路由」（helper 可能定义在别的文件）
    helpers: dict[str, list[str]] = {}
    for path in files:
        if path.suffix.lower() != ".go":
            continue
        try:
            helpers.update(extract_go_helpers(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    for path in files:
        code_chunks = chunk_code_file(path, root)
        if code_chunks:
            chunks_by_file[code_chunks[0].file_path] = code_chunks
        for code_chunk in code_chunks:
            chunks.append(
                Chunk(
                    project_id=project.id,
                    source_id=source.id,
                    content=code_chunk.content,
                    locator={
                        "file_path": code_chunk.file_path,
                        "start_line": code_chunk.start_line,
                        "end_line": code_chunk.end_line,
                        "symbol": code_chunk.symbol,
                        "kind": code_chunk.kind,
                    },
                    meta_json={"language": path.suffix.lstrip(".")},
                    content_hash=hashlib.sha256(code_chunk.content.encode()).hexdigest(),
                )
            )
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        router_prefix = _router_prefix(content)
        for route in extract_routes(
            path,
            content,
            root,
            helpers=helpers if path.suffix.lower() == ".go" else None,
        ):
            route_path = route.path
            if router_prefix and not route_path.startswith(router_prefix):
                route_path = (router_prefix + route_path) if route_path else router_prefix
            routes.append(
                {
                    "method": route.method,
                    "path": route_path,
                    "ref": f"{route.file_path}:{route.line}" if route.file_path else "",
                    "file_path": route.file_path,
                    "line": route.line,
                    "framework": route.framework,
                    "snippet": route.snippet,
                }
            )

    routes = _canonicalize_code_routes(session, project.id, routes)

    if chunks:
        vectors = embedder.embed_many([c.content for c in chunks])
        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector
    session.add_all(chunks)

    for route in routes:
        upsert_endpoint(
            session,
            project.id,
            route["method"],
            route["path"],
            EndpointSource.CODE.value,
            ref=route["ref"],
            summary=route["snippet"][:200],
            source_id=source.id,
        )
    session.flush()

    # 清理代码重构后消失的旧接口（否则会变成幽灵接口）
    keep = {(route["method"].upper(), normalize_path(route["path"])) for route in routes}
    pruned = prune_endpoints(
        session, project.id, EndpointSource.CODE.value, keep, source_id=source.id
    )
    new_snapshot = build_code_snapshot(version, routes, chunks_by_file)
    change_impact = _safe_change_impact(session, project, source, old_snapshot, new_snapshot)
    return {
        "version": version,
        "files": len(files),
        "chunks": len(chunks),
        "routes": len(routes),
        "pruned": pruned,
        "route_list": routes,
        "_structure_snapshot": new_snapshot,
        "change_impact": change_impact,
    }


def _ingest_markdown(session: Session, project: Any, source: KnowledgeSource, embedder: Embedder) -> dict[str, Any]:
    recursive = bool((source.meta_json or {}).get("recursive", True))
    old_snapshot = (source.meta_json or {}).get("_structure_snapshot")
    inputs = resolve_markdown_inputs(source.location, recursive=recursive)

    session.execute(delete(Chunk).where(Chunk.source_id == source.id))
    session.flush()

    chunks: list[Chunk] = []
    hasher_parts: list[str] = []
    file_roles: dict[str, str] = {}

    for text, origin in inputs:
        file_chunks = split_markdown(text, origin)
        file_endpoints = extract_doc_endpoints(
            [
                {
                    "id": index,
                    "content": chunk.content,
                    "locator": {
                        "file_path": chunk.file_path,
                        "section_path": chunk.section_path,
                        "start_line": chunk.start_line,
                    },
                }
                for index, chunk in enumerate(file_chunks)
            ]
        )
        # 逐个文件判定角色：开发日志 → notes；含端点表 → spec；其余 → notes（设计说明）
        file_roles[origin] = (
            "notes"
            if is_dev_log(origin)
            else ("spec" if file_endpoints else "notes")
        )
        for md_chunk in file_chunks:
            hasher_parts.append(md_chunk.content_hash)
            chunks.append(
                Chunk(
                    project_id=project.id,
                    source_id=source.id,
                    content=md_chunk.content,
                    locator={
                        "file_path": origin,
                        "section_path": md_chunk.section_path,
                        "heading": md_chunk.heading,
                        "start_line": md_chunk.start_line,
                        "end_line": md_chunk.end_line,
                    },
                    meta_json={"section": " > ".join(md_chunk.section_path), "role": file_roles[origin]},
                    content_hash=md_chunk.content_hash,
                )
            )
    version = hashlib.sha256("".join(hasher_parts).encode()).hexdigest()[:32]
    if chunks:
        vectors = embedder.embed_many([c.content for c in chunks])
        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector
    session.add_all(chunks)
    session.flush()

    # 文档里若含端点清单（METHOD + path + 说明），抽出来登记成接口（来源 doc）
    doc_endpoints = extract_doc_endpoints(
        [
            {"id": chunk.id, "content": chunk.content, "locator": chunk.locator}
            for chunk in chunks
        ]
    )
    doc_keys: set[tuple[str, str]] = set()
    for endpoint in doc_endpoints:
        path = endpoint.path.split("?")[0]
        doc_keys.add((endpoint.method.upper(), normalize_path(path)))
        upsert_endpoint(
            session,
            project.id,
            endpoint.method,
            path,
            EndpointSource.DOC.value,
            summary=endpoint.summary,
            ref=f"{endpoint.locator.get('file_path', '')}:{endpoint.locator.get('line', '')}",
            source_id=source.id,
        )
    session.flush()
    pruned = prune_endpoints(
        session, project.id, EndpointSource.DOC.value, doc_keys, source_id=source.id
    )

    # 源级角色 = 各文件角色的汇总（有规范文件就是 spec，否则 notes）
    role = "spec" if "spec" in file_roles.values() else "notes"
    source.role = role
    source.meta_json = {
        **(source.meta_json or {}),
        "file_roles": {Path(path).name: value for path, value in file_roles.items()},
    }
    session.flush()

    new_snapshot = build_markdown_snapshot(version, chunks, file_roles)
    change_impact = _safe_change_impact(session, project, source, old_snapshot, new_snapshot)
    return {
        "version": version,
        "files": len(inputs),
        "chunks": len(chunks),
        "doc_endpoints": len(doc_endpoints),
        "pruned": pruned,
        "role": role,
        "file_roles": {Path(path).name: value for path, value in file_roles.items()},
        "_structure_snapshot": new_snapshot,
        "change_impact": change_impact,
    }


def _ingest_openapi(session: Session, project: Any, source: KnowledgeSource, embedder: Embedder) -> dict[str, Any]:
    spec, raw_text, origin = load_spec(source.location)
    normalized = normalize(spec)
    version = spec_content_hash(spec)

    previous = session.scalar(
        select(SpecSnapshot)
        .where(SpecSnapshot.source_id == source.id)
        .order_by(SpecSnapshot.created_at.desc())
        .limit(1)
    )
    old_endpoints: list[dict[str, Any]] = []
    if previous:
        old_endpoints = [
            {
                "method": e.get("method", "GET"),
                "path": e.get("path", ""),
                "params": e.get("params", {}),
                "request_body": e.get("request_body", {}),
                "responses": e.get("responses", {}),
                "security": e.get("security", {}),
                "summary": e.get("summary", ""),
            }
            for e in (previous.spec or {}).get("endpoints", [])
        ]

    new_endpoints = [e.as_dict() for e in normalized]
    diff = diff_specs(old_endpoints, new_endpoints) if previous else {"added": [], "removed": [], "changed": []}

    for endpoint in normalized:
        upsert_endpoint(
            session,
            project.id,
            endpoint.method,
            endpoint.path,
            EndpointSource.OPENAPI.value,
            summary=endpoint.summary,
            params=endpoint.params,
            request_body=endpoint.request_body,
            responses=endpoint.responses,
            security=endpoint.security,
            examples=endpoint.examples,
            ref=endpoint.source_ref,
            source_id=source.id,
        )

    # spec 里删掉的接口要从清单里消失
    pruned = prune_endpoints(
        session,
        project.id,
        EndpointSource.OPENAPI.value,
        {(e.method.upper(), normalize_path(e.path)) for e in normalized},
        source_id=source.id,
    )

    snapshot = SpecSnapshot(
        project_id=project.id,
        source_id=source.id,
        version=version,
        content_hash=version,
        spec={"endpoints": new_endpoints, "origin": origin, "raw_size": len(raw_text)},
        diff=diff,
    )
    session.add(snapshot)
    session.flush()

    affected = _mark_affected_cases(session, project.id, diff)
    return {
        "version": version,
        "origin": origin,
        "endpoints": len(normalized),
        "pruned": pruned,
        "diff": diff,
        "affected_cases": affected,
    }


def _mark_affected_cases(session: Session, project_id: int, diff: dict[str, Any]) -> list[int]:
    """spec 变更时，把受影响的用例标记为 needs_review。"""
    touched: list[str] = [f"{item['method'].upper()} {item['path']}" for item in diff.get("changed", [])]
    removed_keys = {f"{item['method'].upper()} {normalize_path(item['path'])}" for item in diff.get("removed", [])}
    affected: list[int] = []
    cases = session.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
    for case in cases:
        spec = case.spec or {}
        hit = False
        for step in spec.get("steps") or []:
            key = f"{str(step.get('method', '')).upper()} {normalize_path(step.get('path', ''))}"
            if key in removed_keys or key in touched:
                hit = True
                break
        if hit and case.status != "disabled":
            case.status = "needs_review"
            affected.append(case.id)
    return affected


# ── 入口 ────────────────────────────────────────────────────
def ingest_source(session: Session, project: Any, source: KnowledgeSource, embedder: Embedder | None = None) -> dict[str, Any]:
    """导入（或重新导入）一个知识源。"""
    embedder = embedder or Embedder()
    source.status = ParseStatus.PARSING.value
    source.error = None
    session.commit()

    try:
        if source.kind == KnowledgeKind.CODE.value:
            meta = _ingest_code(session, project, source, embedder)
        elif source.kind == KnowledgeKind.MARKDOWN.value:
            meta = _ingest_markdown(session, project, source, embedder)
        elif source.kind == KnowledgeKind.OPENAPI.value:
            meta = _ingest_openapi(session, project, source, embedder)
        else:
            raise ValueError(f"未知知识源类型：{source.kind}")
    except OpenAPIError as exc:
        source.status = ParseStatus.ERROR.value
        source.error = str(exc)
        session.commit()
        raise
    except (MarkdownSourceError, FileNotFoundError, ValueError) as exc:
        # 面向用户的可读错误：直接展示提示，不加异常类名
        source.status = ParseStatus.ERROR.value
        source.error = str(exc)
        session.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        source.status = ParseStatus.ERROR.value
        source.error = f"{type(exc).__name__}: {exc}"
        session.commit()
        raise

    source.status = ParseStatus.READY.value
    source.version = str(meta.get("version", ""))[:128]
    source.parsed_at = _now()
    try:
        from app.case_enrich import count_reenrich_candidates

        meta["reevaluate_candidates"] = count_reenrich_candidates(session, project)
    except Exception as exc:  # noqa: BLE001 —— 提示信息失败不影响知识源导入
        logger.warning("统计待重新评估用例失败（source=%s）：%s", source.id, exc)
        meta["reevaluate_candidates"] = 0
    # 路由明细不进 meta（接口已经落在 endpoints 表里），只留数量摘要
    source.meta_json = {
        k: (len(v) if k == "route_list" else v) for k, v in meta.items() if k != "route_list"
    }
    session.commit()

    recompute_consistency(session, project)
    session.commit()
    return meta


def recompute_consistency(session: Session, project: Any) -> dict[str, Any]:
    """三方一致性对照：OpenAPI × 代码 × 文档。"""
    endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all()
    openapi_endpoints: list[dict[str, Any]] = []
    code_endpoints: list[dict[str, Any]] = []
    for endpoint in endpoints:
        record = {
            "id": endpoint.id,
            "method": endpoint.method,
            "path": endpoint.path,
            "params": endpoint.params,
            "request_body": endpoint.request_body,
            "responses": endpoint.responses,
            "security": endpoint.security,
            "summary": endpoint.summary,
            "sources": endpoint.sources,
        }
        source_types = {s.get("type") for s in (endpoint.sources or [])}
        if EndpointSource.OPENAPI.value in source_types:
            openapi_endpoints.append(record)
        if EndpointSource.CODE.value in source_types:
            code_endpoints.append(record)

    doc_chunks = session.scalars(
        select(Chunk).where(
            Chunk.project_id == project.id,
            Chunk.source_id.in_(_markdown_source_ids(session, project.id, spec_only=True)),
        )
    ).all()
    mentions = extract_doc_mentions(
        [
            {
                "id": chunk.id,
                "content": chunk.content,
                "locator": chunk.locator,
            }
            for chunk in doc_chunks
        ]
    )
    # 文档里写的具体路径（/api/procs/566）归一到接口模板路径（/api/procs/{pid}）
    known_paths = [e.path for e in endpoints]
    for mention in mentions:
        if mention["path"] in known_paths:
            continue
        # 按「最长的模板优先」匹配，避免 /api/procs/{pid} 抢走 /api/procs/{pid}/signal
        for candidate in sorted((p for p in known_paths if "{" in p), key=len, reverse=True):
            if path_pattern(candidate).fullmatch(mention["path"]):
                mention["path"] = candidate
                break

    report = check_consistency(openapi_endpoints, code_endpoints, mentions)

    # 回写每个 endpoint 的一致性状态
    by_key = {(e.method.upper(), e.path): e for e in endpoints}
    doc_only_created = 0
    for key, info in report.items():
        method, path = key.split(" ", 1)
        endpoint = by_key.get((method, path))
        if endpoint is None:
            # 文档提到、但代码与 spec 都没有的接口 → 登记成 endpoint（来源 doc），
            # 这样它会出现在接口清单与一致性页里，作为「可能没实现」的可疑点
            if info["sources"] != ["doc"]:
                continue
            endpoint = Endpoint(
                project_id=project.id,
                method=method,
                path=path,
                summary="文档提到但未在代码 / spec 中找到",
                sources=[{"type": "doc", "ref": _first_doc_ref(info)}],
                consistency={
                    "state": info["state"],
                    "sources": info["sources"],
                    "mismatches": info["mismatches"],
                    "checked_at": _now().isoformat(),
                },
                fingerprint=hashlib.sha256(f"{method} {path}".encode()).hexdigest(),
            )
            session.add(endpoint)
            by_key[(method, path)] = endpoint
            doc_only_created += 1
            continue
        endpoint.consistency = {
            "state": info["state"],
            "sources": info["sources"],
            "mismatches": info["mismatches"],
            "checked_at": _now().isoformat(),
        }

    session.flush()
    return {
        "endpoints": len(report),
        "inconsistent": sum(1 for r in report.values() if r["state"] == ConsistencyState.INCONSISTENT.value),
        "unknown": sum(1 for r in report.values() if r["state"] == ConsistencyState.UNKNOWN.value),
        "consistent": sum(1 for r in report.values() if r["state"] == ConsistencyState.CONSISTENT.value),
        "partial": sum(1 for r in report.values() if r["state"] == "partial"),
        "doc_only": doc_only_created,
    }


def _first_doc_ref(info: dict[str, Any]) -> str:
    for mismatch in info.get("mismatches") or []:
        for evidence in mismatch.get("evidence") or []:
            if isinstance(evidence, dict) and evidence.get("ref"):
                return str(evidence["ref"])
    return ""


def _markdown_source_ids(session: Session, project_id: int, *, spec_only: bool = False) -> list[int]:
    from app.models import KnowledgeSource

    stmt = select(KnowledgeSource.id).where(
        KnowledgeSource.project_id == project_id, KnowledgeSource.kind == KnowledgeKind.MARKDOWN.value
    )
    if spec_only:
        # README / 架构说明是 notes，不是接口契约；把它们当 API 文档会制造
        # 大量 “文档没提到这个接口” 的弱信号。
        stmt = stmt.where(KnowledgeSource.role == "spec")
    ids = session.scalars(stmt).all()
    return list(ids)
