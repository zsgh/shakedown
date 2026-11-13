"""视觉基线 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class VisualBaselineCreate(BaseModel):
    case_id: int
    name: str = "default"
    sha256: str | None = None
    screenshot_path: str | None = None


class VisualBaselineCompare(BaseModel):
    case_id: int
    name: str = "default"


class VisualBaselineOut(BaseModel):
    id: int
    project_id: int
    case_id: int
    name: str
    sha256: str
    screenshot_path: str
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
