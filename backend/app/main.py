"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse

from app.api.router import api_router
from app.config import get_settings
from app.db import SessionLocal
from app.errors import register_exception_handlers
from app.logging import get_logger
from app.mcp.http import build_mcp_app
from app.metrics import new_trace_id, record_request, set_trace_id
from app.rbac import enforce_api_actor
from app.worker.worker import start_worker, stop_worker

logger = get_logger("shakedown.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Shakedown 启动：LLM provider=%s sandbox=%s", settings.llm_provider, settings.sandbox_mode)
    start_worker()
    mcp_app = app.state.mcp_app
    try:
        async with mcp_app.inner.router.lifespan_context(mcp_app.inner):
            yield
    finally:
        stop_worker()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Shakedown",
        description="Shakedown —— 软件测试 Agent 平台（skipper 规划 / berth 沙箱执行）",
        version="0.1.0",
        lifespan=lifespan,
    )
    mcp_app = build_mcp_app()
    app.state.mcp_app = mcp_app
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @app.middleware("http")
    async def _metrics_and_trace(request: Request, call_next):  # type: ignore[no-untyped-def]
        trace_id = request.headers.get("X-Shakedown-Trace-Id") or new_trace_id()
        set_trace_id(trace_id)
        started = time.perf_counter()
        response = None
        actor = request.headers.get("X-Shakedown-Actor", "")
        tenant = request.headers.get("X-Shakedown-Tenant", "")
        if (actor or tenant) and request.url.path.startswith("/api"):
            with SessionLocal() as session:
                try:
                    enforce_api_actor(
                        session,
                        method=request.method,
                        path=request.url.path,
                        actor=actor,
                        tenant=tenant,
                    )
                except PermissionError as exc:
                    response = JSONResponse(
                        status_code=403,
                        content={"error": {"code": "forbidden", "message": str(exc), "details": None}},
                    )
        if response is None:
            response = await call_next(request)
        record_request(
            request.method,
            request.url.path,
            response.status_code,
            time.perf_counter() - started,
        )
        response.headers["X-Shakedown-Trace-Id"] = trace_id
        return response

    register_exception_handlers(app)
    app.include_router(api_router)
    app.mount("/mcp", mcp_app)

    @app.get("/", tags=["health"])
    def root() -> dict[str, Any]:
        return {
            "name": "Shakedown",
            "version": "0.1.0",
            "docs": "/docs",
            "api": "/api",
        }

    @app.get("/api/artifacts/{path:path}", tags=["artifacts"])
    def artifact(path: str) -> FileResponse:
        """只读地产物文件（截图 / trace），限制在 artifacts 目录内。"""
        root = settings.artifacts_dir.resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=404, detail="artifact not found")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(target)

    def custom_openapi() -> dict[str, Any]:
        """把默认 ``{"detail": ...}`` 错误 schema 改成项目真实错误信封。

        FastAPI 默认生成 HTTPValidationError(detail)，但本项目统一返回
        ``{"error": {"code", "message", "details"}}``。不修 OpenAPI 会让
        生成器和外部 Agent 一直按错误的契约写断言。
        """
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ErrorEnvelope"] = {
            "type": "object",
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {"description": "结构化校验/业务错误详情"},
                    },
                }
            },
        }
        for path_item in (schema.get("paths") or {}).values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                responses = operation.get("responses") or {}
                for response in responses.values():
                    if not isinstance(response, dict):
                        continue
                    content = response.get("content")
                    if not isinstance(content, dict) or "application/json" not in content:
                        continue
                    # 只替换错误响应；成功响应保持业务 schema。
                    status = next(
                        (code for code, value in responses.items() if value is response),
                        "",
                    )
                    if str(status).startswith("2"):
                        continue
                    content["application/json"]["schema"] = {
                        "$ref": "#/components/schemas/ErrorEnvelope"
                    }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    return app


app = create_app()
