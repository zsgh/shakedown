"""业务 Agent：功能点抽取、用例生成、失败归因、白盒走查。

所有 Agent 都遵循同一套约定：
1. 素材以结构化 ``context`` 传给 provider（真实模型会序列化进 prompt，离线 provider 直接消费）；
2. 产出先过 schema 校验，不合法则带错误信息重试；
3. 每一步写 ``agent_traces``。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agent.providers.base import LLMError, LLMProvider
from app.agent.tools.base import ToolContext
from app.agent.tools.code_tools import CODE_TOOLS
from app.agent.tools.knowledge_tools import KNOWLEDGE_TOOLS
from app import failure_attribution
from app.enums import (
    AttributionCategory,
    CaseStatus,
    Dimension,
    EndpointSource,
    FindingSource,
    GenerationSource,
    KnowledgeKind,
    Severity,
)
from app.executor.case_schema import (
    collect_assertions,
    split_unbacked_assertions,
    validate_case_spec,
)
from app.executor.normalizer import _path_template
from app.crypto import decrypt_json
from app.logging import get_logger
from app.policies import policy_catalog
from app.models import (
    AgentTrace,
    CaseResult,
    Chunk,
    Endpoint,
    Feature,
    Finding,
    KnowledgeSource,
    Project,
    TestCase,
)

logger = get_logger("shakedown.agent.agents")

FEATURE_SCHEMA = {
    "type": "object",
    "properties": {
        "features": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "module": {"type": "string"},
                    "description": {"type": "string"},
                    # 来源引用必须写清「在哪」与「原文是什么」——这是后续断言依据的来源
                    "source_refs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_type": {
                                    "type": "string",
                                    "enum": ["doc", "openapi", "code", "manual"],
                                },
                                "ref": {"type": "string", "description": "定位串：文档路径/章节、spec 条目或 file:line"},
                                "quote": {"type": "string", "description": "原文片段（最多 80 字）"},
                            },
                            "required": ["ref", "quote"],
                        },
                    },
                    "endpoint_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "source_refs"],
            },
        }
    },
    "required": ["features"],
}

# 断言的统一定义（schema + 提示词示例共用同一套字段名）
_ASSERTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["status_code", "json_path", "db_state", "cross_api", "timing", "text_contains"],
        },
        "target": {"type": "string", "description": "json_path 的路径 / db_state 的查询名 / cross_api 的目标"},
        "operator": {
            "type": "string",
            "enum": ["eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "contains", "not_contains", "exists", "not_exists", "matches", "empty"],
        },
        "expected": {"description": "期望值（可以是对象/数组/数字/字符串）"},
        "threshold_ms": {"type": "number"},
        "status": {"type": "string", "enum": ["confirmed", "pending"]},
        "evidence": {
            "type": "object",
            "properties": {
                "source_type": {"type": "string", "enum": ["doc", "openapi", "code", "manual"]},
                "ref": {"type": "string", "description": "定位串：文档路径#章节 / spec 条目 / file:line"},
                "quote": {"type": "string", "description": "原文片段（最多 80 字）"},
            },
            "required": ["source_type", "ref"],
        },
        "description": {"type": "string"},
    },
    "required": ["kind", "status", "evidence"],
}

CASE_SCHEMA = {
    "type": "object",
    "$defs": {"assertion": _ASSERTION_SCHEMA},
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "dimension": {"type": "string", "enum": [d.value for d in Dimension]},
                    "type": {"type": "string", "enum": ["api", "web"]},
                    "description": {"type": "string"},
                    "feature": {"type": "object"},
                    "preconditions": {"type": "array"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["request", "browser", "db", "sleep"]},
                                "name": {"type": "string"},
                                "method": {"type": "string"},
                                "path": {"type": "string"},
                                "headers": {"type": "object"},
                                "query": {"type": "object"},
                                "body": {},
                                "save": {"type": "object"},
                                "assert": {"type": "array", "items": {"$ref": "#/$defs/assertion"}},
                            },
                            "required": ["action", "method", "path"],
                        },
                    },
                    "assertions": {"type": "array", "items": {"$ref": "#/$defs/assertion"}},
                    "cleanup": {"type": "array"},
                    "evidence": {"type": "array", "items": {"type": "object"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "missing_input": {"type": "string"},
                },
                "required": ["title", "dimension", "steps"],
            },
        }
    },
    "required": ["cases"],
}

ATTRIBUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["environment", "case", "defect", "unconfirmed"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "evidence": {"type": "array"},
        "is_defect": {"type": "boolean"},
    },
    "required": ["category", "reason"],
}

WHITEBOX_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"type": "string"},
                    "risk_type": {"type": "string"},
                    "module": {"type": "string"},
                    "code_location": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "snippet": {"type": "string"},
                        },
                    },
                    "evidence": {"type": "array"},
                },
                "required": ["title", "code_location"],
            },
        }
    },
    "required": ["findings"],
}


# ── trace ───────────────────────────────────────────────────
def record_trace(
    session: Session,
    *,
    project_id: int | None,
    kind: str,
    step: str,
    payload: dict[str, Any],
    run_id: int | None = None,
    task_id: int | None = None,
) -> None:
    trace = AgentTrace(
        project_id=project_id,
        run_id=run_id,
        task_id=task_id,
        kind=kind,
        step=step,
        model=payload.get("model", ""),
        input=payload.get("input", {}),
        output=payload.get("output", {}),
        tool_calls=payload.get("tool_calls", []),
        input_tokens=int(payload.get("usage", {}).get("input_tokens", 0) or 0),
        output_tokens=int(payload.get("usage", {}).get("output_tokens", 0) or 0),
        duration_ms=int(payload.get("duration_ms", 0) or 0),
        error=payload.get("error"),
    )
    session.add(trace)


def _project_context(session: Session, project: Project) -> ToolContext:
    roots: list[Path] = []
    for source in session.scalars(
        select(KnowledgeSource).where(KnowledgeSource.project_id == project.id, KnowledgeSource.kind == "code")
    ).all():
        path = Path(source.location).expanduser()
        if path.exists():
            roots.append(path.resolve())
    return ToolContext(session=session, project=project, code_roots=roots)


def _endpoints_payload(session: Session, project: Project) -> list[dict[str, Any]]:
    endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all()
    return [
        {
            "id": e.id,
            "method": e.method,
            "path": e.path,
            "summary": e.summary,
            "params": e.params,
            "request_body": e.request_body,
            "responses": e.responses,
            "security": e.security,
            "examples": e.examples,
            "sources": e.sources,
        }
        for e in endpoints
    ]


def _schema_brief(schema: Any, depth: int = 2) -> dict[str, Any]:
    """压缩 OpenAPI schema：保留类型 / 必填 / 枚举 / 嵌套字段，去掉长描述。

    生成器以前只看到 ``body`` 字段名，看不到类型，于是把 integer 写成
    ``"P2"``、把对象写成字符串。这里给模型足够的结构信息，同时控制深度。
    """
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("allOf"), list):
        merged: dict[str, Any] = {}
        for item in schema["allOf"]:
            merged.update(_schema_brief(item, depth))
        return merged
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for item in branches:
                branch = _schema_brief(item, depth)
                if branch.get("type") != "null":
                    return branch
    kind = schema.get("type")
    if not kind and "properties" in schema:
        kind = "object"
    brief: dict[str, Any] = {"type": kind} if kind else {}
    for key in ("format", "enum", "default", "minimum", "maximum", "minLength", "maxLength"):
        value = schema.get(key)
        if value not in (None, "", []):
            brief[key] = value
    if kind == "object":
        properties = schema.get("properties") or {}
        if depth <= 0:
            brief["properties"] = list(properties)[:40]
        else:
            brief["required"] = list(schema.get("required") or [])[:30]
            brief["properties"] = {
                name: _schema_brief(value, depth - 1)
                for name, value in list(properties.items())[:40]
            }
    elif kind == "array":
        brief["items"] = _schema_brief(schema.get("items") or {}, depth - 1)
    return brief


def _compact_endpoints(
    endpoints: list[dict[str, Any]],
    *,
    include_schema: bool = False,
) -> list[dict[str, Any]]:
    """精简版接口清单：只保留生成需要的信息（完整 schema 会把上下文撑爆）。"""
    compact: list[dict[str, Any]] = []
    for endpoint in endpoints:
        params = [
            f"{group}:{item.get('name')}"
            for group, items in (endpoint.get("params") or {}).items()
            for item in (items or [])
            if isinstance(item, dict) and item.get("name")
        ]
        body = list(((endpoint.get("request_body") or {}).get("schema") or {}).get("properties", {}).keys())
        responses = [
            f"{code}:{list(((resp or {}).get('schema') or {}).get('properties', {}).keys())}"
            for code, resp in (endpoint.get("responses") or {}).items()
            if isinstance(resp, dict)
        ]
        item: dict[str, Any] = {
            "id": endpoint.get("id"),
            "method": endpoint.get("method"),
            "path": endpoint.get("path"),
            "summary": (endpoint.get("summary") or "")[:120],
            "params": params[:8],
            "body": body[:8],
            "responses": responses[:3],
            "sources": sorted({str(s.get("type")) for s in (endpoint.get("sources") or [])}),
            "source_refs": [
                {"type": str(source.get("type")), "ref": str(source.get("ref"))}
                for source in (endpoint.get("sources") or [])
                if source.get("ref")
            ][:6],
        }
        if include_schema:
            item["request_schema"] = _schema_brief(
                (endpoint.get("request_body") or {}).get("schema") or {}
            )
            item["param_schema"] = {
                group: [
                    {
                        "name": param.get("name"),
                        "type": _schema_brief(param.get("schema") or {}).get("type"),
                        "required": bool(param.get("required")),
                        "enum": (param.get("schema") or {}).get("enum"),
                    }
                    for param in items or []
                    if isinstance(param, dict) and param.get("name")
                ][:20]
                for group, items in (endpoint.get("params") or {}).items()
            }
        compact.append(item)
    return compact


def _doc_chunks(session: Session, project: Project, limit: int = 60) -> list[dict[str, Any]]:
    """文档切片（含角色标记：spec=接口规范，notes=设计笔记 / 开发日志）。"""
    sources = session.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project.id,
            KnowledgeSource.kind == KnowledgeKind.MARKDOWN.value,
        )
    ).all()
    if not sources:
        return []
    chunks = session.scalars(
        select(Chunk).where(Chunk.project_id == project.id, Chunk.source_id.in_([s.id for s in sources])).limit(limit)
    ).all()
    roles = {source.id: (source.role or "spec") for source in sources}
    return [
        {
            "id": c.id,
            "content": c.content,
            "locator": c.locator,
            "source_id": c.source_id,
            "role": (c.meta_json or {}).get("role") or roles.get(c.source_id, "spec"),
            "source": (c.locator or {}).get("file_path", ""),
        }
        for c in chunks
    ]


def _code_chunks(session: Session, project: Project, limit: int = 400) -> list[dict[str, Any]]:
    sources = session.scalars(
        select(KnowledgeSource.id).where(KnowledgeSource.project_id == project.id, KnowledgeSource.kind == "code")
    ).all()
    if not sources:
        return []
    chunks = session.scalars(
        select(Chunk).where(Chunk.project_id == project.id, Chunk.source_id.in_(list(sources))).limit(limit)
    ).all()
    return [{"id": c.id, "content": c.content, "locator": c.locator} for c in chunks]


# ── 从文档抽接口（Agent 读文档）────────────────────────────
def extract_doc_endpoints(
    session: Session,
    project: Project,
    provider: LLMProvider,
    *,
    source_ids: list[int] | None = None,
    max_chunks: int = 60,
    shrink: int = 1,
) -> list[Endpoint]:
    """让 Agent 读文档（非标准格式也认），把接口登记成来源为 doc 的接口。"""
    from app.agent.docextract import extract_endpoints_with_agent
    from app.ingest.service import upsert_endpoint

    stmt = select(Chunk).where(Chunk.project_id == project.id)
    source_ids = source_ids or _doc_source_ids(session, project.id)
    if not source_ids:
        return []
    chunks = session.scalars(stmt.where(Chunk.source_id.in_(source_ids)).limit(max_chunks * 3)).all()
    if not chunks:
        return []
    payload = [
        {"id": chunk.id, "content": chunk.content, "locator": chunk.locator}
        for chunk in chunks
    ]
    extracted = extract_endpoints_with_agent(
        provider,
        payload,
        max_chunks=max_chunks,
        shrink=shrink,
    )

    endpoints: list[Endpoint] = []
    for item in extracted:
        evidence = item.get("evidence") or {}
        endpoint = upsert_endpoint(
            session,
            project.id,
            item["method"],
            item["path"],
            EndpointSource.DOC.value,
            summary=item.get("summary") or "",
            ref=str(evidence.get("ref") or ""),
            evidence={"quote": str(evidence.get("quote") or "")[:400]} if evidence.get("quote") else None,
        )
        endpoints.append(endpoint)
    session.flush()

    record_trace(
        session,
        project_id=project.id,
        kind="plan",
        step="extract_doc_endpoints",
        payload={
            "input": {"chunks": len(payload), "sources": len(source_ids)},
            "output": {"endpoints": len(endpoints)},
        },
    )
    return endpoints


def _doc_source_ids(session: Session, project_id: int) -> list[int]:
    return list(
        session.scalars(
            select(KnowledgeSource.id).where(
                KnowledgeSource.project_id == project_id,
                KnowledgeSource.kind.in_([KnowledgeKind.MARKDOWN.value, "webdoc"]),
            )
        ).all()
    )


# ── 功能点抽取 ──────────────────────────────────────────────
def _group_by_module(endpoints: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按模块分组：/api/users → users，/api/env/vars → env。

    （第一段是 api / v1 这类前缀时，用第二段做模块名，否则整组太大。）
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        path = str(endpoint.get("path") or "/")
        parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
        if not parts:
            module = "root"
        elif parts[0].lower() in {"api", "apis", "v1", "v2", "rest"} and len(parts) > 1:
            module = parts[1]
        else:
            module = parts[0]
        groups.setdefault(module, []).append(endpoint)
    return groups


def _docs_for_module(docs: list[dict[str, Any]], module: str) -> list[dict[str, Any]]:
    """挑出与该模块相关的文档切片（含角色标记）。"""
    if not module or module == "root":
        return docs
    keyword = module.lower()
    picked = [d for d in docs if keyword in str((d.get("locator") or {}).get("file_path", "")).lower()]
    if not picked:
        picked = [d for d in docs if keyword in (d.get("content") or "").lower()]
    return picked or docs[:8]


def module_of(item: dict[str, Any], groups: dict[str, list[dict[str, Any]]]) -> str:
    """功能点没写模块时，按它映射的接口反推模块。"""
    if item.get("module"):
        return str(item["module"])
    endpoint_ids = {e.get("id") for e in item.get("endpoint_ids") or [] if isinstance(e.get("id"), int)}
    for module, endpoints in groups.items():
        if endpoint_ids & {e.get("id") for e in endpoints}:
            return module
    return ""


# ── 基于 LangGraph 的工具型 Agent ────────────────────────────
def analyze_with_tools(
    session: Session,
    project: Project,
    provider: LLMProvider,
    task: str,
    *,
    system: str = "你是严谨的软件测试分析助手。",
    max_turns: int = 8,
    tracer: Callable[[dict[str, Any]], None] | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """跑一轮 LangGraph 工具调用循环，让 Agent 自己去查资料再给结论。

    典型用法：白盒走查时，Agent 用 ``list_code_files`` / ``grep_code`` /
    ``read_code_file`` / ``search_knowledge`` 自己翻代码和文档。
    """
    from app.agent.loop import LangGraphAgentLoop

    ctx = build_tool_context(session, project)
    loop = LangGraphAgentLoop(
        provider,
        tools=ALL_TOOLS,
        tracer=tracer,
        reasoning_effort=reasoning_effort,
    )
    result = loop.run(task, ctx, system=system, max_turns=max_turns)
    return result.final


def _trace_tracer(session: Session, project_id: int, kind: str, step: str) -> Callable[[dict[str, Any]], None]:
    """生成一个 tracer：把每一轮的工具调用写进 agent_traces。"""

    def _tracer(payload: dict[str, Any]) -> None:
        record_trace(
            session,
            project_id=project_id,
            kind=kind,
            step=step,
            payload={
                "input": payload.get("input", {}),
                "output": payload.get("output", {}),
                "tool_calls": payload.get("tool_calls", []),
                "usage": payload.get("usage", {}),
                "error": payload.get("error"),
                "duration_ms": payload.get("duration_ms", 0),
            },
        )

    return _tracer


def extract_features(
    session: Session,
    project: Project,
    provider: LLMProvider,
    *,
    replace: bool = True,
    allow_orphan_cases: bool = False,
) -> list[Feature]:
    # 重新抽取会 delete 旧 Feature；test_cases.feature_id 是 ON DELETE SET NULL，
    # 已有用例会全部变成「未映射功能点」，coverage 直接归零。
    # 默认拒绝这种静默数据损坏；确需清空时必须显式允许。
    if replace and not allow_orphan_cases:
        case_count = session.scalar(
            select(func.count()).select_from(TestCase).where(TestCase.project_id == project.id)
        ) or 0
        if case_count:
            raise ValueError(
                f"项目已有 {case_count} 条用例，重新抽取功能点会清空用例与功能点的关联。"
                "请先删除/迁移用例，或显式传 allow_orphan_cases=True。"
            )

    # 先让 Agent 读文档补齐接口（文档式接口规范 / 非标准文档），再做功能点抽取
    try:
        extract_doc_endpoints(session, project, provider)
        session.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("文档接口抽取失败，跳过：%s", exc)
    endpoints = _endpoints_payload(session, project)
    docs = _doc_chunks(session, project)

    # 按模块分批抽取：一次输出太长会被模型截断（推理模型尤其明显），分批更稳
    groups = _group_by_module(endpoints)
    if not groups:
        groups = {"root": endpoints}

    system = (
        "你是软件测试分析师。基于接口清单与文档抽取功能点；"
        "每个功能点必须带来源引用；文档提到但没有对应接口的功能点要保留（unmapped）。"
        "要求：每个接口最多一个功能点；source_refs 的 quote 最多 80 字；只输出 JSON。"
    )

    collected: list[dict[str, Any]] = []
    started = time.time()

    def _extract_one(module: str, module_endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
        context = {
            "task": "feature_extraction",
            "module": module,
            "endpoints": _compact_endpoints(module_endpoints),
            "doc_chunks": _docs_for_module(docs, module)[:12],
        }
        try:
            data = provider.structured(system, "请抽取该模块的功能点。", FEATURE_SCHEMA, context=context)
        except LLMError as exc:
            logger.warning("模块 %s 功能点抽取失败：%s", module, exc)
            return []
        return [item for item in (data.get("features") or []) if item.get("name")]

    # 模块之间互不依赖 → 并行调用（推理模型单次调用可能几十秒到几分钟）
    workers = max(1, min(len(groups), int(os.environ.get("SHAKEDOWN_LLM_CONCURRENCY", "4"))))
    if workers > 1 and len(groups) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_extract_one, module, items): module for module, items in groups.items()
            }
            for future in as_completed(futures):
                collected.extend(future.result())
    else:
        for module, items in groups.items():
            collected.extend(_extract_one(module, items))

    if replace:
        session.execute(delete(Feature).where(Feature.project_id == project.id))
        session.flush()

    features: list[Feature] = []
    for item in collected:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        endpoint_ids = [int(e) for e in (item.get("endpoint_ids") or []) if isinstance(e, int)]
        feature = Feature(
            project_id=project.id,
            name=name[:300],
            module=(item.get("module") or module_of(item, groups))[:200],
            description=item.get("description") or "",
            source_refs=item.get("source_refs") or [],
            endpoint_ids=endpoint_ids,
            unmapped=not endpoint_ids,
        )
        session.add(feature)
        features.append(feature)
    session.flush()

    record_trace(
        session,
        project_id=project.id,
        kind="plan",
        step="extract_features",
        payload={
            "input": {"endpoints": len(endpoints), "doc_chunks": len(docs)},
            "output": {"features": [{"id": f.id, "name": f.name, "unmapped": f.unmapped} for f in features]},
            "duration_ms": int((time.time() - started) * 1000),
        },
    )
    return features


# ── 用例生成 ────────────────────────────────────────────────
def _case_job_key(dimension: str, batch: list[Feature]) -> str:
    return f"{dimension}|" + ",".join(str(item.id) for item in batch)


def generate_cases(
    session: Session,
    project: Project,
    provider: LLMProvider,
    *,
    feature: Feature | None = None,
    features: Iterable[Feature] | None = None,
    dimensions: Iterable[str] | None = None,
    max_retries: int = 2,
    batch_size: int = 4,
    completed_keys: set[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    job_timeout: float = 360.0,
    concurrency: int | None = None,
    trace_task_id: int | None = None,
    only_missing_dimensions: bool = False,
) -> list[TestCase]:
    """按维度生成用例。

    真实模型很贵也慢，所以：
    - **按批生成**：一次调用覆盖多个功能点（``batch_size`` 个），而不是一个功能点一次；
    - **并行执行**：批次之间互不依赖，用线程池并发调模型；
    - **顺序落库**：模型调用在子线程里（纯网络），写库回到主线程（SQLAlchemy Session 非线程安全）。
    """
    if feature:
        target_features = [feature]
    elif features is not None:
        target_features = list(features)
    else:
        target_features = list(
            session.scalars(select(Feature).where(Feature.project_id == project.id)).all()
        )
    if not target_features:
        return []
    features = target_features
    dimensions = list(dimensions or [d.value for d in Dimension])
    endpoints = _endpoints_payload(session, project)
    docs = _doc_chunks(session, project)
    code_index = _load_code_index(session, project)
    accounts = sorted((decrypt_json(project.test_accounts_encrypted) or {}).keys())
    sla = _extract_sla(docs) or _project_sla(project)

    existing_dimensions: set[tuple[int, str]] = set()
    if only_missing_dimensions:
        rows = session.execute(
            select(TestCase.feature_id, TestCase.dimension).where(
                TestCase.project_id == project.id,
                TestCase.feature_id.in_([feature.id for feature in features]),
                # 空壳 / 缺 SLA 的占位用例不应当阻止该维度重新生成
                TestCase.missing_input.is_(None),
            )
        ).all()
        existing_dimensions = {
            (feature_id, dimension)
            for feature_id, dimension in rows
            if feature_id is not None
        }

    # 1) 组批（按模块聚拢 related 的功能点，输出更连贯）
    # 2) 并行调模型（不碰数据库）
    system_template = _CASE_SYSTEM_PROMPT
    jobs: list[tuple[str, list[Feature], dict[str, Any]]] = []
    for dimension in dimensions:
        dimension_features = (
            [
                feature
                for feature in features
                if (feature.id, dimension) not in existing_dimensions
            ]
            if only_missing_dimensions
            else features
        )
        if not dimension_features:
            continue
        for batch in _batch_features(dimension_features, batch_size):
            id_based = [
                e for e in endpoints if e["id"] in {i for f in batch for i in (f.endpoint_ids or [])}
            ]
            batch_endpoints = _endpoints_for_features(batch, endpoints) or id_based
            context = {
                "task": "case_generation",
                "dimension": dimension,
                "feature": {
                    "id": batch[0].id,
                    "name": batch[0].name,
                    "module": batch[0].module,
                    "description": batch[0].description,
                    "source_refs": batch[0].source_refs,
                    "endpoint_ids": batch[0].endpoint_ids,
                },
                "features": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "module": f.module,
                        "description": f.description,
                        "source_refs": f.source_refs,
                        "endpoint_ids": f.endpoint_ids,
                    }
                    for f in batch
                ],
                # 端点用**精简版**（完整 schema 会把上下文撑到十几万字符，输出直接被截断）
                "endpoints": _compact_endpoints(batch_endpoints, include_schema=True),
                "all_endpoints": _compact_endpoints(endpoints),
                "code_excerpts": _code_excerpts(code_index, batch_endpoints),
                "accounts": accounts,
                "profile": (project.settings_json or {}).get("profile") or {},
                "doc_chunks": _docs_for_features(docs, batch)[:6],
                "sla": sla,
                "performance_samples": _project_performance_samples(project),
                "project": {"base_url": project.base_url, "settings": project.settings_json},
            }
            jobs.append((dimension, batch, context))

    completed = set(completed_keys or set())
    all_keys = {_case_job_key(dimension, batch) for dimension, batch, _ in jobs}
    done_keys = {key for key in completed if key in all_keys}
    pending_jobs = [job for job in jobs if _case_job_key(job[0], job[1]) not in done_keys]
    total = len(jobs)
    failed_keys: set[str] = set()
    if on_progress:
        on_progress(
            {
                "total": total,
                "done": len(done_keys),
                "failed": 0,
                "cases": 0,
                "module": "",
                "keys": sorted(all_keys),
                "finished": not pending_jobs,
            }
        )
    if not pending_jobs:
        return []

    def on_llm_call(
        dimension: str,
        batch: list[Feature],
        phase: str,
        payload: dict[str, Any],
        duration_ms: int,
    ) -> None:
        """每次模型调用完成就写 trace，失败/repair 之前也能看到输出。"""
        if not trace_task_id:
            return
        try:
            from app.db import SessionLocal

            with SessionLocal() as trace_session:
                record_trace(
                    trace_session,
                    project_id=project.id,
                    task_id=trace_task_id,
                    kind="generate",
                    step=f"llm_call:{dimension}:{phase}",
                    payload={
                        "input": {
                            "dimension": dimension,
                            "phase": phase,
                            "features": [
                                {"id": feature.id, "name": feature.name, "module": feature.module}
                                for feature in batch
                            ],
                        },
                        "output": {
                            "cases": payload.get("cases") or [],
                            "case_count": len(payload.get("cases") or []),
                            "error": payload.get("__error__"),
                        },
                        "error": payload.get("__error__"),
                        "duration_ms": duration_ms,
                    },
                )
                trace_session.commit()
        except Exception as exc:  # noqa: BLE001 - trace 失败不能拖死生成
            logger.warning("生成调用 trace 写入失败：%s", exc)

    # 2) 并行调模型（不碰数据库）—— 边完成边落库，长任务超时也不会全丢
    created: list[TestCase] = []
    for (dimension, batch, _context), payload in _iter_llm_jobs(
        provider,
        pending_jobs,
        system_template,
        CASE_SCHEMA,
        max_retries,
        job_timeout=job_timeout,
        should_cancel=should_cancel,
        concurrency=concurrency,
        on_call=on_llm_call,
    ):
        key = _case_job_key(dimension, batch)
        if payload.get("__cancelled__"):
            if on_progress:
                on_progress(
                    {
                        "total": total,
                        "done": len(done_keys),
                        "failed": len(failed_keys),
                        "cases": len(created),
                        "module": batch[0].module if batch else "",
                        "cancelled": True,
                    }
                )
            break
        raw_cases = list(payload.get("cases") or [])
        repair_rounds = int(payload.get("__repair_rounds__") or 0)
        # 失败/超时批次不落库，保证任务重试时不会留下半批或重复用例
        error = payload.get("__error__")
        record_trace(
            session,
            project_id=project.id,
            task_id=trace_task_id,
            kind="generate",
            step=f"generate_cases:{dimension}",
            payload={
                "input": {"features": [f.name for f in batch], "dimension": dimension},
                "output": {
                    "cases": len(raw_cases),
                    "repair_rounds": repair_rounds,
                    "key": key,
                    "initial_ms": int(payload.get("__initial_duration_ms__") or 0),
                    "repair_ms": int(payload.get("__repair_duration_ms__") or 0),
                    "llm_calls": int(payload.get("__llm_calls__") or 0),
                },
                "error": error,
                "duration_ms": int(payload.get("__duration_ms__") or 0),
            },
        )
        if not error:
            for raw in raw_cases:
                # 批次里可能有多个功能点 → 按 feature 名匹配归属
                owner = _match_feature(batch, raw)
                case = _persist_case(session, project, owner, raw, dimension)
                if case:
                    created.append(case)
        # 每批提交一次：长任务被中断时已完成的部分不会丢
        session.commit()
        if error:
            failed_keys.add(key)
        else:
            done_keys.add(key)
        if on_progress:
            on_progress(
                {
                    "total": total,
                    "done": len(done_keys),
                    "failed": len(failed_keys),
                    "cases": len(created),
                    "module": batch[0].module if batch else "",
                    "key": key,
                    "ok": not bool(error),
                    "finished": len(done_keys) + len(failed_keys) >= total,
                }
            )
        if should_cancel and should_cancel():
            break
    session.flush()
    return created


def enrich_suggested_case(
    session: Session,
    project: Project,
    provider: LLMProvider,
    case: TestCase,
    *,
    failure: CaseResult | None = None,
    cli_mode: bool = False,
) -> TestCase:
    """把白盒建议用例补全为可执行用例。

    只补全 steps / assertions / evidence，不改变 pending 状态，也不自动启用。
    模型无法确定接口或输入时允许返回 missing_input，禁止编造。
    """
    finding = session.scalar(
        select(Finding)
        .where(Finding.case_id == case.id)
        .order_by(Finding.id)
        .limit(1)
    )
    endpoints = _endpoints_payload(session, project)
    docs = _doc_chunks(session, project)
    code_index = _load_code_index(session, project)
    accounts = sorted((decrypt_json(project.test_accounts_encrypted) or {}).keys())
    case_endpoints = _endpoints_for_case(case, endpoints)
    symbol_excerpts = _symbol_code_excerpts(code_index, case, endpoints, case_endpoints)
    disk_excerpts = (
        []
        if cli_mode
        else _disk_handler_excerpts(session, project, case, endpoints, case_endpoints)
    )
    if disk_excerpts:
        symbol_excerpts = disk_excerpts + symbol_excerpts
    settings = project.settings_json or {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    adapter = settings.get("exec_adapter") if isinstance(settings.get("exec_adapter"), dict) else {}
    runner: list[str] = []
    if adapter.get("type") == "lima":
        runner = ["limactl", "shell", str(adapter.get("instance") or "debian12"), "--"]
    cli_context = {
        "runner": runner,
        "binary": str((profile.get("environment") or {}).get("cli_binary") or settings.get("cli_binary") or ""),
        "setup_script": str(settings.get("cli_setup_script") or ""),
        "config_path": str(settings.get("cli_config_path") or ""),
        "data_dir": str(settings.get("cli_data_dir") or ""),
        "examples": _cli_examples(session, project),
        "notes": str(profile.get("notes") or "")[:800],
    }
    if cli_mode:
        code_excerpts = _cli_code_excerpts(code_index)
    elif case_endpoints:
        code_excerpts = disk_excerpts + _code_excerpts(code_index, case_endpoints)
    else:
        code_excerpts = symbol_excerpts or _keyword_code_excerpts(code_index, _case_keywords(case))
    location = (finding.code_location if finding else {}) or {}
    context = {
        "task": "enrich_suggested_case",
        "case": {
            "id": case.id,
            "title": case.title,
            "dimension": case.dimension,
            "type": case.case_type,
            "existing_spec": case.spec,
            "missing_input": case.missing_input,
        },
        "finding": {
            "id": finding.id if finding else None,
            "title": finding.title if finding else "",
            "description": finding.description if finding else "",
            "risk_type": finding.risk_type if finding else "",
            "code_location": location,
            "evidence": finding.evidence if finding else [],
        },
        "endpoints": _compact_endpoints(case_endpoints or endpoints, include_schema=True),
        "all_endpoints": _compact_endpoints(endpoints),
        "code_excerpts": code_excerpts,
        "accounts": accounts,
        "profile": profile,
        "cli": cli_context,
        "failure": (
            {
                "run_id": failure.run_id,
                "status": failure.status,
                "logs": (failure.logs or "")[-3000:],
                "request": failure.request or {},
                "response": failure.response or {},
                "attribution": failure.attribution or {},
            }
            if failure is not None
            else None
        ),
        "doc_chunks": _docs_for_features(docs, [])[:8],
        "project": {"base_url": project.base_url, "settings": project.settings_json},
    }
    system = (
        "你是资深测试设计工程师。把一条白盒建议用例补全成可执行 API 测试用例。"
        "必须基于接口清单、代码证据和文档确定接口、输入与预期结果。"
        "context.code_excerpts 是端点对应的 handler 代码：只要里面有 writeJSON / WriteHeader / status / redirect / return，"
        "就必须按代码写出 status_code（必要时加响应字段）断言，并用 evidence={\"source_type\":\"code\",\"ref\":\"file:line\",\"quote\":\"...\"} 引用；"
        "不能因为 endpoints[].responses 为空就放弃断言。需要鉴权时用 auth={\"account\":\"<context.accounts 里的名字>\",\"mode\":\"bearer|basic|private_token\"}，"
        "不要写死 token 或 {{admin_token}}；没有可用账号才写 missing_input。代码里也看不出来的，才在 missing_input 说明缺什么，禁止编造。"
        "context.failure 是这条用例上次执行的真实请求 / 响应 / 日志：必须据此修正步骤和断言"
        "（错误体是 {\"message\":...} 就不要断言 $.detail；登录返回 HTML 就不要断言 JSON；缺会话就补 login 步骤），"
        "不要保留与失败日志矛盾的旧断言。"
        "输出严格 JSON：{\"cases\": [{title, dimension, type, steps, assertions, evidence, missing_input}]}。"
    )
    if cli_mode:
        system = (
            "你是 CLI 测试设计器。只输出一条可执行 case。"
            "steps 必须是 action=command：command 是数组，形如 cli.runner + [\"bash\",\"-lc\", 脚本]；"
            "脚本里用 cli.binary、cli.setup_script、cli.config_path、cli.data_dir，命令必须是真实存在、可执行的。"
            "不要连接、停止或重启线上服务；需要数据时使用 cli.setup_script 生成的临时副本。"
            "断言用 exit_code / stdout_contains / stderr_contains，至少一条 confirmed，evidence.source_type=code。"
            "参考 cli.examples 里已经跑通的命令。禁止编造二进制路径或 flag；"
            "确实无法构造时 steps 留空，并在 missing_input 说明缺什么。"
            "输出严格 JSON：{\"cases\": [{title, dimension, type, steps, assertions, evidence, missing_input}]}。"
        )
    payload = _structured_with_retry(
        provider,
        system,
        "请补全这条建议用例，输出单个 case。",
        CASE_SCHEMA,
        context,
        1,
        reasoning_effort="medium",
    )
    if payload.get("__error__"):
        raise RuntimeError(payload["__error__"])
    raw_cases = list(payload.get("cases") or [])
    if not raw_cases:
        raise RuntimeError("模型没有返回补全结果")
    spec = dict(raw_cases[0] or {})
    spec["title"] = case.title
    spec["dimension"] = case.dimension
    spec["type"] = case.case_type or "api"
    spec.setdefault("schema_version", "1.0")
    spec.setdefault("steps", [])
    spec.setdefault("assertions", [])
    spec.setdefault("evidence", case.evidence or [])
    spec.setdefault("preconditions", [])
    spec.setdefault("cleanup", [])
    spec["assertions"] = [
        _normalize_assertion(item) for item in spec["assertions"] if isinstance(item, dict)
    ]
    for step in spec["steps"]:
        if isinstance(step, dict) and step.get("assert"):
            step["assert"] = [
                _normalize_assertion(item)
                for item in step["assert"]
                if isinstance(item, dict)
            ]
    moved = split_unbacked_assertions(spec)
    if moved:
        record_trace(
            session,
            project_id=project.id,
            kind="enrich",
            step=f"case:{case.id}",
            payload={
                "input": {"case_id": case.id, "finding_id": finding.id if finding else None},
                "output": {"questions": len(spec.get("contract_questions") or [])},
                "error": f"{len(moved)} 条无依据断言已转为待确认问题",
            },
        )
    def _normalize_actions(items: Any) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for step in items or []:
            if not isinstance(step, dict):
                continue
            if not step.get("action"):
                if step.get("sql"):
                    step["action"] = "db"
                elif step.get("selector"):
                    step["action"] = "browser"
                elif step.get("method") or step.get("path"):
                    step["action"] = "request"
                else:
                    continue
            cleaned.append(step)
        return cleaned

    normalized_preconditions: list[dict[str, Any]] = []
    for item in spec.get("preconditions") or []:
        if isinstance(item, str):
            normalized_preconditions.append({"kind": "manual", "note": item})
        elif isinstance(item, dict):
            normalized_preconditions.append({"kind": item.get("kind") or "manual", **item})
    spec["preconditions"] = normalized_preconditions
    from app.case_normalizer import normalize_case_auth

    normalize_case_auth(spec, accounts)
    spec["steps"] = _normalize_actions(spec.get("steps"))
    spec["cleanup"] = _normalize_actions(spec.get("cleanup"))
    errors = validate_case_spec(spec)
    if errors and spec.get("cleanup"):
        # cleanup 写坏不应该丢整条用例的步骤 / 断言
        spec["cleanup"] = []
        errors = validate_case_spec(spec)
    if errors:
        case.missing_input = "AI 补全结果未通过 schema 校验：" + "；".join(errors[:3])
        record_trace(
            session,
            project_id=project.id,
            kind="enrich",
            step=f"case:{case.id}",
            payload={"input": {"case_id": case.id}, "error": case.missing_input},
        )
        return case
    case.steps = spec.get("steps") or []
    case.expected = {"assertions": collect_assertions(spec)}
    case.evidence = spec.get("evidence") or case.evidence or []
    case.preconditions = {"items": spec.get("preconditions") or []}
    case.spec = spec
    case.missing_input = spec.get("missing_input") or (
        None if case.steps else "AI 无法确定这条建议用例的接口/输入，请人工补全"
    )
    record_trace(
        session,
        project_id=project.id,
        kind="enrich",
        step=f"case:{case.id}",
        payload={
            "input": {"case_id": case.id, "finding_id": finding.id if finding else None},
            "output": {"steps": len(case.steps), "assertions": len(case.expected.get("assertions") or [])},
            "error": None,
        },
    )
    session.flush()
    return case


def _batch_features(features: list[Feature], batch_size: int) -> list[list[Feature]]:
    """把功能点按模块聚成批次。"""
    by_module: dict[str, list[Feature]] = {}
    for item in features:
        by_module.setdefault(item.module or "未分组", []).append(item)
    batches: list[list[Feature]] = []
    for module_items in by_module.values():
        for index in range(0, len(module_items), max(1, batch_size)):
            batches.append(module_items[index : index + max(1, batch_size)])
    return batches


def _load_code_index(session: Any, project: Project) -> list[tuple[str, int, int, str]]:
    """把项目里的代码切片整理成 (path, start_line, end_line, content) 索引。"""
    rows = session.scalars(select(Chunk).where(Chunk.project_id == project.id)).all()
    index: list[tuple[str, int, int, str]] = []
    for row in rows:
        locator = row.locator or {}
        path = str(locator.get("file_path") or "")
        if not path or not row.content:
            continue
        try:
            start = int(locator.get("start_line") or 0)
            end = int(locator.get("end_line") or start)
        except (TypeError, ValueError):
            continue
        if start:
            index.append((path, start, end, row.content))
    return index


_CODE_REF_PATTERN = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?:-\d+)?$")
_HANDLER_ARG_PATTERN = re.compile(r",\s*(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*)\s*\)")
_METHOD_PATH_MENTION = re.compile(
    r"\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?P<path>/[A-Za-z0-9_\-./{}:]{2,})"
)
_ANY_PATH_MENTION = re.compile(r"(?<![\w:])(?P<path>/[A-Za-z0-9_\-./{}:]{2,})")


def _route_key(method: str, path: str) -> tuple[str, str]:
    from app.ingest.consistency import normalize_path

    # 用例里的 {{var}} 模板与端点里的 {name} 占位符统一成 {}
    raw = re.sub(r"\{\{.*?\}\}", "{}", str(path or ""))
    normalized = re.sub(r"\{[^}]+\}", "{}", normalize_path(raw))
    return str(method or "").upper(), normalized.rstrip("/") or "/"


def _endpoints_for_features(
    features: list[Feature], endpoints: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按功能点描述 / 来源引用里提到的路由，挑出真正相关的端点。"""
    method_keys: set[tuple[str, str]] = set()
    path_keys: set[str] = set()
    for feature in features:
        text = " ".join(
            [
                feature.name or "",
                feature.description or "",
                " ".join(
                    f"{str(item.get('quote') or '')} {str(item.get('ref') or '')}"
                    for item in (feature.source_refs or [])
                ),
            ]
        )
        for match in _METHOD_PATH_MENTION.finditer(text):
            method_keys.add(_route_key(match.group("method"), match.group("path")))
        for match in _ANY_PATH_MENTION.finditer(text):
            path_keys.add(_route_key("GET", match.group("path"))[1])
    if not method_keys and not path_keys:
        return []
    matched: list[dict[str, Any]] = []
    for endpoint in endpoints:
        key = _route_key(endpoint.get("method", ""), endpoint.get("path", ""))
        if key in method_keys or key[1] in path_keys:
            matched.append(endpoint)
    return matched


def _endpoints_for_case(case: TestCase, endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按用例步骤里的 method + path 找到它真正打到的端点。"""
    keys = {
        _route_key(step.get("method", ""), step.get("path", ""))
        for step in (case.steps or [])
        if isinstance(step, dict) and step.get("method") and step.get("path")
    }
    if not keys:
        return []
    return [
        endpoint
        for endpoint in endpoints
        if _route_key(endpoint.get("method", ""), endpoint.get("path", "")) in keys
    ]


def _handler_names(line_text: str) -> list[str]:
    """从路由注册行里猜 handler 名：mux.HandleFunc("POST /x", s.deleteX)。"""
    names: list[str] = []
    for match in _HANDLER_ARG_PATTERN.finditer(line_text or ""):
        name = match.group("name")
        if name and name not in {"nil", "true", "false"} and name not in names:
            names.append(name)
    return names


def _defines_handler(content: str, name: str) -> bool:
    escaped = re.escape(name)
    return bool(
        re.search(rf"\bfunc\s+(?:\([^)]*\)\s*)?{escaped}\s*\(", content)
        or re.search(rf"\bdef\s+{escaped}\s*\(", content)
        or re.search(rf"\b(?:public|private|protected)\s+[\w<>\[\], ]+\s+{escaped}\s*\(", content)
    )


def _code_excerpts(
    index: list[tuple[str, int, int, str]],
    endpoints: list[dict[str, Any]],
    *,
    max_chars: int = 12000,
    per_ref: int = 2,
) -> list[dict[str, Any]]:
    """给端点补 handler 代码片段，让生成器能引用 code:文件:行号 作为断言依据。

    - 只对 responses 为空的端点取代码：有 OpenAPI 契约时不需要用代码兜底；
    - 先取路由注册行附近的 chunk，再按行里的 handler 名找 handler 函数体，
      这样模型看到的是「状态码 / 重定向 / JSON 字段」而不是只有一行 mux.HandleFunc。
    """
    refs: list[tuple[str, int]] = []
    for endpoint in endpoints:
        if endpoint.get("responses"):
            continue
        for source in endpoint.get("sources") or []:
            if str(source.get("type")) != "code":
                continue
            match = _CODE_REF_PATTERN.match(str(source.get("ref") or ""))
            if match:
                refs.append((match.group("path"), int(match.group("line"))))

    excerpts: list[dict[str, Any]] = []
    used = 0
    seen: set[tuple[str, int]] = set()

    def add(path: str, start: int, end: int, content: str, ref: str) -> bool:
        nonlocal used
        key = (path, start)
        if key in seen:
            return True
        text = content[:3500]
        if used + len(text) > max_chars:
            return False
        seen.add(key)
        excerpts.append({"ref": ref, "file": path, "lines": f"{start}-{end}", "content": text})
        used += len(text)
        return True

    def add_with_next(path: str, start: int, end: int, content: str, ref: str) -> None:
        """handler 经常跨 chunk 边界：把同文件的下一个 chunk 一起带上。"""
        add(path, start, end, content, ref)
        following = sorted(
            [(item_start, item_end, item_content) for item_path, item_start, item_end, item_content in index if item_path == path and item_start > start],
            key=lambda item: item[0],
        )
        if following:
            next_start, next_end, next_content = following[0]
            add(path, next_start, next_end, next_content, f"{path}:{next_start}")

    # 有些 Go 路由是 helper 拼出来的（prefix + path），extract_routes 找不到 code ref；
    # 用路径关键字回退到最可能包含 handler 的代码块，保证「代码即契约」仍能生效。
    fallback_chunks: list[tuple[str, int, int, str, str]] = []
    for endpoint in endpoints:
        if endpoint.get("responses"):
            continue
        if any(str(source.get("type")) == "code" for source in endpoint.get("sources") or []):
            continue
        endpoint_path = str(endpoint.get("path") or "")
        segments = [
            segment
            for segment in re.split(r"[/{}\s]+", endpoint_path)
            if segment and segment.lower() not in {"api", "admin", "static", "v1", "v2", "rest"}
        ]
        keywords = [segment for segment in segments if len(segment) >= 3]
        short_keywords = [segment for segment in segments if 2 <= len(segment) < 3]
        prefix = endpoint_path.strip("/").split("/")[0].lower()
        scored: list[tuple[int, int, str, str]] = []
        for chunk_path, start, _end, content in index:
            if not re.search(r"\.(go|py|js|ts|java|rb|rs|php|cs)$", chunk_path):
                continue
            lowered = content.lower()
            if prefix == "api" and "api" not in chunk_path.lower():
                continue
            if prefix == "admin" and not any(
                token in chunk_path.lower() for token in ("admin", "handler", "server")
            ):
                continue
            if lowered.lstrip().startswith("package ") and "func " not in lowered:
                continue
            hits = sum(1 for keyword in keywords[-2:] if keyword.lower() in lowered)
            quoted = sum(
                1
                for keyword in short_keywords
                if f'"{keyword.lower()}"' in lowered or f"'{keyword.lower()}'" in lowered
            )
            if not hits and not quoted:
                continue
            score = hits * 8 + quoted * 10
            if "func " in lowered:
                score += 6
            if any(
                re.search(
                    rf"\bfunc\s+(?:\([^)]*\)\s*)?\w*{re.escape(keyword)}",
                    content,
                    re.I,
                )
                for keyword in keywords
            ):
                score += 30
            if "writejson" in lowered or "http.method" in lowered:
                score += 6
            if prefix and prefix in chunk_path.lower():
                score += 6
            scored.append((score, start, chunk_path, content))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for _score, start, chunk_path, content in scored[:1]:
            fallback_chunks.append((chunk_path, start, start, content, f"{chunk_path}:{start}"))

    for chunk_path, start, end, content, ref in fallback_chunks:
        add_with_next(chunk_path, start, end, content, ref)

    for path, line in refs:
        candidates: list[tuple[int, int, int, str, str]] = []
        for chunk_path, start, end, content in index:
            if not (
                chunk_path == path
                or chunk_path.endswith("/" + path)
                or path.endswith("/" + chunk_path)
            ):
                continue
            distance = 0 if start <= line <= end else min(abs(start - line), abs(end - line))
            candidates.append((distance, start, end, chunk_path, content))
        candidates.sort(key=lambda item: (item[0], item[1]))
        for position, (_distance, start, end, chunk_path, content) in enumerate(candidates[:per_ref]):
            if not add(chunk_path, start, end, content, f"{path}:{line}"):
                break
            if position > 0:
                continue
            # 路由注册行 → handler 函数体
            offset = line - start
            lines = content.splitlines()
            if not (0 <= offset < len(lines)):
                continue
            line_text = lines[offset]
            for name in _handler_names(line_text)[:2]:
                for handler_path, handler_start, handler_end, handler_content in index:
                    if not _defines_handler(handler_content, name):
                        continue
                    add_with_next(
                        handler_path,
                        handler_start,
                        handler_end,
                        handler_content,
                        f"{handler_path}:{handler_start}",
                    )
                    break
    return excerpts


def _cli_examples(session: Any, project: Project, *, limit: int = 3) -> list[dict[str, Any]]:
    """复用项目里已经跑通的 command 用例，给 LLM 当 few-shot。"""
    rows = session.scalars(
        select(TestCase)
        .where(TestCase.project_id == project.id, TestCase.status == "enabled")
        .order_by(TestCase.updated_at.desc())
        .limit(80)
    ).all()
    examples: list[dict[str, Any]] = []
    for case in rows:
        steps = [step for step in (case.steps or []) if isinstance(step, dict) and step.get("action") == "command"]
        if not steps:
            continue
        examples.append(
            {
                "title": case.title,
                "command": steps[0].get("command"),
                "assertions": (case.expected or {}).get("assertions", [])[:3],
            }
        )
        if len(examples) >= limit:
            break
    return examples


def _cli_code_excerpts(index: list[tuple[str, int, int, str]], *, max_chars: int = 9000) -> list[dict[str, Any]]:
    """CLI 模式没有 endpoint 可挂，按 main/flags 关键字找代码片段。"""
    keywords = ("-backup", "-restore", "reset-password", "flag.", "flag.Parse", "main(")
    scored: list[tuple[int, int, str, str, str]] = []
    for path, start, end, content in index:
        if not re.search(r"\.(go|py|js|ts|java|rb|rs|php|cs)$", path):
            continue
        hits = sum(1 for keyword in keywords if keyword in content)
        if not hits:
            continue
        scored.append((hits, start, path, end, content))
    scored.sort(key=lambda item: (-item[0], item[1]))
    excerpts: list[dict[str, Any]] = []
    used = 0
    for _hits, start, path, end, content in scored[:4]:
        text = content[:3200]
        if used + len(text) > max_chars:
            break
        excerpts.append({"ref": f"{path}:{start}", "file": path, "lines": f"{start}-{end}", "content": text})
        used += len(text)
    return excerpts


def _symbol_code_excerpts(
    index: list[tuple[str, int, int, str]],
    case: TestCase,
    endpoints: list[dict[str, Any]],
    matched_endpoints: list[dict[str, Any]] | None = None,
    *,
    max_chars: int = 10000,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按路由 → handler 符号精确取函数体，解决“关键词只捞到通用 handler”的问题。"""
    words = set(_case_keywords(case))
    candidates = list(matched_endpoints or [])
    if not candidates:
        candidates = [
            endpoint
            for endpoint in endpoints
            if any(word in str(endpoint.get("path") or "").lower() for word in words)
        ]
    handler_names: list[str] = []
    for endpoint in candidates[:12]:
        for source in endpoint.get("sources") or []:
            if str(source.get("type")) != "code":
                continue
            match = _CODE_REF_PATTERN.match(str(source.get("ref") or ""))
            if not match:
                continue
            path, line = match.group("path"), int(match.group("line"))
            for chunk_path, start, end, content in index:
                if not (
                    chunk_path == path
                    or chunk_path.endswith("/" + path)
                    or path.endswith("/" + chunk_path)
                ):
                    continue
                if not (start <= line <= end):
                    continue
                lines = content.splitlines()
                offset = line - start
                if 0 <= offset < len(lines):
                    handler_names.extend(_handler_names(lines[offset]))
                break
    excerpts: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    used = 0
    for name in dict.fromkeys(handler_names):
        for chunk_path, start, end, content in index:
            if not _defines_handler(content, name):
                continue
            key = (chunk_path, start)
            if key not in seen:
                seen.add(key)
                text = content[:3500]
                if used + len(text) <= max_chars:
                    excerpts.append(
                        {"ref": f"{chunk_path}:{start}", "file": chunk_path, "lines": f"{start}-{end}", "content": text}
                    )
                    used += len(text)
            break
        if len(excerpts) >= limit:
            break
    return excerpts


def _disk_handler_excerpts(
    session: Any,
    project: Project,
    case: TestCase,
    endpoints: list[dict[str, Any]],
    matched_endpoints: list[dict[str, Any]] | None = None,
    *,
    max_chars: int = 10000,
) -> list[dict[str, Any]]:
    """直接读本地代码源文件，按 func/def 名字取函数体；解决 chunk 把签名切碎的问题。"""
    from pathlib import Path

    words = set(_case_keywords(case))
    candidates = list(matched_endpoints or [])
    if not candidates:
        candidates = [
            endpoint
            for endpoint in endpoints
            if any(word in str(endpoint.get("path") or "").lower() for word in words)
        ]
    names: list[str] = []
    for endpoint in candidates[:12]:
        for source in endpoint.get("sources") or []:
            if str(source.get("type")) != "code":
                continue
            match = _CODE_REF_PATTERN.match(str(source.get("ref") or ""))
            if not match:
                continue
            path, line = match.group("path"), int(match.group("line"))
            # 先在 chunk 里解析注册行，拿 handler 名
            for chunk_path, start, end, content in _load_code_index(session, project):
                if not (
                    chunk_path == path
                    or chunk_path.endswith("/" + path)
                    or path.endswith("/" + chunk_path)
                ):
                    continue
                if start <= line <= end:
                    lines = content.splitlines()
                    offset = line - start
                    if 0 <= offset < len(lines):
                        names.extend(_handler_names(lines[offset]))
                    break
    names = list(dict.fromkeys(names))
    if not names:
        return []
    sources = session.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project.id,
            KnowledgeSource.kind == "code",
        )
    ).all()
    excerpts: list[dict[str, Any]] = []
    used = 0
    for source in sources:
        root = Path(str(source.location)).expanduser()
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".go", ".py", ".js", ".ts", ".java", ".rb", ".rs"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in names:
                pattern = re.compile(
                    rf"(?m)^\s*(?:func\s+(?:\([^)]*\)\s*)?|def\s+|public\s+[\w<>\[\], ]+\s+){re.escape(name)}\s*\("
                )
                match = pattern.search(text)
                if not match:
                    continue
                start_line = text[: match.start()].count("\n") + 1
                lines = text.splitlines()
                chunk = "\n".join(lines[max(0, start_line - 1): start_line + 70])
                rel = str(path.relative_to(root))
                if used + len(chunk) > max_chars:
                    return excerpts
                excerpts.append(
                    {
                        "ref": f"{rel}:{start_line}",
                        "file": rel,
                        "lines": f"{start_line}-{start_line + 70}",
                        "content": chunk[:3500],
                    }
                )
                used += len(chunk)
                break
            if len(excerpts) >= 4:
                return excerpts
    return excerpts


def _case_keywords(case: TestCase) -> list[str]:
    words = [word for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", case.title or "")]
    mapping = {
        "用户": ["user", "users", "admin"],
        "组": ["group", "groups", "member", "members"],
        "成员": ["member", "members", "group"],
        "管理员": ["admin", "users"],
        "口令": ["password", "reset"],
        "密码": ["password"],
        "禁用": ["toggle", "disable"],
        "启用": ["toggle", "enable"],
        "删除": ["delete", "purge"],
        "创建": ["create"],
        "备份": ["backup"],
        "恢复": ["restore"],
    }
    for chinese, extra in mapping.items():
        if chinese in (case.title or ""):
            words.extend(extra)
    return list(dict.fromkeys(word.lower() for word in words if len(word) >= 3))


def _keyword_code_excerpts(
    index: list[tuple[str, int, int, str]],
    keywords: list[str],
    *,
    max_chars: int = 10000,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """没有 endpoint 映射时，按标题/功能关键字找 handler 代码。"""
    words = [word.lower() for word in keywords if len(word) >= 3]
    if not words:
        return []
    scored: list[tuple[int, int, str, int, str]] = []
    for path, start, end, content in index:
        if not re.search(r"\.(go|py|js|ts|java|rb|rs|php|cs)$", path):
            continue
        lowered = content.lower()
        hits = sum(1 for word in words if word in lowered)
        if not hits:
            continue
        if "func " not in lowered and "def " not in lowered and "handlefunc" not in lowered:
            continue
        score = hits * 10
        if any(word in path.lower() for word in ("admin", "handler", "api", "server")):
            score += 5
        if "writejson" in lowered or "status" in lowered or "redirect" in lowered:
            score += 5
        scored.append((score, start, path, end, content))
    scored.sort(key=lambda item: (-item[0], item[1]))
    excerpts: list[dict[str, Any]] = []
    used = 0
    for _score, start, path, end, content in scored[:limit]:
        text = content[:3200]
        if used + len(text) > max_chars:
            break
        excerpts.append({"ref": f"{path}:{start}", "file": path, "lines": f"{start}-{end}", "content": text})
        used += len(text)
    return excerpts


def _docs_for_features(docs: list[dict[str, Any]], batch: list[Feature]) -> list[dict[str, Any]]:
    """挑出与这批功能点相关的文档切片（**按总字数限量**，避免上下文爆炸）。"""
    keywords = [f.name.lower() for f in batch if f.name]
    modules = [f.module.lower() for f in batch if f.module]
    picked = [
        d
        for d in docs
        if any(k and k in (d.get("content") or "").lower() for k in keywords[:3])
        or any(m and m in (d.get("content") or "").lower() for m in modules[:2])
    ]
    picked = picked or docs[:6]
    # 每个切片截断 + 总量限制
    out: list[dict[str, Any]] = []
    used = 0
    for item in picked:
        content = (item.get("content") or "")[:2500]
        if used + len(content) > 15000:
            break
        out.append({**item, "content": content})
        used += len(content)
    return out


def _match_feature(batch: list[Feature], raw: dict[str, Any]) -> Feature:
    """把模型产出的用例匹配回它所属的功能点（按标题里的功能点名字）。"""
    title = str(raw.get("title") or "")
    for item in batch:
        if item.name and item.name[:12] in title:
            return item
    # 没匹配上就用批里第一个（总比丢弃好）
    return batch[0]


def _case_user_prompt(batch: list[Feature]) -> str:
    names = "、".join(f.name for f in batch)
    return f"为功能点「{names}」生成测试用例（每个功能点都要有）。"


def _iter_llm_jobs(
    provider: LLMProvider,
    jobs: list[tuple[str, list[Feature], dict[str, Any]]],
    system_template: str,
    schema: dict[str, Any],
    max_retries: int,
    *,
    job_timeout: float = 360.0,
    should_cancel: Callable[[], bool] | None = None,
    concurrency: int | None = None,
    on_call: Callable[[str, list[Feature], str, dict[str, Any], int], None] | None = None,
):
    """并行跑 LLM 调用，**按完成顺序**产出结果（生成器）。

    每个批次有硬超时：上游模型挂住时返回 ``__error__``，不阻塞整个任务。
    超时/失败批次不会写进 checkpoint，任务重试时会重新生成。
    """
    if not jobs:
        return
    if should_cancel and should_cancel():
        for job in jobs:
            yield job, {"__error__": "任务已取消", "__cancelled__": True}
        return

    started_times: dict[tuple[str, tuple[int, ...]], float] = {}

    def _job_key(dimension: str, batch: list[Feature]) -> tuple[str, tuple[int, ...]]:
        return dimension, tuple(item.id for item in batch)

    def _one(dimension: str, batch: list[Feature], context: dict[str, Any]) -> dict[str, Any]:
        # 用墙钟而不是 monotonic：macOS 休眠时 monotonic 会暂停，
        # 否则挂住的模型调用在机器唤醒后还要再等满一个超时周期。
        started_wall = time.time()
        started_times[_job_key(dimension, batch)] = started_wall
        system = system_template.format(dimension=dimension, policy_catalog=_policy_catalog_prompt())
        user = _case_user_prompt(batch)
        started = time.time()
        initial_started = time.time()
        payload = _structured_with_retry(
            provider,
            system,
            user,
            schema,
            context,
            max_retries,
            reasoning_effort="medium",
        )
        initial_duration_ms = int((time.time() - initial_started) * 1000)
        if on_call:
            on_call(dimension, batch, "initial", payload, initial_duration_ms)
        raw_cases = [
            _normalize_generated_case(case)
            for case in (payload.get("cases") or [])
            if isinstance(case, dict)
        ]
        invalid = _invalid_cases(raw_cases)
        repair_rounds = 0
        repair_started = time.time()
        error = payload.get("__error__")
        # repair 也放在工作线程里：多个批次可以并行修复，
        # 不会被主线程串行落库拖成“假超时”。
        while invalid and not error and repair_rounds < max_retries:
            repair_rounds += 1
            repair_context = {
                **context,
                "last_error": "上一批用例不符合 schema：" + json.dumps(invalid[:5], ensure_ascii=False),
                "invalid_cases": invalid,
            }
            repair_call_started = time.time()
            repair = _structured_with_retry(
                provider,
                system,
                user,
                schema,
                repair_context,
                0,
                reasoning_effort="medium",
            )
            if on_call:
                on_call(
                    dimension,
                    batch,
                    f"repair-{repair_rounds}",
                    repair,
                    int((time.time() - repair_call_started) * 1000),
                )
            if repair.get("__error__"):
                error = repair["__error__"]
                break
            repaired = [
                _normalize_generated_case(case)
                for case in (repair.get("cases") or [])
                if isinstance(case, dict)
            ]
            if not repaired:
                break
            raw_cases = _merge_cases(raw_cases, repaired)
            invalid = _invalid_cases(raw_cases)
        payload["cases"] = raw_cases
        payload["__repair_rounds__"] = repair_rounds
        payload["__initial_duration_ms__"] = initial_duration_ms
        payload["__repair_duration_ms__"] = int((time.time() - repair_started) * 1000)
        payload["__llm_calls__"] = 1 + repair_rounds
        if error:
            payload["__error__"] = error
        payload.setdefault("__duration_ms__", int((time.time() - started) * 1000))
        payload.setdefault("__started_at__", started_wall)
        return payload

    configured = int(concurrency if concurrency is not None else os.environ.get("SHAKEDOWN_LLM_CONCURRENCY", "4"))
    workers = max(1, min(len(jobs), configured))

    # 按 wave 提交，而不是一次性把所有任务塞进同一个线程池。
    # 某些模型连接挂住时，线程无法被 Python 强杀；如果继续用同一个池，
    # 4 个泄漏线程会占满所有 worker，后面的批次永远排不到，任务表现为“无进展”。
    # 每批 wave 用独立池，超时后换新池继续，保证任务一定能往下走。
    for offset in range(0, len(jobs), workers):
        if should_cancel and should_cancel():
            for job in jobs[offset:]:
                yield job, {"__error__": "任务已取消", "__cancelled__": True}
            return
        wave = jobs[offset : offset + workers]
        pool = ThreadPoolExecutor(max_workers=max(1, len(wave)))
        futures = {
            pool.submit(_one, dim, batch, ctx): (dim, batch, ctx)
            for dim, batch, ctx in wave
        }
        pending = set(futures)
        try:
            while pending:
                if should_cancel and should_cancel():
                    for future in pending:
                        future.cancel()
                    for future in pending:
                        yield futures[future], {"__error__": "任务已取消", "__cancelled__": True}
                    return
                now = time.time()
                expired: list[Any] = []
                next_deadlines: list[float] = []
                for future in pending:
                    dimension, batch, _context = futures[future]
                    started_at = started_times.get(_job_key(dimension, batch))
                    if started_at is None:
                        continue
                    deadline = started_at + job_timeout
                    if deadline <= now:
                        # 刚好在超时点完成的批次仍然取结果，避免误判
                        if not future.done():
                            expired.append(future)
                    else:
                        next_deadlines.append(deadline)
                for future in expired:
                    dimension, batch, _context = futures[future]
                    started_at = started_times.get(_job_key(dimension, batch), now)
                    future.cancel()
                    pending.discard(future)
                    yield futures[future], {
                        "__error__": f"批次超时（>{int(job_timeout)}s），已跳过，重试时会重新生成",
                        "__timeout__": True,
                        "__duration_ms__": int((now - started_at) * 1000),
                    }
                if not pending:
                    break
                # 最多每 2s 醒一次：取消要能及时生效，不能等满一个批次超时
                timeout = 2.0
                if next_deadlines:
                    timeout = min(timeout, max(0.0, min(next_deadlines) - time.time()))
                done, _ = wait(
                    pending,
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    pending.discard(future)
                    try:
                        yield futures[future], future.result()
                    except Exception as exc:  # noqa: BLE001 - 批次失败不能拖死整个任务
                        dimension, batch, _context = futures[future]
                        started_at = started_times.get(_job_key(dimension, batch), time.time())
                        yield futures[future], {
                            "__error__": f"{type(exc).__name__}: {exc}",
                            "__duration_ms__": int((time.monotonic() - started_at) * 1000),
                        }
        finally:
            # 已超时的线程可能仍在网络等待；不要让任务线程再等它们
            pool.shutdown(wait=False, cancel_futures=True)


def _normalize_generated_case(raw: dict[str, Any]) -> dict[str, Any]:
    """归一化模型常见的 schema 偏差，避免为一个格式问题多跑一轮 repair。"""
    spec = dict(raw or {})
    preconditions = spec.get("preconditions")
    if isinstance(preconditions, list):
        normalized: list[dict[str, Any]] = []
        for item in preconditions:
            if isinstance(item, str):
                normalized.append({"kind": "manual", "note": item[:500]})
            elif isinstance(item, dict):
                value = dict(item)
                if not value.get("kind"):
                    if value.get("sql"):
                        value["kind"] = "db_fixture"
                    elif value.get("account_ref") or value.get("role"):
                        value["kind"] = "http_login"
                    else:
                        value["kind"] = "manual"
                        value.setdefault("note", str(value.get("name") or value.get("note") or "")[:500])
                normalized.append(value)
        spec["preconditions"] = normalized
    elif isinstance(preconditions, str):
        spec["preconditions"] = [{"kind": "manual", "note": preconditions[:500]}]
    return spec


def _invalid_cases(raw_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回不符合统一用例 schema 的用例（附错误说明）。"""
    invalid: list[dict[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            continue
        spec = dict(raw)
        spec.setdefault("schema_version", "1.0")
        spec.setdefault("steps", [])
        spec.setdefault("assertions", [])
        errors = validate_case_spec(spec)
        if errors:
            invalid.append({"title": raw.get("title", ""), "errors": errors[:5]})
    return invalid


def _merge_cases(original: list[dict[str, Any]], repaired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用修复后的用例替换掉不合法的那些（按标题对齐）。"""
    repaired_titles = {str(case.get("title", "")) for case in repaired}
    merged: list[dict[str, Any]] = []
    for case in original:
        title = str(case.get("title", ""))
        if title in repaired_titles:
            continue
        spec = dict(case)
        spec.setdefault("schema_version", "1.0")
        spec.setdefault("steps", [])
        spec.setdefault("assertions", [])
        if not validate_case_spec(spec):
            merged.append(case)
    merged.extend(repaired)
    return merged


def _normalize_assertion(raw: Any) -> dict[str, Any]:
    """把模型产出的断言归一化成执行器认的形状。

    模型偶尔会：
    - 把 ``evidence`` 写成字符串（``"openapi:xxx 说明"``）；
    - 用 ``name`` 代替 ``kind``；
    - 漏掉 ``operator``。

    这里统一修正，避免整条用例被丢弃。
    """
    if not isinstance(raw, dict):
        return {}
    assertion = dict(raw)
    # kind：兼容 name / type
    kind = assertion.get("kind") or assertion.get("name") or assertion.get("type")
    if isinstance(kind, str):
        kind = kind.strip().lower()
        kind = {
            "status": "status_code",
            "json": "json_path",
            "path": "json_path",
            "db": "db_state",
            "database": "db_state",
            "time": "timing",
            "duration": "timing",
            "text": "text_contains",
            "stdout": "stdout_contains",
            "stderr": "stderr_contains",
            "exit": "exit_code",
            "exit_status": "exit_code",
        }.get(kind, kind)
    assertion["kind"] = kind if kind in {
        "status_code",
        "json_path",
        "db_state",
        "cross_api",
        "timing",
        "text_contains",
        "exit_code",
        "stdout_contains",
        "stderr_contains",
    } else "status_code"
    assertion.setdefault("operator", "eq")
    if assertion["kind"] == "json_path" and "target" not in assertion:
        assertion["target"] = str(assertion.pop("name", "") or "")
    # evidence：字符串 → 对象
    evidence = assertion.get("evidence")
    if isinstance(evidence, str):
        text = evidence.strip()
        source_type = "doc"
        ref = text
        quote = ""
        for prefix in ("openapi:", "doc:", "code:", "manual:"):
            if text.startswith(prefix):
                source_type = prefix[:-1]
                rest = text[len(prefix) :]
                parts = rest.split(" ", 1)
                ref = parts[0]
                quote = parts[1] if len(parts) > 1 else ""
                break
        assertion["evidence"] = {"source_type": source_type, "ref": ref, "quote": quote}
    elif isinstance(evidence, list):
        first = evidence[0] if evidence else ""
        assertion["evidence"] = (
            {"source_type": "doc", "ref": str(first)[:200], "quote": str(first)[:200]}
            if first
            else {"source_type": "none", "ref": "", "note": "缺少依据"}
        )
    elif not isinstance(evidence, dict):
        assertion["evidence"] = {"source_type": "none", "ref": "", "note": "缺少依据"}
    # 没有依据来源 → 标成待确认（不判失败）
    if assertion["evidence"].get("source_type") in (None, "", "none"):
        assertion["status"] = "pending"
    return assertion


def _persist_case(
    session: Session, project: Project, feature: Feature, raw: dict[str, Any], dimension: str
) -> TestCase | None:
    spec = dict(raw or {})
    spec.setdefault("schema_version", "1.0")
    spec["dimension"] = spec.get("dimension") or dimension
    spec["type"] = spec.get("type") or "api"
    spec["feature"] = {"id": feature.id, "name": feature.name}
    spec.setdefault("steps", [])
    spec.setdefault("assertions", [])
    spec.setdefault("evidence", [])
    spec.setdefault("preconditions", [])
    spec.setdefault("cleanup", [])
    # 断言归一化（步骤内联断言 + 顶层断言）
    spec["assertions"] = [_normalize_assertion(a) for a in spec["assertions"] if isinstance(a, dict)]
    for step in spec["steps"]:
        if isinstance(step, dict) and step.get("assert"):
            step["assert"] = [_normalize_assertion(a) for a in step["assert"] if isinstance(a, dict)]

    from app.spec_repair import repair_spec

    repaired = repair_spec(spec)
    if repaired:
        logger.info("用例 %s 的断言类型已按步骤动作修正：%s", spec.get("title"), repaired)
    _ensure_performance_samples(spec, project)
    # 无依据的猜测不是断言：降级成 contract_questions，不进执行链也不计入 pending。
    moved = split_unbacked_assertions(spec)
    if moved:
        logger.info("用例 %s 有 %d 条无依据断言已转为待确认问题", spec.get("title"), len(moved))
    if spec.get("probe") and "probe" not in (spec.get("tags") or []):
        spec["tags"] = [*(spec.get("tags") or []), "probe"]

    from app.case_normalizer import normalize_case_auth

    normalize_case_auth(spec, list((decrypt_json(project.test_accounts_encrypted) or {}).keys()))
    errors = validate_case_spec(spec)
    if errors:
        logger.warning("用例 schema 校验失败（已丢弃）：%s；%s", spec.get("title"), errors[:3])
        return None

    assertions = collect_assertions(spec)
    evidence = spec.get("evidence") or []
    case = TestCase(
        project_id=project.id,
        feature_id=feature.id,
        title=(spec.get("title") or "未命名用例")[:400],
        dimension=spec["dimension"],
        case_type=spec["type"],
        preconditions={"items": spec.get("preconditions") or []},
        steps=spec.get("steps") or [],
        expected={"assertions": assertions},
        evidence=evidence,
        spec=spec,
        generation=GenerationSource.AGENT.value,
        status=CaseStatus.PENDING.value,
        tags=spec.get("tags") or [],
        missing_input=spec.get("missing_input") or None,
        verification_mode="frontend" if spec["type"] == "web" else "api",
    )
    session.add(case)
    return case


def _structured_with_retry(
    provider: LLMProvider,
    system: str,
    user: str,
    schema: dict[str, Any],
    context: dict[str, Any],
    max_retries: int,
    *,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """调用 provider；失败时把错误回灌重试；全部失败返回带 __error__ 的空结果。"""
    last_error = ""
    current = dict(context)
    for attempt in range(max_retries + 1):
        try:
            payload = provider.structured(
                system,
                user,
                schema,
                context=current,
                reasoning_effort=reasoning_effort,
            )
            if payload:
                return payload
            last_error = "模型返回空结果"
        except LLMError as exc:
            last_error = str(exc)
        # 重试时缩小素材量（推理模型输出被截断时能收敛）
        current = {**context, "last_error": last_error, "retry": attempt + 1, "shrink": attempt + 1}
    payload = dict(current.get("last_payload") or {})
    payload["__error__"] = last_error
    return payload


_CASE_SYSTEM_PROMPT = """你是资深测试设计工程师，负责为「{dimension}」维度设计测试用例。

关于文档来源（很重要）：
- role="spec" 的是**接口规范**（OpenAPI / 端点表）：可以作为**断言依据**引用；
- role="notes" 的是**设计笔记 / 开发日志**（todo、设计稿、开发记录）：
  用来理解产品设计意图、边界取舍、历史改动，**不能**作为断言依据引用
  （引用了也只能标为「待确认」，因为它记的是过程而不是契约）。

硬性要求：
1. 每个功能点至少一条正常流用例（functional 维度必须覆盖正常流），断言要验证业务结果（状态变化、金额、数据落库），不能只看状态码；
2. 边界维度覆盖：空值、超长、负数/零、非法状态跳转、重复提交、精度与时区；
3. 安全维度覆盖：越权（A 访问 B 的数据）、未鉴权访问、注入类输入、敏感信息泄露；
4. 性能维度只做接口级响应时间基线，阈值必须来自文档或项目配置中的 SLA；没有 SLA 就不要生成 timing 断言，把「缺少 SLA」写进 contract_questions（owner_kind=test-side），不要硬编阈值；带 timing 断言的请求步骤必须设置 samples（context.performance_samples，默认 20），执行器会按多次采样计算 P95；
5. 每条断言都必须有可引用依据：openapi / doc / code 写 ref；通用策略（不得 5xx、不得泄露堆栈/SQL）用 source_type=policy + ref=POLICY-ID；人工假设用 manual，并必须在 note 写作者与理由。**没有依据就不要写断言**；
6. 没有依据但确实需要确认的期望，写进 `contract_questions`：它没有 expected，不进入执行链、不计入待确认断言、不会被判失败。形状为 {{"question": "请确认……", "missing_contract": "缺少哪部分契约", "owner_kind": "sut-contract|test-side|environment"}}。只有连可执行步骤都无法构造（缺接口、必填参数、账号或环境）时才输出 missing_input，并让 steps 为空；
7. 用例可以绑定测试数据集：用 {{{{data.字段名}}}} 引用当前行；未绑定数据集时用 {{{{run_id}}}} / {{{{uuid}}}} / {{{{timestamp}}}} 生成唯一值。不要写未定义的 {{{{字段名}}}}——执行器只认 run_id / uuid / timestamp / save 变量 / data.* / account.*，其它占位符会报未解析变量；
8. 断言写在它要校验的那一步的 assert 数组里（这样能绑定该步骤的响应）；
   顶层 assertions 只用于跨步骤 / 用例级断言，通常留空；
   断言里的 expected / target 也支持 {{{{run_id}}}} / {{{{save 的变量}}}}，执行时统一渲染；
9. 创建或修改数据的用例，用 save 保存关键标识，并在 cleanup 里删除 / 还原测试数据
   （cleanup 在用例失败时也会执行；不要只依赖 run_id 前缀，否则数据会越积越多）。需要可复用前置数据时用 fixtures: [{{"kind":"api|sql|file|command","name":"...","steps"/"sql"/"file_id":...,"cleanup":[...]}}]；fixture 失败用例直接 error，不会继续跑 steps。
10. 必须严格遵守 endpoints[].request_schema 和 param_schema：
    integer 字段不要写 "P1" / "2" 这类字符串，object 字段不要写成字符串，
    array 字段不要写成对象；required 字段必须提供，enum 字段只能使用枚举值。
11. 错误响应先看 endpoints[].responses 和文档/代码：如果实际约定是
    {{"error": {{"code", "message", "details"}}}}，断言用 $.error / $.error.code /
    $.error.message，不要照搬 FastAPI 默认的 $.detail；无法确定时写 contract_questions，不要猜。
12. 异步任务：创建 run 后应保存立即存在的内部 task_id（$.task_id），
    不要使用要等 worker 准备环境后才写入的 berth_task_id；后者可能为 null。
13. 后续步骤要用的 ID 必须在创建步骤的 save 里明确保存，变量名前后一致；
    不要使用未 save 的 {{project_id}} / {{case_id}}。
14. 没有项目契约、但有通用风险假设时，输出探针用例：设置 "probe": true，steps 只发请求 / 走流程，
    不写具体 expected 断言；把想确认的行为写进 contract_questions。探针结果只作为候选，
    不会直接判失败或确认缺陷。
15. 变形 / 差分关系（创建后列表能查到、更新后详情能读到、删除后详情 404、分页不重不漏、
    排序生效、过滤结果是全集子集）优先用 contract 依据；没有依据的写 contract_questions。
16. 策略断言可直接标 confirmed：例如
    {{"kind": "status_code", "operator": "not_in", "expected": [500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511],
      "status": "confirmed", "evidence": {{"source_type": "policy", "ref": "POLICY-REL-001"}}}}。
17. **代码就是契约**：endpoints[].responses 为空时，用 endpoints[].source_refs（code 的 ref 形如 file.go:74）
    和 context.code_excerpts 里的 handler 代码确定状态码、响应 JSON 字段和错误结构；
    断言 evidence 写 {{"source_type": "code", "ref": "file.go:74", "quote": "代码里的关键行"}}。
    代码里也看不出来的才写 contract_questions，不要凭空猜。
18. 需要登录 / 鉴权的步骤必须引用项目账号：auth: {{"account": "<账号名>", "mode": "bearer|basic|private_token"}}；
    文件上传步骤用 files: {{"字段名": {{"file_id": 1}}}}（file_id 来自项目测试文件），不要在 body 里塞 base64。
    context.accounts 是可用账号名（不含密码）。不要写死 token / password / {{{{admin_token}}}}；
    没有可用账号时把「缺少测试账号」写进 missing_input，不要发字面量占位符。

可用的通用策略（source_type=policy 时 ref 引用其 ID）：
{policy_catalog}

输出格式（严格照抄这个结构；evidence 必须是**对象**，不是字符串）：
{{
  "cases": [
    {{
      "title": "[正常流] 创建用户 - POST /api/users",
      "dimension": "functional",
      "type": "api",
      "description": "按文档完成一次正常业务操作",
      "steps": [
        {{
          "action": "request",
          "method": "POST",
          "path": "/api/users",
          "headers": {{"Content-Type": "application/json"}},
          "body": {{"name": "shakedown-{{{{run_id}}}}-name"}},
          "assert": [
            {{
              "kind": "status_code",
              "operator": "eq",
              "expected": 201,
              "status": "confirmed",
              "evidence": {{"source_type": "openapi", "ref": "#/paths/~1api~1users/post", "quote": "成功返回 201"}},
              "description": "创建成功"
            }},
            {{
              "kind": "json_path",
              "target": "$.id",
              "operator": "exists",
              "expected": true,
              "status": "confirmed",
              "evidence": {{"source_type": "openapi", "ref": "#/paths/~1api~1users/post", "quote": "响应含 id"}}
            }}
          ]
        }}
      ],
      "assertions": [],
      "contract_questions": [],
      "probe": false,
      "tags": ["normal-flow"]
    }}
  ]
}}
"""


def _policy_catalog_prompt() -> str:
    return "\n".join(
        f"- {item['id']}（v{item['version']}）：{item['title']}" for item in policy_catalog()
    )


def _project_performance_samples(project: Project) -> int:
    settings = project.settings_json or {}
    try:
        value = int(settings.get("performance_samples") or 20)
    except (TypeError, ValueError):
        value = 20
    return max(1, min(value, 1000))


def _ensure_performance_samples(spec: dict[str, Any], project: Project) -> None:
    """给带 timing 断言的性能请求补采样次数，避免只测一次就宣称 P95。"""
    if spec.get("dimension") != Dimension.PERFORMANCE.value:
        return
    samples = _project_performance_samples(project)
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if any(
            isinstance(assertion, dict) and assertion.get("kind") == "timing"
            for assertion in step.get("assert") or []
        ):
            step.setdefault("samples", samples)


def _project_sla(project: Project) -> dict[str, Any]:
    """项目设置里的性能基线，作为文档 SLA 缺失时的显式兜底。"""
    settings = project.settings_json or {}
    value = settings.get("performance_sla_ms")
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return {}
    if threshold <= 0:
        return {}
    return {
        "threshold_ms": threshold,
        "ref": "project.settings.performance_sla_ms",
        "evidence": {
            "source_type": "manual",
            "ref": "project.settings.performance_sla_ms",
            "quote": f"项目配置性能基线：{threshold:g}ms",
        },
    }


def _extract_sla(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """从文档里找响应时间 SLA。"""
    import re

    for doc in docs:
        content = doc.get("content") or ""
        match = re.search(r"(?:响应时间|响应时延|延迟|latency)[^0-9]{0,12}(\d{2,5})\s*(ms|毫秒|s|秒)", content, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            if match.group(2).lower() in {"s", "秒"}:
                value *= 1000
            locator = doc.get("locator") or {}
            ref = locator.get("file_path") or " > ".join(locator.get("section_path") or []) or f"chunk:{doc.get('id')}"
            return {
                "threshold_ms": value,
                "ref": ref,
                "evidence": {"source_type": "doc", "ref": ref, "quote": match.group(0)},
            }
    return {}


# ── 失败归因 ────────────────────────────────────────────────
_COMMON_SENSE_FIELDS = {
    "name",
    "title",
    "label",
    "display_name",
    "project_name",
    "feature_name",
    "case_name",
    "task_name",
    "username",
    "slug",
    "key",
}
_ERROR_LEAK_MARKERS = ("psycopg", "traceback", "sqlstate", "invalidrowcount", "stack trace")


def _step_payloads(result: CaseResult, key: str) -> list[dict[str, Any]]:
    values = (result.output or {}).get(key)
    if isinstance(values, list) and values:
        return [item if isinstance(item, dict) else {} for item in values]
    fallback = getattr(result, key[:-1], {}) if key.endswith("s") else {}
    return [fallback] if isinstance(fallback, dict) and fallback else []


def _case_endpoints(session: Session, project: Project, case: TestCase) -> list[dict[str, Any]]:
    endpoints = session.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all()
    by_path = {
        f"{(endpoint.method or 'GET').upper()} {_path_template(endpoint.path or '/')}": endpoint
        for endpoint in endpoints
    }
    matched: list[dict[str, Any]] = []
    seen: set[int] = set()
    for step_index, step in enumerate((case.spec or {}).get("steps") or []):
        if not isinstance(step, dict) or step.get("action") != "request":
            continue
        method = str(step.get("method") or "GET").upper()
        raw_path = str(step.get("path") or "/")
        candidates = [raw_path]
        if raw_path.startswith("/") and not raw_path.startswith("/api"):
            candidates.append("/api" + raw_path)
        endpoint = None
        for candidate in candidates:
            endpoint = by_path.get(f"{method} {_path_template(candidate)}")
            if endpoint is not None:
                break
        if endpoint is None or endpoint.id in seen:
            continue
        seen.add(endpoint.id)
        responses = {
            str(code): _schema_brief((item or {}).get("schema") or {}, depth=2)
            for code, item in (endpoint.responses or {}).items()
            if isinstance(item, dict)
        }
        params = {
            group: [
                {
                    "name": item.get("name"),
                    "type": _schema_brief(item.get("schema") or {}).get("type"),
                    "required": bool(item.get("required")),
                    "enum": (item.get("schema") or {}).get("enum"),
                    "minimum": (item.get("schema") or {}).get("minimum"),
                    "maximum": (item.get("schema") or {}).get("maximum"),
                }
                for item in items or []
                if isinstance(item, dict) and item.get("name")
            ]
            for group, items in (endpoint.params or {}).items()
        }
        matched.append(
            {
                "id": endpoint.id,
                "step_index": step_index,
                "method": endpoint.method,
                "path": endpoint.path,
                "summary": endpoint.summary,
                "request_schema": _schema_brief(
                    ((endpoint.request_body or {}).get("schema") or {}),
                    depth=3,
                ),
                "params": params,
                "responses": responses,
            }
        )
    return matched[:8]


def _constraints_from_schema(schema: Any, prefix: str = "") -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list):
            for item in branches:
                found = _constraints_from_schema(item, prefix)
                if found:
                    return found
    constraints: list[dict[str, Any]] = []
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    for name, item in properties.items():
        if not isinstance(item, dict):
            continue
        path = f"{prefix}.{name}".lstrip(".")
        values = {
            key: item[key]
            for key in ("type", "minLength", "maxLength", "minimum", "maximum", "enum", "pattern")
            if key in item
        }
        if path in required:
            values["required"] = True
        if values:
            constraints.append({"field": path, **values})
        nested = _constraints_from_schema(item, path)
        constraints.extend(nested)
    return constraints[:80]


def _relevant_code_chunks(
    session: Session,
    project: Project,
    case: TestCase,
    endpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keywords = {case.title}
    feature = session.get(Feature, case.feature_id) if case.feature_id else None
    if feature is not None:
        keywords.add(feature.name)
    for endpoint in endpoints:
        keywords.add(str(endpoint.get("path") or ""))
        keywords.add(str(endpoint.get("summary") or ""))
    pieces = [piece for piece in re.split(r"[^0-9A-Za-z_\u4e00-\u9fff]+", " ".join(keywords)) if len(piece) >= 3]
    chosen: list[dict[str, Any]] = []
    for chunk in _code_chunks(session, project, limit=300):
        content = str(chunk.get("content") or "")
        score = sum(1 for piece in pieces if piece.lower() in content.lower())
        if score == 0:
            continue
        chosen.append(
            {
                "score": score,
                "locator": chunk.get("locator") or {},
                "content": content[:1800],
            }
        )
        if len(chosen) >= 20:
            break
    chosen.sort(key=lambda item: int(item["score"]), reverse=True)
    return chosen[:5]


def _relevant_docs(
    session: Session,
    project: Project,
    case: TestCase,
    endpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keywords = {case.title}
    feature = session.get(Feature, case.feature_id) if case.feature_id else None
    if feature is not None:
        keywords.add(feature.name)
    for endpoint in endpoints:
        keywords.add(str(endpoint.get("path") or ""))
    chosen: list[dict[str, Any]] = []
    for chunk in _doc_chunks(session, project, limit=80):
        content = str(chunk.get("content") or "")
        score = sum(1 for keyword in keywords if keyword and keyword.lower() in content.lower())
        if score:
            chosen.append(
                {
                    "score": score,
                    "role": chunk.get("role") or "spec",
                    "locator": chunk.get("locator") or {},
                    "content": content[:1200],
                }
            )
    chosen.sort(key=lambda item: int(item["score"]), reverse=True)
    return chosen[:3]


def _failed_assertions(result: CaseResult) -> list[dict[str, Any]]:
    return [
        item
        for item in (result.output or {}).get("assertions") or []
        if isinstance(item, dict) and not item.get("passed") and not item.get("pending")
    ]


def _expected_error(expected: Any) -> bool:
    values = expected if isinstance(expected, list) else [expected]
    for value in values:
        try:
            if int(value) >= 400:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _empty_semantic_fields(body: Any) -> list[str]:
    found: list[str] = []
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, dict):
                found.extend(f"{key}.{item}" for item in _empty_semantic_fields(value))
            elif str(key).lower() in _COMMON_SENSE_FIELDS and isinstance(value, str) and not value.strip():
                found.append(str(key))
            elif str(key).lower() in _COMMON_SENSE_FIELDS and value is None:
                found.append(str(key))
    return found


def _negative_limit(request: dict[str, Any]) -> bool:
    for key in ("query", "body"):
        value = request.get(key)
        if not isinstance(value, dict):
            continue
        try:
            if "limit" in value and int(value["limit"]) < 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _deterministic_signals(
    case: TestCase,
    result: CaseResult,
    endpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requests = _step_payloads(result, "requests")
    responses = _step_payloads(result, "responses")
    constraints_by_step = {
        int(endpoint.get("step_index", -1)): _constraints_from_schema(endpoint.get("request_schema") or {})
        for endpoint in endpoints
        if isinstance(endpoint.get("step_index"), int)
    }
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, detail: str, *, source: str, field: str = "", confidence: float = 0.9) -> None:
        key = f"{kind}:{field}:{detail}"
        if key in seen:
            return
        seen.add(key)
        signals.append(
            {
                "type": kind,
                "source": source,
                "field": field,
                "detail": detail,
                "confidence": confidence,
            }
        )

    for assertion in _failed_assertions(result):
        if assertion.get("kind") != "status_code":
            continue
        index = assertion.get("step_index")
        request = requests[index] if isinstance(index, int) and 0 <= index < len(requests) else {}
        response = responses[index] if isinstance(index, int) and 0 <= index < len(responses) else {}
        try:
            actual = int(assertion.get("actual"))
        except (TypeError, ValueError):
            try:
                actual = int(response.get("status_code"))
            except (TypeError, ValueError):
                actual = 0
        expected = assertion.get("expected")
        body = request.get("body")
        text = json.dumps(response.get("body"), ensure_ascii=False, default=str)
        leak = next((marker for marker in _ERROR_LEAK_MARKERS if marker in text.lower()), "")
        if actual >= 500:
            add(
                "server_error",
                f"{request.get('method', '')} {request.get('url', '')} 返回 {actual}，期望 {expected}",
                source="result",
                confidence=0.98,
            )
        if leak:
            add(
                "error_detail_leak",
                f"错误响应包含 {leak}，疑似数据库/堆栈信息泄露",
                source="result",
                confidence=0.96,
            )
        if 200 <= actual < 300 and _expected_error(expected):
            empty_fields = _empty_semantic_fields(body)
            for field in empty_fields:
                step_constraints = constraints_by_step.get(index, []) if isinstance(index, int) else []
                explicit = next(
                    (
                        item
                        for item in step_constraints
                        if item.get("field") == field and int(item.get("minLength") or 0) >= 1
                    ),
                    None,
                )
                add(
                    "explicit_constraint_violation" if explicit else "common_sense_empty_identifier",
                    f"{request.get('method', '')} {request.get('url', '')} 接受了空值字段 {field}，期望 {expected}",
                    source="openapi" if explicit else "common_sense",
                    field=field,
                    confidence=0.95 if explicit else 0.88,
                )
            if _negative_limit(request):
                add(
                    "negative_limit_accepted",
                    f"{request.get('method', '')} {request.get('url', '')} 接受了负数 limit",
                    source="common_sense",
                    field="limit",
                    confidence=0.9,
                )
    return signals


def _apply_deterministic_attribution(
    case: TestCase,
    result: CaseResult,
    endpoints: list[dict[str, Any]],
    data: dict[str, Any],
) -> dict[str, Any]:
    # 环境/平台结论不能被「业务信号」覆盖：请求都没到，谈不上缺陷。
    if data.get("category") in {
        AttributionCategory.ENVIRONMENT.value,
        failure_attribution.CATEGORY_PLATFORM,
    }:
        return data
    signals = _deterministic_signals(case, result, endpoints)
    if not signals:
        return data
    confidence = max([float(item.get("confidence") or 0.0) for item in signals] + [0.0])
    data["category"] = AttributionCategory.DEFECT.value
    data["is_defect"] = True
    data["confidence"] = max(float(data.get("confidence") or 0.0), confidence)
    data["auto_confirm"] = confidence >= 0.85
    data["deterministic_signals"] = signals
    data["evidence"] = [
        {
            "source": item["source"],
            "type": item["type"],
            "field": item.get("field", ""),
            "detail": item["detail"],
        }
        for item in signals
    ] + list(data.get("evidence") or [])
    prefix = "确定性规则判定：" + "；".join(item["detail"] for item in signals)
    reason = str(data.get("reason") or "")
    data["reason"] = prefix + (("\n" + reason) if reason else "")
    return data


def attribute_failure(
    session: Session,
    project: Project,
    provider: LLMProvider,
    case: TestCase,
    result: CaseResult,
) -> dict[str, Any]:
    spec = case.spec or {}
    endpoints = _case_endpoints(session, project, case)
    endpoint_constraints = [
        {
            "step_index": endpoint.get("step_index"),
            "method": endpoint.get("method"),
            "path": endpoint.get("path"),
            "constraints": _constraints_from_schema(endpoint.get("request_schema") or {}),
        }
        for endpoint in endpoints
    ]
    context = {
        "task": "failure_attribution",
        "case": {
            "id": case.id,
            "title": case.title,
            "dimension": case.dimension,
            "steps": spec.get("steps") or [],
            "expected": spec.get("assertions") or [],
            "evidence": spec.get("evidence") or [],
        },
        "result": {
            "status": result.status,
            "request": result.request,
            "response": result.response,
            "requests": _step_payloads(result, "requests"),
            "responses": _step_payloads(result, "responses"),
            "logs": (result.logs or "")[:4000],
            "errors": (result.output or {}).get("errors") or [],
            "assertions": (result.output or {}).get("assertions") or [],
            "assertion_failed": (result.output or {}).get("assertion_failed", False),
        },
        "endpoint_schemas": endpoints,
        "endpoint_constraints": endpoint_constraints,
        "project_rules": (project.settings_json or {}).get("business_rules") or {},
        "code_evidence": _relevant_code_chunks(session, project, case, endpoints),
        "doc_evidence": _relevant_docs(session, project, case, endpoints),
        "common_sense_rules": [
            "name / title / label / username 等语义标识字段为空或纯空白时，创建或更新成功后没有业务意义，应视为输入校验缺陷。",
            "limit / offset / page 等数量参数为负数时，不应进入数据库层；返回 5xx 或泄露驱动异常属于缺陷。",
            "HTTP 5xx、SQL / 驱动异常 / 堆栈信息出现在响应中，属于服务端错误或信息泄露，不需要额外契约依据。",
            "显式 schema 的 minLength / minimum / maximum / enum / pattern 被接受时，属于契约违反。",
        ],
    }
    system = (
        "你是测试失败分析专家。对失败结果判断类别：environment（环境问题）/ case（用例问题）/ "
        "defect（疑似真实缺陷）/ unconfirmed（依据不足）。必须给出理由与依据。"
        "证据优先级：显式 schema 约束 > HTTP 5xx/异常泄露 > 语义常识 > 用例猜测。"
        "如果实际结果接受了空 name/title 等无业务意义的标识字段，或 5xx/泄露 SQL、堆栈，"
        "即使文档没有写约束，也应按常识判为 defect，并在 evidence 中标注 common_sense/result。"
        "只有实际结果与有依据的预期明确冲突时才判定为 defect；环境错误不要判成产品缺陷。"
    )
    user = f"分析这条失败用例：{case.title}"
    started = time.time()

    # 确定性规则先行：环境/平台/5xx 这类结论不依赖模型，先命中先返回，
    # 避免把「请求没到」判成产品缺陷，也避免把平台的断言类型缺陷赖到用例头上。
    rules = failure_attribution.classify_result(case, result)
    if rules is not None and float(rules.get("confidence") or 0) >= 0.85:
        record_trace(
            session,
            project_id=project.id,
            run_id=result.run_id,
            kind="analyze",
            step="attribute_failure",
            payload={
                "input": {"case_id": case.id, "result_id": result.id},
                "output": rules,
                "duration_ms": int((time.time() - started) * 1000),
                "source": "rules",
            },
        )
        return rules

    try:
        data = provider.structured(system, user, ATTRIBUTION_SCHEMA, context=context)
    except LLMError as exc:
        data = {
            "category": AttributionCategory.UNCONFIRMED.value,
            "reason": f"归因失败：{exc}",
            "confidence": 0.0,
            "evidence": [],
            "is_defect": False,
        }
    if rules is not None:
        data["rules"] = {
            key: rules.get(key)
            for key in ("category", "reason_code", "reason_label", "reason", "confidence", "fixable", "fix")
        }
        data["evidence"] = [*(rules.get("evidence") or []), *(data.get("evidence") or [])]
        if data.get("category") in {None, "", AttributionCategory.UNCONFIRMED.value} and rules.get("category"):
            data["category"] = rules["category"]
            data["reason"] = rules.get("reason") or data.get("reason") or ""
            data["confidence"] = rules.get("confidence") or data.get("confidence") or 0.0
    category = data.get("category", AttributionCategory.UNCONFIRMED.value)
    allowed = {c.value for c in AttributionCategory} | {failure_attribution.CATEGORY_PLATFORM}
    data["category"] = category if category in allowed else AttributionCategory.UNCONFIRMED.value
    data.setdefault("reason", "")
    data.setdefault("confidence", 0.0)
    data.setdefault("evidence", [])
    data["is_defect"] = bool(data.get("is_defect")) or data["category"] == AttributionCategory.DEFECT.value
    data = _apply_deterministic_attribution(case, result, endpoints, data)

    record_trace(
        session,
        project_id=project.id,
        run_id=result.run_id,
        kind="analyze",
        step="attribute_failure",
        payload={
            "input": {"case_id": case.id, "result_id": result.id},
            "output": data,
            "duration_ms": int((time.time() - started) * 1000),
        },
    )
    return data


# ── 白盒走查 ────────────────────────────────────────────────
WHITEBOX_MAX_TURNS = 5


def whitebox_review(
    session: Session,
    project: Project,
    provider: LLMProvider,
    *,
    use_tools: bool = True,
    on_progress: Callable[[int, int, str, set[str]], None] | None = None,
    completed_modules: set[str] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[Finding]:
    """白盒代码走查。

    两种模式：
    - 小模块（代码量 < 120k 字符）：一次性把代码切片交给结构化输出，减少工具往返；
    - 大模块：走 **LangGraph 工具循环**，Agent 自己用 ``list_code_files`` /
      ``grep_code`` / ``read_code_file`` 翻代码，避免上下文被超大代码块撑爆。

    每条发现都会关联到一条「白盒建议用例」；重复走查按「标题 + 文件 + 起始行」更新，
    不重复制造 finding / 建议用例。
    """
    chunks = _code_chunks(session, project)
    if not chunks:
        return []
    ctx = _project_context(session, project)
    valid_locations = _valid_code_locations(ctx)

    findings: list[Finding] = []
    modules = list(_group_chunks_by_module(chunks).items())
    total = len(modules)
    done_modules = set(completed_modules or ())
    for index, (module, module_chunks) in enumerate(modules):
        if module in done_modules:
            continue
        if should_cancel and should_cancel():
            break
        if on_progress:
            on_progress(len(done_modules), total, module, set(done_modules))
        system = (
            "你是代码走查专家。按模块检查逻辑风险：入参未校验、事务缺失、越权判断缺位、并发问题、"
            "异常吞没、敏感信息日志等。每条发现必须给出代码位置（文件+行号），无法定位的不要输出。"
            "可以用 list_code_files / grep_code / read_code_file 工具自己查代码。"
        )
        user = f"走查模块 {module} 的代码，输出高价值发现。"
        started = time.time()
        data: dict[str, Any] = {}
        module_chars = sum(len(str(item.get("content") or "")) for item in module_chunks)
        try:
            tools_available = use_tools and getattr(provider, "supports_tools", True)
            if tools_available:
                # 工具型 Agent：让模型自己翻代码，最后按 JSON 给出结论
                task = (
                    f"{user}\n"
                    "请先用工具查看该模块的代码，再输出 JSON："
                    '{"findings": [{"title": "...", "description": "...", "severity": "high|medium|low",'
                    ' "risk_type": "...", "module": "...", '
                    ' "code_location": {"file_path": "...", "start_line": 1, "end_line": 2, "snippet": "..."},'
                    ' "evidence": [{"source_type": "code", "ref": "file:line", "quote": "..."}]}]}\n'
                    "只输出 JSON，不要其它文字。"
                )
                final = analyze_with_tools(
                    session,
                    project,
                    provider,
                    task,
                    system=system,
                    max_turns=WHITEBOX_MAX_TURNS,
                    tracer=_trace_tracer(session, project.id, "whitebox", f"review:{module}"),
                    reasoning_effort="medium",
                )
                data = _parse_json_loose(final)
            else:
                # 离线 mock provider 不会主动调工具 → 直接给切片
                context = {"task": "whitebox_review", "module": module, "code_chunks": module_chunks}
                data = provider.structured(
                    system,
                    user,
                    WHITEBOX_SCHEMA,
                    context=context,
                    reasoning_effort="medium",
                    max_tokens=8192,
                )
        except LLMError as exc:
            record_trace(
                session,
                project_id=project.id,
                kind="whitebox",
                step=f"review:{module}",
                payload={"input": {"module": module}, "error": str(exc)},
            )
            if on_progress:
                on_progress(len(done_modules), total, module, set(done_modules))
            continue
        count = 0
        for raw in data.get("findings", []):
            location = raw.get("code_location") or {}
            if not _location_is_valid(location, valid_locations):
                continue
            title = (raw.get("title") or "未命名发现")[:400]
            evidence = _normalize_whitebox_evidence(raw, location)
            existing = _find_existing_whitebox_finding(session, project.id, title, location)
            if existing is not None:
                existing.description = raw.get("description") or existing.description
                existing.severity = _normalize_severity(raw.get("severity"))
                existing.evidence = evidence
                existing.code_location = location
                existing.module = raw.get("module") or module
                existing.risk_type = raw.get("risk_type") or existing.risk_type
                finding = existing
            else:
                finding = Finding(
                    project_id=project.id,
                    source=FindingSource.WHITEBOX.value,
                    title=title,
                    description=raw.get("description") or "",
                    severity=_normalize_severity(raw.get("severity")),
                    evidence=evidence,
                    code_location=location,
                    module=raw.get("module") or module,
                    risk_type=raw.get("risk_type") or "",
                    status="pending",
                )
                session.add(finding)
            session.flush()
            case = _suggest_case_for_finding(session, project, finding)
            if case is not None:
                finding.case_id = case.id
            findings.append(finding)
            count += 1
        record_trace(
            session,
            project_id=project.id,
            kind="whitebox",
            step=f"review:{module}",
            payload={
                "input": {"module": module, "chunks": len(module_chunks), "chars": module_chars},
                "output": {"findings": count},
                "duration_ms": int((time.time() - started) * 1000),
            },
        )
        done_modules.add(module)
        if on_progress:
            on_progress(len(done_modules), total, module, set(done_modules))
        if should_cancel and should_cancel():
            break
    session.flush()
    return findings


def _normalize_severity(value: Any) -> str:
    allowed = {s.value for s in Severity}
    text = str(value or "").lower()
    return text if text in allowed else Severity.MEDIUM.value


_WHITEBOX_RISK_DIMENSION = {
    "sql_injection": "security",
    "dangerous_eval": "security",
    "dangerous_exec": "security",
    "auth_disabled": "security",
    "cors_wildcard": "security",
    "sensitive_log": "security",
    "exception_swallowed": "boundary",
}


def _normalize_whitebox_evidence(raw: dict[str, Any], location: dict[str, Any]) -> list[dict[str, Any]]:
    """白盒模型有时把 evidence 输出成字符串数组；统一成 FindingOut 要求的对象数组。"""
    evidence = raw.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    file_path = str(location.get("file_path") or "")
    start_line = location.get("start_line") or ""
    ref = f"{file_path}:{start_line}" if file_path else ""
    normalized: list[dict[str, Any]] = []
    for item in evidence:
        if isinstance(item, dict):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append({"source_type": "code", "ref": ref, "quote": item[:500]})
        else:
            normalized.append({"source_type": "code", "ref": ref, "quote": str(item)[:500]})
    return normalized


def _find_existing_whitebox_finding(
    session: Session,
    project_id: int,
    title: str,
    location: dict[str, Any],
) -> Finding | None:
    """重复走查时按「标题 + 文件 + 起始行」复用 finding，避免刷屏。"""
    candidates = session.scalars(
        select(Finding).where(
            Finding.project_id == project_id,
            Finding.source == FindingSource.WHITEBOX.value,
            Finding.title == title,
        )
    ).all()
    file_path = str(location.get("file_path") or "")
    start_line = int(location.get("start_line") or 0)
    for finding in candidates:
        current = finding.code_location or {}
        if str(current.get("file_path") or "") == file_path and int(current.get("start_line") or 0) == start_line:
            return finding
    return None


def _suggest_case_for_finding(session: Session, project: Project, finding: Finding) -> TestCase | None:
    """白盒发现 → 建议一条黑盒验证用例（待人工补全后启用）。

    重复走查复用同一条建议用例，避免每次生成一批重复的 pending 用例。
    """
    location = finding.code_location or {}
    dimension = _WHITEBOX_RISK_DIMENSION.get(finding.risk_type, "security")
    evidence = {
        "source_type": "code",
        "ref": f"{location.get('file_path', '')}:{location.get('start_line', '')}",
        "quote": (location.get("snippet") or "")[:200],
    }
    spec = {
        "schema_version": "1.0",
        "title": f"[白盒建议] {finding.title}"[:400],
        "dimension": dimension,
        "type": "api",
        "description": finding.description,
        "preconditions": [],
        "steps": [],
        "assertions": [],
        "evidence": [evidence],
        "tags": ["whitebox", finding.risk_type or "risk"],
        "missing_input": f"需要人工补全：针对 {location.get('file_path', '?')}:{location.get('start_line', '?')} 的风险，"
        "确定对应接口、输入与预期结果",
    }
    existing = session.scalar(
        select(TestCase)
        .where(TestCase.project_id == project.id, TestCase.title == spec["title"][:400])
        .order_by(TestCase.id)
        .limit(1)
    )
    if existing is not None:
        return existing
    from app.case_verification import infer_verification_mode

    location = finding.code_location or {}
    mode = infer_verification_mode(
        str(location.get("file_path") or ""),
        finding.title,
        "api",
    )
    case = TestCase(
        project_id=project.id,
        feature_id=None,
        title=spec["title"][:400],
        dimension=dimension,
        case_type="api",
        preconditions={"items": []},
        steps=[],
        expected={"assertions": []},
        evidence=[evidence],
        spec=spec,
        generation=GenerationSource.AGENT.value,
        status=CaseStatus.PENDING.value,
        tags=spec["tags"],
        missing_input=spec["missing_input"],
        verification_mode=mode,
    )
    session.add(case)
    session.flush()
    return case


def _group_chunks_by_module(chunks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按文件分组（白盒走查用）。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        locator = chunk.get("locator") or {}
        file_path = locator.get("file_path") or "unknown"
        module = file_path.split("/")[0] if "/" in file_path else file_path
        groups.setdefault(module, []).append(chunk)
    return groups


def _valid_code_locations(ctx: ToolContext) -> dict[str, tuple[int, int]]:
    locations: dict[str, tuple[int, int]] = {}
    for root in ctx.code_roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                continue
            try:
                lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue
            locations[rel] = (1, lines)
    return locations


def _location_is_valid(location: dict[str, Any], valid: dict[str, tuple[int, int]]) -> bool:
    file_path = (location or {}).get("file_path") or ""
    if not file_path or file_path not in valid:
        return False
    start = int(location.get("start_line") or 0)
    end = int(location.get("end_line") or start)
    low, high = valid[file_path]
    return low <= start <= high and low <= end <= high


def _parse_json_loose(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON（容忍 ```json 包裹与前后废话）。"""
    from app.agent.providers.base import extract_json

    try:
        return extract_json(text or "")
    except LLMError:
        return {"findings": []}


def build_tool_context(session: Session, project: Project) -> ToolContext:
    return _project_context(session, project)


ALL_TOOLS = CODE_TOOLS + KNOWLEDGE_TOOLS
