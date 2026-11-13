"""MCP 请求级项目访问控制：项目 allowlist + 租户 + 角色。"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from app.db import SessionLocal
from app.models import Project
from app.rbac import project_role, require_project_role, tenant_allowed

_current_client: ContextVar[dict[str, Any] | None] = ContextVar("mcp_current_client", default=None)


def set_current_client(client: dict[str, Any] | None) -> Token:
    return _current_client.set(client)


def reset_current_client(token: Token) -> None:
    _current_client.reset(token)


def current_client() -> dict[str, Any] | None:
    return _current_client.get()


def allowed_project_ids(client: dict[str, Any] | None = None) -> set[int]:
    value = (client or current_client() or {}).get("project_ids") or []
    result: set[int] = set()
    for item in value:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


def require_project_access(
    project_id: int,
    client: dict[str, Any] | None = None,
    *,
    minimum_role: str = "viewer",
) -> None:
    effective = client or current_client()
    allowed = allowed_project_ids(effective)
    if allowed and int(project_id) not in allowed:
        raise PermissionError(f"MCP client 无权访问项目 {project_id}")
    if effective is None:
        # stdio 模式没有 HTTP token，按文档视为本机受信任进程，拥有完整权限。
        return
    with SessionLocal() as session:
        project = session.get(Project, int(project_id))
        if project is None:
            # 访问控制只负责授权；项目不存在交给后续业务查询报错，保持旧语义。
            return
        require_project_role(session, project, effective, minimum_role)


__all__ = [
    "allowed_project_ids",
    "client_role",
    "current_client",
    "project_role",
    "require_project_access",
    "reset_current_client",
    "set_current_client",
    "tenant_allowed",
]


def client_role(project_id: int, client: dict[str, Any] | None = None) -> str:
    with SessionLocal() as session:
        project = session.get(Project, int(project_id))
        if project is None:
            raise PermissionError(f"项目不存在：{project_id}")
        return project_role(session, project, client or current_client())
