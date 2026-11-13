"""项目成员 / 角色矩阵 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MemberRole = Literal["owner", "maintainer", "viewer"]


class ProjectMemberCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    role: MemberRole = "viewer"
    note: str = ""


class ProjectMemberUpdate(BaseModel):
    role: MemberRole | None = None
    active: bool | None = None
    note: str | None = None


class ProjectMemberOut(BaseModel):
    id: int
    project_id: int
    actor: str
    role: str
    active: bool
    note: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
