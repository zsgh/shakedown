"""攻击用例包的只读上下文：接口、账号、代码根目录。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import decrypt_json
from app.models import Endpoint, KnowledgeSource, Project


@dataclass
class AttackContext:
    project: Project
    endpoints: list[Endpoint]
    accounts: dict[str, dict[str, Any]]
    code_roots: list[Path]
    base_url: str
    # 每个模式扫描时填充的临时数据（例如依赖清单），不落库
    scratch: dict[str, Any] = field(default_factory=dict)

    # ── 接口查找 ──────────────────────────────────────────
    def find(self, method: str, path: str, *, exact: bool = False) -> Endpoint | None:
        want_method = method.upper()
        for endpoint in self.endpoints:
            if endpoint.method.upper() != want_method:
                continue
            candidate = endpoint.path
            if exact and candidate == path:
                return endpoint
            if not exact and candidate.startswith(path):
                return endpoint
        return None

    def find_contains(self, method: str, fragment: str) -> Endpoint | None:
        for endpoint in self.endpoints:
            if endpoint.method.upper() == method.upper() and fragment in endpoint.path:
                return endpoint
        return None

    @property
    def login_endpoint(self) -> Endpoint | None:
        return self.find("POST", "/login", exact=True)

    @property
    def reset_password_endpoint(self) -> Endpoint | None:
        return next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.method.upper() == "POST"
                and endpoint.path.startswith("/admin/users/")
                and endpoint.path.endswith("/password")
            ),
            None,
        )

    @property
    def admin_create_user_endpoint(self) -> Endpoint | None:
        return self.find("POST", "/admin/users", exact=True)

    @property
    def admin_pages(self) -> list[Endpoint]:
        return [
            endpoint
            for endpoint in self.endpoints
            if endpoint.method.upper() == "GET" and endpoint.path in {"/admin", "/admin/backup"}
        ]

    @property
    def admin_download_endpoint(self) -> Endpoint | None:
        return next(
            (
                endpoint
                for endpoint in self.endpoints
                if endpoint.method.upper() == "GET"
                and "/backup/" in endpoint.path
                and endpoint.path.endswith("{name}")
            ),
            None,
        )

    # ── 账号 ──────────────────────────────────────────────
    def account(self, name: str) -> dict[str, Any]:
        return dict(self.accounts.get(name) or {})

    @property
    def has_admin(self) -> bool:
        admin = self.accounts.get("admin") or {}
        return bool(admin.get("token") or admin.get("password"))

    def admin_headers(self) -> dict[str, str]:
        admin = self.account("admin")
        if admin.get("token"):
            return {"PRIVATE-TOKEN": "{{account.admin.token}}"}
        return {}

    def admin_auth(self) -> dict[str, Any]:
        if self.account("admin").get("token"):
            return {}
        return {"mode": "basic", "account": "admin"}

    @property
    def has_low_priv(self) -> bool:
        for name in ("viewer", "alice", "user", "developer"):
            account = self.accounts.get(name) or {}
            if account.get("password") or account.get("token"):
                return True
        return False

    def low_priv_account(self) -> str:
        for name in ("viewer", "alice", "user", "developer"):
            account = self.accounts.get(name) or {}
            if account.get("password") or account.get("token"):
                return name
        return "viewer"

    @property
    def base(self) -> str:
        return (self.base_url or "").rstrip("/")


def build_context(db: Session, project: Project) -> AttackContext:
    endpoints = list(
        db.scalars(select(Endpoint).where(Endpoint.project_id == project.id).order_by(Endpoint.path)).all()
    )
    accounts = decrypt_json(project.test_accounts_encrypted) or {}
    roots: list[Path] = []
    for source in db.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.project_id == project.id,
            KnowledgeSource.kind == "code",
        )
    ).all():
        path = Path(str(source.location)).expanduser()
        if path.exists():
            roots.append(path.resolve())
    return AttackContext(
        project=project,
        endpoints=endpoints,
        accounts={str(k): v for k, v in accounts.items() if isinstance(v, dict)},
        code_roots=roots,
        base_url=project.base_url or "",
    )


__all__ = ["AttackContext", "build_context"]
