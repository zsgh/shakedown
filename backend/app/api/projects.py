"""项目 CRUD。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.crypto import decrypt_json, encrypt, encrypt_json
from app.db import get_db
from app.errors import AgentError, NotFoundError
from app.models import Endpoint, Feature, Finding, KnowledgeSource, Project, ProjectMember, Run, Task, TestCase
from app.schemas import OnboardingRequest, ProjectCreate, ProjectOut, ProjectSummary, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


def _mask_accounts(accounts: dict[str, Any]) -> dict[str, Any]:
    """测试账号里的密码 / token 打码（不回显明文）。"""
    masked: dict[str, Any] = {}
    for name, account in (accounts or {}).items():
        if not isinstance(account, dict):
            masked[name] = account
            continue
        item = dict(account)
        for key in ("password", "token", "secret"):
            if item.get(key):
                item[key] = "********"
        masked[name] = item
    return masked


def _to_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        tenant=project.tenant or "default",
        base_url=project.base_url,
        target_start=project.target_start or {},
        isolation=project.isolation or {},
        test_accounts=_mask_accounts(decrypt_json(project.test_accounts_encrypted) or {}),
        target_dsn_configured=bool(project.target_dsn_encrypted),
        target_read_dsn_configured=bool(project.target_read_dsn_encrypted),
        settings=project.settings_json or {},
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("", response_model=list[ProjectSummary])
def list_projects(request: Request, db: Session = Depends(get_db)) -> list[ProjectSummary]:
    stmt = select(Project).order_by(Project.id.desc())
    tenant = request.headers.get("X-Shakedown-Tenant", "").strip()
    if tenant:
        stmt = stmt.where(Project.tenant == tenant)
    actor = request.headers.get("X-Shakedown-Actor", "").strip()
    if actor:
        stmt = stmt.where(
            Project.id.in_(
                select(ProjectMember.project_id).where(
                    ProjectMember.actor == actor,
                    ProjectMember.active.is_(True),
                )
            )
        )
    projects = db.scalars(stmt).all()
    summaries: list[ProjectSummary] = []
    for project in projects:
        from app.api.cases import coverage_tree

        try:
            coverage = coverage_tree(project.id, db)
            coverage_rate = coverage["coverage_rate"]
        except Exception:  # noqa: BLE001 - 项目数据不完整时首页仍要能打开
            coverage_rate = 0.0
        last_run = db.scalar(
            select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(1)
        )
        summaries.append(
            ProjectSummary(
                id=project.id,
                name=project.name,
                description=project.description,
                tenant=project.tenant or "default",
                base_url=project.base_url,
                case_count=db.scalar(select(func.count()).select_from(TestCase).where(TestCase.project_id == project.id)) or 0,
                enabled_case_count=db.scalar(
                    select(func.count())
                    .select_from(TestCase)
                    .where(TestCase.project_id == project.id, TestCase.status == "enabled")
                )
                or 0,
                feature_count=db.scalar(select(func.count()).select_from(Feature).where(Feature.project_id == project.id)) or 0,
                endpoint_count=db.scalar(select(func.count()).select_from(Endpoint).where(Endpoint.project_id == project.id)) or 0,
                source_count=db.scalar(
                    select(func.count()).select_from(KnowledgeSource).where(KnowledgeSource.project_id == project.id)
                )
                or 0,
                pending_review=db.scalar(
                    select(func.count())
                    .select_from(TestCase)
                    .where(
                        TestCase.project_id == project.id,
                        TestCase.status.in_(["pending", "needs_review"]),
                    )
                )
                or 0,
                findings_pending=db.scalar(
                    select(func.count())
                    .select_from(Finding)
                    .where(Finding.project_id == project.id, Finding.status == "pending")
                )
                or 0,
                coverage_rate=coverage_rate,
                last_run_status=last_run.status if last_run else None,
                last_run_at=last_run.created_at if last_run else None,
                last_run_pass_rate=(last_run.stats or {}).get("pass_rate") if last_run else None,
            )
        )
    return summaries


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    project = Project(
        name=payload.name,
        description=payload.description,
        tenant=payload.tenant or "default",
        base_url=payload.base_url,
        target_start=payload.target_start.model_dump(),
        isolation=payload.isolation.model_dump(),
        test_accounts_encrypted=encrypt_json(payload.test_accounts) if payload.test_accounts else None,
        target_dsn_encrypted=encrypt(payload.target_dsn) if payload.target_dsn else None,
        target_read_dsn_encrypted=encrypt(payload.target_read_dsn) if payload.target_read_dsn else None,
        settings_json=payload.settings,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _to_out(project)


@router.get("/{project_id}/accounts")
def list_project_accounts(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """账号矩阵：只返回账号名、字段名和打码值，不回显密码/token。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    accounts = decrypt_json(project.test_accounts_encrypted) or {}
    masked = _mask_accounts(accounts)
    return {
        "project_id": project.id,
        "roles": sorted(accounts.keys()),
        "accounts": [
            {"name": name, "fields": sorted(value.keys()) if isinstance(value, dict) else [], "values": value}
            for name, value in masked.items()
        ],
    }


@router.post("/{project_id}/accounts/validate")
def validate_project_accounts(
    project_id: int,
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """预检账号：token 是否有效 / 用户名密码能否登录，不修改任何账号。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    from app.account_service import preflight_accounts

    names = payload.get("names") if isinstance(payload, dict) else None
    return preflight_accounts(project, names=names if isinstance(names, list) else None)


@router.post("/{project_id}/onboarding")
def onboarding(project_id: int, payload: OnboardingRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Agent 原生 onboarding：只给地址 + admin 凭据，自己发现 profile 并 bootstrap 账号。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    from app.onboarding import run_onboarding

    try:
        return run_onboarding(
            db,
            project,
            admin_username=payload.admin_username,
            admin_password=payload.admin_password,
            dry_run=payload.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        raise AgentError(f"onboarding 失败：{type(exc).__name__}: {exc}") from exc


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return _to_out(project)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    data = payload.model_dump(exclude_unset=True)
    if "target_start" in data and data["target_start"] is not None:
        project.target_start = data["target_start"]
    if "isolation" in data and data["isolation"] is not None:
        project.isolation = data["isolation"]
    if "test_accounts" in data and data["test_accounts"] is not None:
        stored = decrypt_json(project.test_accounts_encrypted) or {}
        accounts: dict[str, Any] = {}
        for name, account in (data["test_accounts"] or {}).items():
            if not isinstance(account, dict):
                accounts[name] = account
                continue
            merged = dict(stored.get(name) or {})
            for key, value in account.items():
                # 打码占位符表示「没改」→ 保留原值
                if key in ("password", "token", "secret") and value == "********":
                    continue
                merged[key] = value
            accounts[name] = merged
        project.test_accounts_encrypted = encrypt_json(accounts) if accounts else None
    if "target_dsn" in data and data["target_dsn"] is not None:
        project.target_dsn_encrypted = encrypt(data["target_dsn"]) if data["target_dsn"] else None
    if "target_read_dsn" in data and data["target_read_dsn"] is not None:
        project.target_read_dsn_encrypted = (
            encrypt(data["target_read_dsn"]) if data["target_read_dsn"] else None
        )
    for key in ("name", "description", "tenant", "base_url"):
        if key in data and data[key] is not None:
            setattr(project, key, data[key])
    if "settings" in data and data["settings"] is not None:
        # ORM 属性名是 settings_json（列名才叫 settings）
        project.settings_json = data["settings"]
    db.commit()
    db.refresh(project)
    return _to_out(project)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: int, db: Session = Depends(get_db)) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    from app.test_data_service import delete_project_files

    delete_project_files(project)
    db.delete(project)
    db.commit()


@router.get("/{project_id}/overview")
def project_overview(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """项目总览：覆盖率、各维度用例数与通过率、最近执行趋势。"""
    from app.api.reports import build_overview

    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return build_overview(db, project)


@router.get("/{project_id}/sources", response_model=list[dict[str, Any]])
def project_sources(project_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    sources = db.scalars(select(KnowledgeSource).where(KnowledgeSource.project_id == project_id)).all()
    return [
        {
            "id": s.id,
            "kind": s.kind,
            "name": s.name,
            "location": s.location,
            "status": s.status,
            "version": s.version,
            "meta": s.meta_json,
        }
        for s in sources
    ]


@router.get("/{project_id}/health")
def project_health(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """项目健康检查：被测环境是否可达、worker 是否有积压、知识源是否就绪。"""
    import time

    import httpx

    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")

    target: dict[str, Any] = {"ok": None, "url": project.base_url, "latency_ms": None, "error": None}
    if project.base_url:
        started = time.perf_counter()
        try:
            response = httpx.get(project.base_url, timeout=2.5, follow_redirects=True)
            target = {
                "ok": response.status_code < 500,
                "url": project.base_url,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": None if response.status_code < 500 else f"HTTP {response.status_code}",
            }
        except Exception as exc:  # noqa: BLE001 - 健康检查失败原因需要原样展示
            target = {
                "ok": False,
                "url": project.base_url,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(exc)[:300],
            }

    active_tasks = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.project_id == project_id, Task.status.in_(["queued", "running"]))
    ) or 0
    sources = db.scalars(select(KnowledgeSource).where(KnowledgeSource.project_id == project_id)).all()
    return {
        "project_id": project_id,
        "target": target,
        "worker": {"active_tasks": active_tasks},
        "knowledge": {
            "sources": len(sources),
            "failed": sum(1 for s in sources if s.status == "failed"),
            "parsing": sum(1 for s in sources if s.status in {"pending", "parsing"}),
        },
    }


@router.get("/{project_id}/search-all")
def search_all(
    project_id: int,
    q: str = Query(min_length=1),
    limit: int = Query(default=6, ge=0, le=20),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """命令面板 / 顶栏搜索：跨用例、缺陷、接口。"""
    pattern = f"%{q}%"
    cases = db.scalars(
        select(TestCase)
        .where(TestCase.project_id == project_id, TestCase.title.ilike(pattern))
        .order_by(TestCase.updated_at.desc())
        .limit(limit)
    ).all()
    findings = db.scalars(
        select(Finding)
        .where(Finding.project_id == project_id, Finding.title.ilike(pattern))
        .order_by(Finding.created_at.desc())
        .limit(limit)
    ).all()
    endpoints = db.scalars(
        select(Endpoint)
        .where(Endpoint.project_id == project_id, Endpoint.path.ilike(pattern))
        .order_by(Endpoint.path)
        .limit(limit)
    ).all()
    return {
        "cases": [
            {"id": c.id, "title": c.title, "status": c.status, "dimension": c.dimension}
            for c in cases
        ],
        "findings": [
            {"id": f.id, "title": f.title, "status": f.status, "severity": f.severity}
            for f in findings
        ],
        "endpoints": [
            {"id": e.id, "method": e.method, "path": e.path, "summary": e.summary}
            for e in endpoints
        ],
    }


@router.get("/{project_id}/settings")
def project_settings(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return {
        "id": project.id,
        "settings": project.settings_json or {},
        "isolation": project.isolation or {},
    }


@router.get("/{project_id}/search")
def search_knowledge(
    project_id: int,
    q: str = Query(min_length=1),
    limit: int = Query(default=10, ge=0, le=50),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """知识检索（pgvector 语义检索 + 关键词兜底）。"""
    from sqlalchemy import select

    from app.ingest.embeddings import Embedder
    from app.models import Chunk

    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    embedder = Embedder()
    vector = embedder.embed(q)
    chunks = db.scalars(
        select(Chunk)
        .where(Chunk.project_id == project_id)
        .order_by(Chunk.embedding.cosine_distance(vector))
        .limit(limit)
    ).all()
    return [
        {
            "id": c.id,
            "content": c.content[:1200],
            "locator": c.locator,
            "distance": float(c.embedding.cosine_distance(vector)),
        }
        for c in chunks
    ]
