"""自动化例外 / SLA API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.schedule import RunPolicy


class AutomationTriggerRequest(BaseModel):
    policy: RunPolicy | None = None
    trigger: str = "deploy"
    note: str = ""


class ArtifactVerifyRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=200)


class TestDataGenerateRequest(BaseModel):
    endpoint_id: int | None = None
    fields: list[dict[str, Any]] = Field(default_factory=list)
    count: int = Field(default=1, ge=1, le=200)
    seed: int | None = None
    unicode: bool = False
    distribution: str = Field(default="uniform", pattern="^(uniform|normal)$")


class PerformanceRunRequest(BaseModel):
    mode: str = Field(default="baseline", pattern="^(baseline|load|stress|soak)$")
    method: str = "GET"
    path: str
    concurrency: int = Field(default=5, ge=1, le=50)
    iterations: int = Field(default=20, ge=1, le=5000)
    duration_seconds: float | None = Field(default=None, ge=0, le=300)
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    timeout: float = Field(default=10.0, gt=0, le=60)


class ExploratoryRunRequest(BaseModel):
    mode: str = Field(pattern="^(differential|fuzz|metamorphic|property)$")
    method: str = "GET"
    path: str = ""
    variants: list[dict[str, Any]] = Field(default_factory=list)
    payloads: list[Any] = Field(default_factory=list)
    properties: list[dict[str, Any]] = Field(default_factory=list)
    cases: list[dict[str, Any]] = Field(default_factory=list)
    generator: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    read_path: str = ""
    id_field: str = "id"
    compare_fields: list[str] = Field(default_factory=list)
    headers: dict[str, Any] = Field(default_factory=dict)
    allow_write: bool = False


class VerificationCommandRunRequest(BaseModel):
    case_id: int | None = None


class AutomationExceptionOut(BaseModel):
    id: int
    project_id: int
    kind: str
    target_type: str
    target_id: int | None
    severity: str
    status: str
    owner: str
    note: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AutomationExceptionUpdate(BaseModel):
    status: str | None = Field(default=None, description="open / acked / resolved / ignored")
    owner: str | None = None
    note: str | None = None
