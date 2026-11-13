"""数据库引擎 / 会话 / 基础声明。"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    type_annotation_map = {dict[str, Any]: None}


def _engine_kwargs() -> dict[str, Any]:
    settings = get_settings()
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "future": True, "pool_pre_ping": True}
    if settings.database_url.startswith("postgresql"):
        kwargs.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
    return kwargs


engine = create_engine(get_settings().sqlalchemy_url, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _set_pg_search_path(dbapi_connection, connection_record) -> None:  # pragma: no cover - 连接钩子
    try:
        with dbapi_connection.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
    except Exception:  # noqa: BLE001
        pass


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / worker 用的会话上下文（自动提交）。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """仅用于测试：按模型建表（生产用 alembic 迁移）。"""
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine)
