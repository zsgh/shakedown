"""MCP 客户端管理 API。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import NotFoundError, ValidationError
from app.mcp.tokens import VALID_SCOPES, create_client
from app.models import McpClient
from app.schemas import McpClientCreate, McpClientCreated, McpClientOut

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/clients", response_model=list[McpClientOut])
def list_mcp_clients(db: Session = Depends(get_db)) -> list[McpClient]:
    return list(db.scalars(select(McpClient).order_by(McpClient.id.desc())).all())


@router.post("/clients", response_model=McpClientCreated, status_code=201)
def create_mcp_client(payload: McpClientCreate, db: Session = Depends(get_db)) -> McpClientCreated:
    invalid = [scope for scope in payload.scopes if scope not in VALID_SCOPES]
    if invalid:
        raise ValidationError(f"不支持的 MCP scope：{invalid}；可用 read / write")
    client, raw = create_client(
        db,
        payload.name,
        payload.scopes,
        payload.project_ids,
        tenant=payload.tenant,
        actor=payload.actor,
    )
    db.commit()
    db.refresh(client)
    return McpClientCreated(
        id=client.id,
        name=client.name,
        token_prefix=client.token_prefix,
        scopes=list(client.scopes or []),
        project_ids=list(client.project_ids or []),
        tenant=client.tenant,
        actor=client.actor,
        enabled=client.enabled,
        last_used_at=client.last_used_at,
        created_at=client.created_at,
        token=raw,
    )


@router.delete("/clients/{client_id}")
def delete_mcp_client(client_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    client = db.get(McpClient, client_id)
    if client is None:
        raise NotFoundError(f"MCP 客户端不存在：{client_id}")
    db.delete(client)
    db.commit()
    return {"id": client_id, "deleted": True}
