"""项目相关 API 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IsolationConfig(BaseModel):
    """项目隔离配置：每个项目独立的被测库与初始化脚本。"""

    mode: str = Field("reset", description="reset=整库重置；prefix=run_id 前缀数据 + 事后清理")
    db_service: str = Field("", description="berth 服务名（名字即身份，天然按项目隔离）")
    db_port: int | None = None
    db_name: str = ""
    db_template: str = Field("postgres", description="berth 服务模板，如 postgres / mysql")
    overrides: dict[str, Any] = Field(default_factory=dict)
    per_run: bool = Field(False, description="reset 模式是否为每次 run 创建独立数据库服务")
    init_script: str = Field("", description="初始化 SQL / 迁移脚本路径")
    allowed_hosts: list[str] = Field(default_factory=list, description="沙箱网络白名单")


class TargetStart(BaseModel):
    """被测系统启动方式 / 数据准备命令。"""

    type: str = Field("external", description="external / docker / command")
    image: str = ""
    command: list[str] = Field(default_factory=list)
    workdir: str = ""
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    health_url: str = ""
    notes: str = ""
    allow_execute: bool = Field(False, description="允许平台执行 start/stop/seed 等配置命令")
    start_command: str | list[str] = ""
    stop_command: str | list[str] = ""
    migrate_command: str | list[str] = ""
    seed_command: str | list[str] = ""
    account_command: str | list[str] = ""
    cleanup_command: str | list[str] = ""
    wait_seconds: float = 30.0
    timeout: float = 60.0
    auto_start: bool = False
    auto_stop: bool = False


class ProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    tenant: str = Field(default="default", max_length=64, description="租户隔离标识")
    base_url: str = ""
    target_start: TargetStart = Field(default_factory=TargetStart)
    isolation: IsolationConfig = Field(default_factory=IsolationConfig)
    test_accounts: dict[str, Any] = Field(default_factory=dict)
    target_dsn: str = ""
    target_read_dsn: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectCreate(ProjectBase):
    pass


class OnboardingRequest(BaseModel):
    admin_username: str = ""
    admin_password: str = ""
    dry_run: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    tenant: str | None = Field(default=None, max_length=64)
    base_url: str | None = None
    target_start: TargetStart | None = None
    isolation: IsolationConfig | None = None
    test_accounts: dict[str, Any] | None = None
    target_dsn: str | None = None
    target_read_dsn: str | None = None
    settings: dict[str, Any] | None = None


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    tenant: str = "default"
    base_url: str
    target_start: dict[str, Any]
    isolation: dict[str, Any]
    test_accounts: dict[str, Any] = Field(default_factory=dict)
    target_dsn_configured: bool = False
    target_read_dsn_configured: bool = False
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectSummary(BaseModel):
    id: int
    name: str
    description: str
    tenant: str = "default"
    base_url: str
    case_count: int = 0
    enabled_case_count: int = 0
    feature_count: int = 0
    endpoint_count: int = 0
    source_count: int = 0
    pending_review: int = 0
    findings_pending: int = 0
    coverage_rate: float = 0.0
    last_run_status: str | None = None
    last_run_at: datetime | None = None
    last_run_pass_rate: float | None = None
