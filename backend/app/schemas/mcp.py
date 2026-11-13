"""MCP 客户端管理模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class McpClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    project_ids: list[int] = Field(default_factory=list, description="为空表示不限制项目")
    tenant: str = Field(default="", max_length=64, description="非空时只能访问同租户项目")
    actor: str = Field(default="", max_length=120, description="绑定 ProjectMember.actor；为空用 client name")


class McpClientOut(BaseModel):
    id: int
    name: str
    token_prefix: str
    scopes: list[str]
    project_ids: list[int] = Field(default_factory=list)
    tenant: str = ""
    actor: str = ""
    enabled: bool
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class McpClientCreated(McpClientOut):
    token: str
