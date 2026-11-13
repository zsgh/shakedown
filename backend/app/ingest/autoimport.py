"""仓库一键自动导入：给一个目录，把能用的都导进来。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.enums import KnowledgeKind, ParseStatus
from app.ingest.discovery import discover_repository
from app.ingest.service import ingest_source
from app.logging import get_logger
from app.models import KnowledgeSource, Project

logger = get_logger("shakedown.ingest.autoimport")


def select_source(session: Session, project_id: int, kind: str, location: str) -> KnowledgeSource | None:
    from sqlalchemy import select

    return session.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project_id,
            KnowledgeSource.kind == kind,
            KnowledgeSource.location == location,
        )
    )


def auto_import(
    session: Session,
    project: Project,
    path: str,
    *,
    kinds: list[str] | None = None,
    parse: bool = True,
) -> dict[str, Any]:
    """扫描仓库 → 建知识源 → （可选）解析。

    返回扫描结果 + 每个知识源的结果，用户能一眼看到「导了什么、出了什么错」。
    """
    discovery = discover_repository(path)
    kinds = kinds or [KnowledgeKind.CODE.value, KnowledgeKind.MARKDOWN.value, KnowledgeKind.OPENAPI.value]
    root = discovery.path

    plans: list[tuple[str, str, str]] = []  # (kind, location, name)
    if KnowledgeKind.CODE.value in kinds and discovery.code_files:
        plans.append((KnowledgeKind.CODE.value, root, "代码库（自动发现）"))
    if KnowledgeKind.MARKDOWN.value in kinds and discovery.markdown_files:
        plans.append((KnowledgeKind.MARKDOWN.value, root, "Markdown 文档（自动发现）"))
    if KnowledgeKind.OPENAPI.value in kinds:
        for spec in discovery.openapi_files:
            from pathlib import Path

            plans.append((KnowledgeKind.OPENAPI.value, str(Path(root) / spec), f"OpenAPI（自动发现）：{spec}"))

    created: list[dict[str, Any]] = []
    for kind, location, name in plans:
        source = select_source(session, project.id, kind, location)
        created_new = source is None
        if source is None:
            source = KnowledgeSource(
                project_id=project.id,
                kind=kind,
                name=name,
                location=location,
                status=ParseStatus.PENDING.value,
                meta_json={"auto_import": True},
            )
            session.add(source)
            session.flush()
        item: dict[str, Any] = {
            "id": source.id,
            "kind": kind,
            "name": name,
            "location": location,
            "new": created_new,
            "status": source.status,
        }
        if parse:
            try:
                meta = ingest_source(session, project, source)
                item["status"] = source.status
                item["meta"] = {k: v for k, v in meta.items() if k != "route_list"}
            except Exception as exc:  # noqa: BLE001
                item["status"] = source.status
                item["error"] = str(exc)[:500]
        created.append(item)

    session.commit()
    return {
        "discovery": discovery.as_dict(),
        "sources": created,
        "summary": {
            "sources": len(created),
            "failed": sum(1 for item in created if item.get("status") == ParseStatus.ERROR.value),
        },
    }
