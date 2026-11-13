"""项目级 RBAC / 租户校验。

说明：目前没有内置登录态，API 的 actor / tenant 通过请求头传入，属于
「部署在可信网关后」的轻量权限模型；MCP token 则是真正的凭据。
两者共用同一套 ``ProjectMember`` 角色规则。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AutomationException,
    CaseResult,
    Finding,
    Project,
    ProjectMember,
    ProjectSchedule,
    Run,
    Task,
    TestCase,
)

ROLE_RANK = {"viewer": 1, "maintainer": 2, "owner": 3}
DEFAULT_ROLE_BY_SCOPE = {"read": "viewer", "write": "maintainer"}
_PROJECT_PATH = re.compile(r"^/api/projects/(\d+)(?:/|$)")
_RESOURCE_PATHS = {
    "case": re.compile(r"^/api/cases/(\d+)(?:/|$)"),
    "run": re.compile(r"^/api/runs/(\d+)(?:/|$)"),
    "finding": re.compile(r"^/api/findings/(\d+)(?:/|$)"),
    "result": re.compile(r"^/api/results/(\d+)(?:/|$)"),
    "task": re.compile(r"^/api/tasks/(\d+)(?:/|$)"),
}


def role_at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(minimum, 99)


def client_actor(client: dict[str, Any] | None) -> str:
    value = client or {}
    return str(value.get("actor") or value.get("name") or "").strip()


def project_role(db: Session, project: Project, client: dict[str, Any] | None) -> str:
    """解析 MCP client 在该项目的角色。

    - 有 ProjectMember 记录：以成员角色为准，停用则拒绝；
    - 显式绑定 actor 但没有成员记录：拒绝（避免成员被删除后回退到 token scope）；
    - 未绑定 actor 且没有成员记录：保留旧行为，按 token scope 推导。
    """
    explicit_actor = str((client or {}).get("actor") or "").strip()
    actor = client_actor(client)
    if actor:
        member = db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.actor == actor,
            )
        )
        if member is not None:
            if not member.active:
                raise PermissionError(f"项目成员已停用：{actor}")
            return member.role
        if explicit_actor:
            raise PermissionError(f"项目成员不存在：{explicit_actor}")
    scopes = set((client or {}).get("scopes") or [])
    if "write" in scopes:
        return DEFAULT_ROLE_BY_SCOPE["write"]
    return DEFAULT_ROLE_BY_SCOPE["read"]


def tenant_allowed(project: Project, client: dict[str, Any] | None) -> bool:
    tenant = str((client or {}).get("tenant") or "").strip()
    if not tenant:
        return True
    return str(project.tenant or "default") == tenant


def require_project_role(
    db: Session,
    project: Project,
    client: dict[str, Any] | None,
    minimum: str = "viewer",
) -> None:
    if not tenant_allowed(project, client):
        raise PermissionError(f"租户不匹配：项目 {project.id} 属于 {project.tenant or 'default'}")
    role = project_role(db, project, client)
    if not role_at_least(role, minimum):
        raise PermissionError(f"需要 {minimum} 角色，当前为 {role}")


def _api_member(db: Session, actor: str, project: Project) -> ProjectMember | None:
    return db.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.actor == actor,
        )
    )


def _project_for_request(db: Session, path: str) -> Project | None:
    match = _PROJECT_PATH.match(path)
    if match:
        return db.get(Project, int(match.group(1)))
    member_match = re.match(r"^/api/members/(\d+)(?:/|$)", path)
    if member_match:
        member = db.get(ProjectMember, int(member_match.group(1)))
        return db.get(Project, member.project_id) if member is not None else None
    exception_match = re.match(r"^/api/exceptions/(\d+)(?:/|$)", path)
    if exception_match:
        exception = db.get(AutomationException, int(exception_match.group(1)))
        return db.get(Project, exception.project_id) if exception is not None else None
    schedule_match = re.match(r"^/api/schedules/(\d+)(?:/|$)", path)
    if schedule_match:
        schedule = db.get(ProjectSchedule, int(schedule_match.group(1)))
        return db.get(Project, schedule.project_id) if schedule is not None else None
    resource_models = {
        "case": TestCase,
        "run": Run,
        "finding": Finding,
        "result": CaseResult,
        "task": Task,
    }
    for kind, pattern in _RESOURCE_PATHS.items():
        match = pattern.match(path)
        if not match:
            continue
        row = db.get(resource_models[kind], int(match.group(1)))
        project_id = getattr(row, "project_id", None) if row is not None else None
        return db.get(Project, project_id) if project_id is not None else None
    return None


def enforce_api_actor(
    db: Session,
    *,
    method: str,
    path: str,
    actor: str = "",
    tenant: str = "",
) -> None:
    """可选的 API 权限头：``X-Shakedown-Actor`` / ``X-Shakedown-Tenant``。

    未提供请求头时保持旧行为（本地/内网部署兼容）。提供后：
    - 项目 tenant 必须匹配；
    - actor 必须是 active 成员；
    - GET 需要 viewer，写操作需要 maintainer，成员管理写操作需要 owner。
    """
    actor = (actor or "").strip()
    tenant = (tenant or "").strip()
    if not actor and not tenant:
        return
    project = _project_for_request(db, path)
    if project is None:
        return
    if tenant and str(project.tenant or "default") != tenant:
        raise PermissionError(f"租户不匹配：项目 {project.id}")
    if not actor:
        return
    member = _api_member(db, actor, project)
    if member is None:
        raise PermissionError(f"{actor} 不是项目 {project.id} 的成员")
    if not member.active:
        raise PermissionError(f"项目成员已停用：{actor}")
    upper = method.upper()
    if upper in {"GET", "HEAD", "OPTIONS"}:
        minimum = "viewer"
    elif "/members" in path or re.match(r"^/api/members/", path):
        minimum = "owner"
    else:
        minimum = "maintainer"
    if not role_at_least(member.role, minimum):
        raise PermissionError(f"需要 {minimum} 角色，当前为 {member.role}")
