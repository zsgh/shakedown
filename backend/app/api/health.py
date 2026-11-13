"""健康检查。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.providers import get_provider
from app.db import get_db
from app.sandbox import get_sandbox

router = APIRouter(tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    database_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        database_error = str(exc)
    else:
        database_error = None
    payload: dict[str, Any] = {"status": "ok" if database_ok else "degraded", "database": database_ok}
    if database_error:
        payload["database_error"] = database_error
    return payload


@router.get("/health/live")
def health_live() -> dict[str, Any]:
    """进程存活探针（不查依赖）。"""
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, Any]:
    """就绪探针：数据库 + 迁移版本；不触发 LLM / 沙箱。"""
    database_ok = True
    migration = ""
    error = ""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        error = str(exc)
    else:
        try:
            migration = str(db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar() or "")
        except Exception:  # noqa: BLE001 —— 测试库可能由 create_all 建表，没有 alembic_version
            migration = ""
    return {
        "status": "ready" if database_ok else "not_ready",
        "database": database_ok,
        "migration": migration,
        "error": error,
    }


@router.get("/health/full")
def health_full() -> dict[str, Any]:
    provider = get_provider()
    sandbox = get_sandbox()
    return {
        "status": "ok",
        "llm": provider.health(),
        "sandbox": sandbox.health(),
    }
