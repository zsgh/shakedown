"""知识源 / 接口清单 / 功能点 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import ParseStatus
from app.errors import NotFoundError
from app.ingest.service import ingest_source, recompute_consistency
from app.models import Chunk, Endpoint, Feature, KnowledgeSource, Project, TestCase
from app.schemas import (
    EndpointMerge,
    EndpointOut,
    FeatureCreate,
    FeatureOut,
    FeatureUpdate,
    KnowledgeSourceCreate,
    KnowledgeSourceOut,
    KnowledgeSourceUpdate,
)
from app.worker.queue import enqueue

router = APIRouter(tags=["knowledge"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


# ── 知识源 ──────────────────────────────────────────────────
@router.get("/projects/{project_id}/sources", response_model=list[KnowledgeSourceOut])
def list_sources(
    project_id: int,
    db: Session = Depends(get_db),
) -> list[KnowledgeSource]:
    return list(db.scalars(select(KnowledgeSource).where(KnowledgeSource.project_id == project_id)).all())


@router.post("/projects/{project_id}/sources", response_model=KnowledgeSourceOut, status_code=201)
def create_source(
    project_id: int,
    payload: KnowledgeSourceCreate,
    sync: bool = Query(default=False, description="同步解析（否则交给 worker）"),
    db: Session = Depends(get_db),
) -> KnowledgeSource:
    project = _project(db, project_id)
    source = KnowledgeSource(
        project_id=project.id,
        kind=payload.kind,
        name=payload.name or payload.location,
        location=payload.location,
        status=ParseStatus.PENDING.value,
        meta_json={"recursive": payload.recursive, "include_globs": payload.include_globs},
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    if sync:
        try:
            ingest_source(db, project, source)
        except Exception:  # noqa: BLE001 —— 错误已写入 source.error
            db.refresh(source)
    else:
        enqueue(db, "reparse_source", {"source_id": source.id}, project_id=project.id)
        db.commit()
    db.refresh(source)
    return source


@router.post("/projects/{project_id}/sources/discover")
def discover(project_id: int, payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    """扫描一个目录，返回里面有什么可导入（不落库）。"""
    from app.ingest.discovery import discover_repository

    _project(db, project_id)
    path = str(payload.get("path") or "").strip()
    if not path:
        raise NotFoundError("缺少 path 参数")
    try:
        return discover_repository(path).as_dict()
    except FileNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@router.post("/projects/{project_id}/sources/auto-import")
def auto_import(
    project_id: int,
    payload: dict[str, Any],
    parse: bool = Query(default=True, description="是否立即解析"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """给一个仓库路径，自动把代码 / 文档 / OpenAPI 都导进来。"""
    from app.ingest.autoimport import auto_import as run_auto_import

    project = _project(db, project_id)
    path = str(payload.get("path") or "").strip()
    if not path:
        raise NotFoundError("缺少 path 参数")
    kinds = payload.get("kinds") or None
    try:
        return run_auto_import(db, project, path, kinds=kinds, parse=parse)
    except FileNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@router.get("/sources/{source_id}", response_model=KnowledgeSourceOut)
def get_source(source_id: int, db: Session = Depends(get_db)) -> KnowledgeSource:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise NotFoundError(f"知识源不存在：{source_id}")
    return source


@router.patch("/sources/{source_id}", response_model=KnowledgeSourceOut)
def update_source(source_id: int, payload: KnowledgeSourceUpdate, db: Session = Depends(get_db)) -> KnowledgeSource:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise NotFoundError(f"知识源不存在：{source_id}")
    if payload.name is not None:
        source.name = payload.name
    if payload.location is not None:
        source.location = payload.location
    if payload.role is not None:
        if payload.role not in {"spec", "notes"}:
            raise NotFoundError(f"非法 role：{payload.role}（可选 spec / notes）")
        source.role = payload.role
        # 改了角色，chunk 上的标记也要跟着改
        from app.models import Chunk

        for chunk in db.scalars(select(Chunk).where(Chunk.source_id == source.id)).all():
            chunk.meta_json = {**(chunk.meta_json or {}), "role": source.role}
    db.commit()
    db.refresh(source)
    return source


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, db: Session = Depends(get_db)) -> None:
    from app.ingest.service import drop_source_endpoints

    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise NotFoundError(f"知识源不存在：{source_id}")
    project_id = source.project_id
    db.delete(source)
    db.flush()
    drop_source_endpoints(db, project_id, source_id)
    db.commit()


@router.post("/sources/{source_id}/reparse", response_model=KnowledgeSourceOut)
def reparse_source(
    source_id: int,
    sync: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> KnowledgeSource:
    source = db.get(KnowledgeSource, source_id)
    if source is None:
        raise NotFoundError(f"知识源不存在：{source_id}")
    project = db.get(Project, source.project_id)
    if sync:
        try:
            ingest_source(db, project, source)
        except Exception:  # noqa: BLE001
            db.refresh(source)
    else:
        source.status = ParseStatus.PENDING.value
        db.commit()
        enqueue(db, "reparse_source", {"source_id": source.id}, project_id=source.project_id)
        db.commit()
    db.refresh(source)
    return source


@router.get("/projects/{project_id}/spec-snapshots")
def list_spec_snapshots(
    project_id: int,
    source_id: int | None = None,
    limit: int = Query(default=50, ge=0, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Spec 快照历史（OpenAPI 每次重新导入都会留一份）。"""
    from app.models import SpecSnapshot

    _project(db, project_id)
    stmt = select(SpecSnapshot).where(SpecSnapshot.project_id == project_id)
    if source_id:
        stmt = stmt.where(SpecSnapshot.source_id == source_id)
    rows = db.scalars(stmt.order_by(SpecSnapshot.created_at.desc()).limit(limit)).all()
    sources = {
        source.id: source.name
        for source in db.scalars(
            select(KnowledgeSource).where(KnowledgeSource.project_id == project_id)
        ).all()
    }
    result = []
    for row in rows:
        diff = row.diff or {}
        result.append(
            {
                "id": row.id,
                "source_id": row.source_id,
                "source_name": sources.get(row.source_id, f"来源 #{row.source_id}"),
                "version": row.version,
                "content_hash": row.content_hash,
                "created_at": row.created_at,
                "endpoints": len((row.spec or {}).get("endpoints") or []),
                "origin": (row.spec or {}).get("origin", ""),
                "diff": diff,
                "diff_counts": {
                    "added": len(diff.get("added") or []),
                    "removed": len(diff.get("removed") or []),
                    "changed": len(diff.get("changed") or []),
                },
            }
        )
    return result


@router.get("/projects/{project_id}/spec-snapshots/compare")
def compare_spec_snapshots(
    project_id: int,
    base_id: int = Query(...),
    target_id: int = Query(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """对比两个 spec 快照，并找出受影响的用例。"""
    from app.ingest.consistency import normalize_path
    from app.ingest.openapi import diff_specs
    from app.models import SpecSnapshot, TestCase

    _project(db, project_id)
    base = db.get(SpecSnapshot, base_id)
    target = db.get(SpecSnapshot, target_id)
    if base is None or target is None:
        raise NotFoundError("快照不存在")
    if base.project_id != project_id or target.project_id != project_id:
        raise NotFoundError("快照不属于该项目")

    old_endpoints = list((base.spec or {}).get("endpoints") or [])
    new_endpoints = list((target.spec or {}).get("endpoints") or [])
    diff = diff_specs(old_endpoints, new_endpoints)

    touched = {
        f"{str(item.get('method', '')).upper()} {normalize_path(item.get('path', ''))}"
        for item in (diff.get("changed") or []) + (diff.get("removed") or [])
    }
    affected: list[dict[str, Any]] = []
    if touched:
        for case in db.scalars(select(TestCase).where(TestCase.project_id == project_id)).all():
            for step in (case.spec or {}).get("steps") or []:
                key = f"{str(step.get('method', '')).upper()} {normalize_path(step.get('path', ''))}"
                if key in touched:
                    affected.append({"id": case.id, "title": case.title, "status": case.status})
                    break

    return {
        "base": {
            "id": base.id,
            "version": base.version,
            "source_id": base.source_id,
            "created_at": base.created_at,
            "endpoints": len(old_endpoints),
        },
        "target": {
            "id": target.id,
            "version": target.version,
            "source_id": target.source_id,
            "created_at": target.created_at,
            "endpoints": len(new_endpoints),
        },
        "diff": diff,
        "affected_cases": affected[:200],
        "affected_total": len(affected),
    }


@router.get("/projects/{project_id}/change-impact")
def get_change_impact(
    project_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """最近一次代码/文档变更影响面：按来源和用例两个视角返回。"""
    _project(db, project_id)
    sources = db.scalars(
        select(KnowledgeSource).where(KnowledgeSource.project_id == project_id)
    ).all()
    source_rows: list[dict[str, Any]] = []
    for source in sources:
        impact = (source.meta_json or {}).get("change_impact")
        if not impact:
            continue
        source_rows.append(
            {
                "source_id": source.id,
                "name": source.name,
                "source_kind": source.kind,
                "role": source.role,
                "version": source.version,
                **impact,
            }
        )

    cases = db.scalars(
        select(TestCase)
        .where(TestCase.project_id == project_id)
        .order_by(TestCase.updated_at.desc())
    ).all()
    case_rows: list[dict[str, Any]] = []
    for case in cases:
        spec = case.spec or {}
        impact = spec.get("change_impact")
        candidates = spec.get("change_candidates") or []
        if not impact and not candidates:
            continue
        case_rows.append(
            {
                "id": case.id,
                "title": case.title,
                "status": case.status,
                "feature_id": case.feature_id,
                "change_impact": impact or {},
                "change_candidates": candidates[-5:],
            }
        )
        if len(case_rows) >= limit:
            break
    return {"sources": source_rows, "cases": case_rows, "total": len(case_rows)}


@router.get("/projects/{project_id}/chunks")
def list_chunks(
    project_id: int,
    source_id: int | None = None,
    limit: int = Query(default=50, ge=0, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Chunk).where(Chunk.project_id == project_id)
    if source_id:
        stmt = stmt.where(Chunk.source_id == source_id)
    chunks = db.scalars(stmt.limit(limit)).all()
    return [
        {
            "id": c.id,
            "source_id": c.source_id,
            "content": c.content[:2000],
            "locator": c.locator,
            "meta": c.meta_json,
            "content_hash": c.content_hash,
        }
        for c in chunks
    ]


# ── 接口清单 ────────────────────────────────────────────────
@router.get("/projects/{project_id}/endpoints", response_model=list[EndpointOut])
def list_endpoints(
    project_id: int,
    method: str | None = None,
    path_contains: str | None = None,
    inconsistent_only: bool = False,
    db: Session = Depends(get_db),
) -> list[Endpoint]:
    stmt = select(Endpoint).where(Endpoint.project_id == project_id)
    if method:
        stmt = stmt.where(Endpoint.method == method.upper())
    if path_contains:
        stmt = stmt.where(Endpoint.path.ilike(f"%{path_contains}%"))
    endpoints = list(db.scalars(stmt.order_by(Endpoint.path, Endpoint.method)).all())
    if inconsistent_only:
        endpoints = [e for e in endpoints if (e.consistency or {}).get("state") in {"inconsistent", "conflict"}]
    return endpoints

@router.get("/endpoints/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(endpoint_id: int, db: Session = Depends(get_db)) -> Endpoint:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise NotFoundError(f"接口不存在：{endpoint_id}")
    return endpoint


@router.post("/projects/{project_id}/endpoints", response_model=EndpointOut, status_code=201)
def merge_endpoint(project_id: int, payload: EndpointMerge, db: Session = Depends(get_db)) -> Endpoint:
    """把代码 / 文档侧抽到的接口候选合并进清单。"""
    from app.ingest.service import upsert_endpoint

    _project(db, project_id)
    endpoint = upsert_endpoint(
        db,
        project_id,
        payload.method,
        payload.path,
        payload.source_type,
        summary=payload.summary,
        params=payload.params,
        request_body=payload.request_body,
        responses=payload.responses,
        security=payload.security,
        ref=payload.ref,
    )
    db.commit()
    db.refresh(endpoint)
    return endpoint


@router.post("/projects/{project_id}/consistency/check")
def check_consistency(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    report = recompute_consistency(db, project)
    db.commit()
    return report


@router.get("/projects/{project_id}/consistency")
def consistency_report(
    project_id: int,
    include_weak: bool = Query(default=True, description="是否包含弱信号（文档缺口等改进项）"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """三方一致性对照结果。

    默认返回**强信号 + 弱信号**：
    - 强信号（inconsistent/conflict）：文档提到但没实现、与 spec 冲突、方法契约不一致 —— 值得人工看；
    - 弱信号（partial）：文档缺口等**改进项** —— 不是缺陷，但说明文档/契约该补。
    """
    _project(db, project_id)
    return _consistency_items(db, project_id, include_weak)


def _consistency_items(db: Session, project_id: int, include_weak: bool) -> list[dict[str, Any]]:
    endpoints = db.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all()
    wanted = {"inconsistent", "conflict"}
    if include_weak:
        wanted.add("partial")
    items: list[dict[str, Any]] = []
    for endpoint in endpoints:
        consistency = endpoint.consistency or {}
        state = consistency.get("state")
        if state in wanted:
            items.append(
                {
                    "endpoint_id": endpoint.id,
                    "method": endpoint.method,
                    "path": endpoint.path,
                    "summary": endpoint.summary,
                    "state": state,
                    "sources": consistency.get("sources", []),
                    "mismatches": consistency.get("mismatches", []),
                    "checked_at": consistency.get("checked_at", ""),
                }
            )
    return items


@router.get("/projects/{project_id}/consistency/export.md")
def export_consistency_markdown(
    project_id: int,
    request: Request,
    include_weak: bool = Query(default=True, description="是否包含弱信号（文档缺口等改进项）"),
    db: Session = Depends(get_db),
) -> Response:
    """导出「AI 可直接执行」的 Markdown 修复清单：带证据定位、修复指引与验收标准。

    导出后可直接交给编码 AI（Claude Code / Cursor 等）按清单修复。
    """
    from urllib.parse import quote as urlquote

    from app.reporting.consistency_md import render_consistency_markdown

    project = _project(db, project_id)
    items = _consistency_items(db, project_id, include_weak)
    sources = db.scalars(
        select(KnowledgeSource).where(KnowledgeSource.project_id == project_id).order_by(KnowledgeSource.kind)
    ).all()
    markdown = render_consistency_markdown(
        project_name=project.name,
        items=items,
        sources=[
            {"kind": s.kind, "role": s.role, "name": s.name, "location": s.location}
            for s in sources
        ],
        project_id=project_id,
        base_url=str(request.base_url),
    )
    filename = f"consistency-fixes-{project_id}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(filename)}"},
    )


@router.get("/projects/{project_id}/consistency/summary")
def consistency_summary(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """一致性汇总：强信号 / 弱信号 / 文档覆盖率 一览。"""
    _project(db, project_id)
    endpoints = db.scalars(select(Endpoint).where(Endpoint.project_id == project_id)).all()
    has_spec_docs = db.scalar(
        select(KnowledgeSource.id)
        .where(
            KnowledgeSource.project_id == project_id,
            KnowledgeSource.kind == "markdown",
            KnowledgeSource.role == "spec",
        )
        .limit(1)
    ) is not None
    counts: dict[str, int] = {}
    covered_by_doc = 0
    api_total = 0
    for endpoint in endpoints:
        state = (endpoint.consistency or {}).get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
        if state == "n/a":
            continue
        api_total += 1
        if "doc" in (endpoint.consistency or {}).get("sources", []):
            covered_by_doc += 1
    return {
        "endpoints": len(endpoints),
        "api_endpoints": api_total,
        "states": counts,
        "strong_signals": counts.get("inconsistent", 0) + counts.get("conflict", 0),
        "weak_signals": counts.get("partial", 0),
        "doc_coverage": (
            round(covered_by_doc / api_total, 4)
            if api_total and has_spec_docs
            else None
        ),
        "non_api": counts.get("n/a", 0),
    }


# ── 功能点 ──────────────────────────────────────────────────
@router.get("/projects/{project_id}/features", response_model=list[FeatureOut])
def list_features(project_id: int, db: Session = Depends(get_db)) -> list[Feature]:
    return list(db.scalars(select(Feature).where(Feature.project_id == project_id).order_by(Feature.module, Feature.id)).all())


@router.post("/projects/{project_id}/features", response_model=FeatureOut, status_code=201)
def create_feature(project_id: int, payload: FeatureCreate, db: Session = Depends(get_db)) -> Feature:
    _project(db, project_id)
    feature = Feature(
        project_id=project_id,
        name=payload.name,
        module=payload.module,
        description=payload.description,
        source_refs=payload.source_refs,
        endpoint_ids=payload.endpoint_ids,
        unmapped=not payload.endpoint_ids,
    )
    db.add(feature)
    db.commit()
    db.refresh(feature)
    return feature


@router.patch("/features/{feature_id}", response_model=FeatureOut)
def update_feature(feature_id: int, payload: FeatureUpdate, db: Session = Depends(get_db)) -> Feature:
    feature = db.get(Feature, feature_id)
    if feature is None:
        raise NotFoundError(f"功能点不存在：{feature_id}")
    data = payload.model_dump(exclude_unset=True)
    for key in ("name", "module", "description", "source_refs", "endpoint_ids"):
        if key in data and data[key] is not None:
            setattr(feature, key, data[key])
    if "endpoint_ids" in data:
        feature.unmapped = not data["endpoint_ids"]
    db.commit()
    db.refresh(feature)
    return feature


@router.delete("/features/{feature_id}", status_code=204)
def delete_feature(feature_id: int, db: Session = Depends(get_db)) -> None:
    feature = db.get(Feature, feature_id)
    if feature is None:
        raise NotFoundError(f"功能点不存在：{feature_id}")
    db.delete(feature)
    db.commit()
