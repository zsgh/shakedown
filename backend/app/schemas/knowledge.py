"""知识源 / 接口 / 功能点 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class KnowledgeSourceCreate(BaseModel):
    kind: str = Field(description="code / markdown / openapi")
    name: str = ""
    location: str = Field(description="本地路径或 URL")
    # markdown 专用：目录则递归导入
    recursive: bool = True
    include_globs: list[str] = Field(default_factory=list)
    # spec=接口规范（可作断言依据）；notes=设计笔记/开发日志（只用于理解设计）
    role: str = Field(default="", description="留空则自动判定")


class KnowledgeSourceOut(BaseModel):
    id: int
    project_id: int
    kind: str
    role: str = "spec"
    name: str
    location: str
    version: str
    status: str
    error: str | None = None
    meta: dict[str, Any]
    reevaluate_candidates: int = 0
    parsed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class KnowledgeSourceUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    role: str | None = None


class ChunkOut(BaseModel):
    id: int
    source_id: int
    content: str
    locator: dict[str, Any]
    meta: dict[str, Any]
    content_hash: str

    model_config = {"from_attributes": True}


class EndpointOut(BaseModel):
    id: int
    project_id: int
    method: str
    path: str
    summary: str
    params: dict[str, Any]
    request_body: dict[str, Any]
    responses: dict[str, Any]
    security: dict[str, Any]
    examples: dict[str, Any]
    sources: list[dict[str, Any]]
    consistency: dict[str, Any]

    model_config = {"from_attributes": True}


class EndpointMerge(BaseModel):
    """代码 / 文档侧抽取到的接口候选，合并进清单。"""

    method: str
    path: str
    summary: str = ""
    source_type: str = "code"
    ref: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    request_body: dict[str, Any] = Field(default_factory=dict)
    responses: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)


class ConsistencyReport(BaseModel):
    state: str
    checked_at: str = ""
    mismatches: list[dict[str, Any]] = Field(default_factory=list)


class FeatureOut(BaseModel):
    id: int
    project_id: int
    name: str
    module: str
    description: str
    source_refs: list[dict[str, Any]]
    endpoint_ids: list[int]
    unmapped: bool

    model_config = {"from_attributes": True}


class FeatureCreate(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    module: str = ""
    description: str = ""
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    endpoint_ids: list[int] = Field(default_factory=list)


class FeatureUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    module: str | None = None
    description: str | None = None
    source_refs: list[dict[str, Any]] | None = None
    endpoint_ids: list[int] | None = None
