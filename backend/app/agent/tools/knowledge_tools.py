"""知识检索工具：向量检索文档 / 代码切片、查看接口清单与功能点。

这四个工具覆盖了「Agent 查项目知识」的主要场景：
- 想知道「文档里怎么说的」→ ``search_knowledge``（pgvector 语义检索）
- 想知道「有哪些接口」→ ``list_endpoints``
- 想看某个接口的完整定义 → ``read_endpoint``
- 想知道「功能点是什么」→ ``list_features``
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.ingest.embeddings import Embedder
from app.models import Chunk, Endpoint, Feature


class SearchKnowledge(Tool):
    """在项目知识切片里做语义检索。"""

    name = "search_knowledge"
    description = (
        "在项目的知识切片（接口文档 + 设计笔记 + 代码）里做语义检索，返回带来源位置的片段。"
        "当你想了解某个功能「文档里怎么描述的」时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "top_k": {"type": "integer", "description": "返回条数，默认 8"},
        },
        "required": ["query"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, error="缺少 query 参数")
        top_k = int(args.get("top_k") or 8)
        embedder = Embedder()
        vector = embedder.embed(query)
        chunks = ctx.session.scalars(
            select(Chunk)
            .where(Chunk.project_id == ctx.project.id)
            .order_by(Chunk.embedding.cosine_distance(vector))
            .limit(top_k)
        ).all()
        results = [
            {
                "id": chunk.id,
                "source_id": chunk.source_id,
                "locator": chunk.locator,
                "role": (chunk.meta_json or {}).get("role", "spec"),
                "content": chunk.content[:1500],
                "distance": float(chunk.embedding.cosine_distance(vector)),
            }
            for chunk in chunks
        ]
        return ToolResult(ok=True, data={"results": results, "count": len(results)})


class ListEndpoints(Tool):
    """列出项目接口清单。"""

    name = "list_endpoints"
    description = (
        "列出项目的接口清单（method / path / 说明 / 来源 / 一致性状态）。"
        "当你需要知道「这个项目对外提供了哪些接口」时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "description": "按方法过滤，如 GET"},
            "path_contains": {"type": "string", "description": "按路径关键字过滤"},
            "limit": {"type": "integer", "description": "最多返回条数"},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        stmt = select(Endpoint).where(Endpoint.project_id == ctx.project.id)
        method = (args.get("method") or "").upper()
        if method:
            stmt = stmt.where(Endpoint.method == method)
        path_contains = args.get("path_contains")
        if path_contains:
            stmt = stmt.where(Endpoint.path.ilike(f"%{path_contains}%"))
        stmt = stmt.order_by(Endpoint.path, Endpoint.method).limit(int(args.get("limit") or 100))
        endpoints = ctx.session.scalars(stmt).all()
        return ToolResult(
            ok=True,
            data={
                "endpoints": [
                    {
                        "id": e.id,
                        "method": e.method,
                        "path": e.path,
                        "summary": e.summary,
                        "sources": e.sources,
                        "consistency": e.consistency.get("state", "unknown"),
                    }
                    for e in endpoints
                ],
                "count": len(endpoints),
            },
        )


class ReadEndpoint(Tool):
    """读取单个接口的完整定义。"""

    name = "read_endpoint"
    description = (
        "读取单个接口的完整定义（参数 / 请求体 / 响应 / 鉴权 / 来源）。"
        "当你需要知道某个接口的字段细节时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {"endpoint_id": {"type": "integer"}},
        "required": ["endpoint_id"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        endpoint_id = args.get("endpoint_id")
        endpoint = ctx.session.get(Endpoint, endpoint_id)
        if endpoint is None or endpoint.project_id != ctx.project.id:
            return ToolResult(ok=False, error=f"接口不存在：{endpoint_id}")
        return ToolResult(
            ok=True,
            data={
                "id": endpoint.id,
                "method": endpoint.method,
                "path": endpoint.path,
                "summary": endpoint.summary,
                "params": endpoint.params,
                "request_body": endpoint.request_body,
                "responses": endpoint.responses,
                "security": endpoint.security,
                "examples": endpoint.examples,
                "sources": endpoint.sources,
                "consistency": endpoint.consistency,
            },
        )


class ListFeatures(Tool):
    """列出已抽取的功能点。"""

    name = "list_features"
    description = (
        "列出已抽取的功能点（名称 / 模块 / 说明 / 来源引用 / 关联接口）。"
        "当你需要了解「这个项目有哪些功能」时使用。"
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        features = ctx.session.scalars(select(Feature).where(Feature.project_id == ctx.project.id)).all()
        return ToolResult(
            ok=True,
            data={
                "features": [
                    {
                        "id": f.id,
                        "name": f.name,
                        "module": f.module,
                        "description": f.description,
                        "source_refs": f.source_refs,
                        "endpoint_ids": f.endpoint_ids,
                    }
                    for f in features
                ],
                "count": len(features),
            },
        )


KNOWLEDGE_TOOLS = [SearchKnowledge(), ListEndpoints(), ReadEndpoint(), ListFeatures()]
