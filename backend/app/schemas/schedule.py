"""无人值守调度 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunPolicy(BaseModel):
    mode: Literal["full", "smoke", "impacted"] = "full"
    dimensions: list[str] = Field(default_factory=list)
    max_cases: int = Field(default=200, ge=1, le=500)
    priority_max: int | None = None
    smoke_priority: int = 50
    smoke_tags: list[str] = Field(default_factory=lambda: ["smoke"])
    smoke_fallback_limit: int = 20
    tags: list[str] = Field(default_factory=list)


class ProjectScheduleCreate(BaseModel):
    name: str = ""
    enabled: bool = True
    interval_seconds: int = Field(default=86400, ge=60, le=30 * 86400)
    next_run_at: datetime | None = None
    policy: RunPolicy = Field(default_factory=RunPolicy)


class ProjectScheduleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=30 * 86400)
    next_run_at: datetime | None = None
    policy: RunPolicy | None = None


class ProjectScheduleOut(BaseModel):
    id: int
    project_id: int
    name: str
    enabled: bool
    interval_seconds: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_id: int | None
    last_status: str
    last_error: str
    policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
