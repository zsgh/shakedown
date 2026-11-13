"""测试数据资产 / 绑定 / 文件 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TestDatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    kind: Literal["generated", "schema", "manual"] = "generated"
    # 直接给字段定义；也可以只给 endpoint_id 让平台从 request_body 推断。
    fields: list[dict[str, Any]] = Field(default_factory=list)
    definition: dict[str, Any] = Field(default_factory=dict, alias="schema")
    endpoint_id: int | None = None
    seed: int = 42
    count: int = Field(default=10, ge=1, le=1000)
    unicode: bool = False
    distribution: Literal["uniform", "normal"] = "uniform"
    negative: bool = False
    generate: bool = True
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    status: str = "active"


class TestDatasetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    fields: list[dict[str, Any]] | None = None
    definition: dict[str, Any] | None = Field(default=None, alias="schema")
    seed: int | None = None
    count: int | None = Field(default=None, ge=1, le=1000)
    unicode: bool | None = None
    distribution: Literal["uniform", "normal"] | None = None
    negative: bool | None = None
    status: str | None = None
    regenerate: bool = False


class TestDataRowOut(BaseModel):
    id: int
    row_index: int
    data: dict[str, Any]


class TestDatasetOut(BaseModel):
    id: int
    project_id: int
    name: str
    description: str
    kind: str
    definition: dict[str, Any] = Field(alias="schema")
    seed: int
    version: int
    status: str
    row_count: int = 0
    pii_fields: list[str] = Field(default_factory=list)
    rows: list[TestDataRowOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CaseDataBindingCreate(BaseModel):
    dataset_id: int
    mode: Literal["single", "iterate"] = "single"
    row_index: int | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class CaseDataBindingOut(BaseModel):
    id: int
    project_id: int
    case_id: int
    dataset_id: int
    dataset_name: str = ""
    mode: str
    row_index: int | None
    overrides: dict[str, Any]
    enabled: bool
    dataset_row_count: int = 0
    created_at: datetime


class TestFileOut(BaseModel):
    id: int
    project_id: int
    name: str
    filename: str
    content_type: str
    size: int
    sha256: str
    created_at: datetime


class TestDataHealthOut(BaseModel):
    project_id: int
    dataset_count: int
    row_count: int
    binding_count: int
    file_count: int
    datasets_without_rows: int
    cases_without_data: int
    orphan_rows: int
    orphan_files: int = 0
    stale_bindings: int = 0
    pii_dataset_count: int
