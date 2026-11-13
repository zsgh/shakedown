"""MCP 客户端令牌：生成、哈希存储和校验。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import McpClient

VALID_SCOPES = {"read", "write"}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return "mcp_" + secrets.token_urlsafe(32)


def create_client(
    session: Session,
    name: str,
    scopes: list[str],
    project_ids: list[int] | None = None,
    *,
    tenant: str = "",
    actor: str = "",
) -> tuple[McpClient, str]:
    selected = [scope for scope in ("read", "write") if scope in set(scopes)]
    if "write" in selected and "read" not in selected:
        selected.insert(0, "read")
    if not selected:
        selected = ["read"]
    raw = generate_token()
    client = McpClient(
        name=name.strip()[:120] or "mcp-client",
        token_hash=hash_token(raw),
        token_prefix=raw[:16],
        scopes=selected,
        project_ids=[int(item) for item in (project_ids or [])],
        tenant=str(tenant or "").strip()[:64],
        actor=str(actor or "").strip()[:120],
    )
    session.add(client)
    session.flush()
    return client, raw


def authenticate_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with SessionLocal() as session:
        client = session.scalar(
            select(McpClient).where(
                McpClient.token_hash == hash_token(token),
                McpClient.enabled.is_(True),
            )
        )
        if client is None:
            return None
        client.last_used_at = datetime.now(timezone.utc)
        session.commit()
        return {
            "id": client.id,
            "name": client.name,
            "scopes": list(client.scopes or []),
            "project_ids": list(client.project_ids or []),
            "tenant": client.tenant or "",
            "actor": client.actor or "",
        }
