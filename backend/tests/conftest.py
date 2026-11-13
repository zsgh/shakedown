"""pytest 公共夹具：测试库 + 表清理。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

# 测试库（默认与开发库同名加 _test 后缀）
os.environ.setdefault(
    "SHAKEDOWN_DATABASE_URL",
    "postgresql+psycopg://shakedown:shakedown@localhost:5432/shakedown_test",
)
os.environ.setdefault("SHAKEDOWN_LLM_PROVIDER", "mock")
os.environ.setdefault("SHAKEDOWN_SANDBOX_MODE", "local")
os.environ.setdefault("SHAKEDOWN_WORKER_ENABLED", "false")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.models import (  # noqa: E402,F401
    AgentTrace,
    CaseResult,
    Chunk,
    Endpoint,
    Feature,
    Finding,
    HumanFeedback,
    KnowledgeSource,
    Project,
    Run,
    SpecSnapshot,
    Task,
    TestCase,
)


def _ensure_database() -> None:
    url = engine.url.render_as_string(hide_password=False)
    db_name = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = engine.execution_options(url=admin_url) if False else None
    if admin_engine is not None:  # pragma: no cover
        return
    from sqlalchemy import create_engine

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": db_name}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database() -> None:
    _ensure_database()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    # 测试库是一次性的：重建 schema，保证与模型一致（只允许 _test 结尾的库）
    db_name = engine.url.database or ""
    if db_name.endswith("_test"):
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def session():
    """每个用例一个干净的事务（回滚）。"""
    from app.db import SessionLocal

    db_session = SessionLocal()
    try:
        yield db_session
        db_session.rollback()
    finally:
        db_session.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def sample_project(session):
    project = Project(
        name="测试项目",
        description="pytest 用例",
        base_url="http://127.0.0.1:9",
        isolation={"mode": "prefix"},
        settings_json={},
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project
