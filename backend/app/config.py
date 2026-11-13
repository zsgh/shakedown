"""集中式配置：所有可调项都从环境变量读取（前缀 ``SHAKEDOWN_``）。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent


def sanitize_proxy_env() -> None:
    """移除 NO_PROXY 里的方括号 IPv6 写法（[::1]），httpx 会把它当成非法端口。"""
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parts: list[str] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
            parts.append(item)
        os.environ[key] = ",".join(parts)


sanitize_proxy_env()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SHAKEDOWN_",
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 基础 ──────────────────────────────────────────────
    app_name: str = "Shakedown"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = False
    data_dir: Path = Field(default=REPO_ROOT / "var")
    artifacts_dir: Path = Field(default=REPO_ROOT / "var" / "artifacts")

    # ── 数据库 ────────────────────────────────────────────
    database_url: str = "postgresql+psycopg://shakedown:shakedown@localhost:5432/shakedown"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── LLM provider ─────────────────────────────────────
    llm_provider: Literal["mock", "openai", "skipper"] = "mock"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = "shakedown-mock"
    llm_temperature: float = 0.2
    # 结构化输出可能很长（推理模型还要额外花 reasoning token），默认给足
    llm_max_tokens: int = 32768
    # 推理强度：auto（不传）/ low / medium / high —— 推理模型在批量生成时用 low 能快很多
    llm_reasoning_effort: str = "low"
    llm_timeout: float = 120.0
    llm_max_retries: int = 2
    # 单个生成批次的硬上限：上游模型挂住时不能把整个任务拖死
    llm_job_timeout: int = 360
    # 生成用例时同时发起的模型调用数；上游不稳时可降到 1 提高成功率
    llm_concurrency: int = 4

    # ── 缺陷工单集成 ──────────────────────────────────────
    github_token: str = ""
    github_repo: str = ""  # owner/repo
    github_api_url: str = "https://api.github.com"
    github_labels: str = ""
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    jira_issue_type: str = "Bug"

    # ── MCP HTTP 接入 ─────────────────────────────────────
    mcp_require_auth: bool = True
    mcp_allowed_hosts: str = (
        "127.0.0.1,127.0.0.1:8000,localhost,localhost:8000,testserver"
    )

    # ── Embedding ────────────────────────────────────────
    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # ── 沙箱（berth）──────────────────────────────────────
    sandbox_mode: Literal["berth", "local"] = "local"
    berth_base_url: str = "http://localhost:9000"
    berth_token: str = ""
    berth_image: str = "base"
    # 在 berth 里跑 CLI 用例时，把 limactl shell 转成对服务 namespace 的 exec。
    berth_cli_service: str = ""
    berth_runner_image: str = "runner"
    sandbox_timeout: int = 600
    sandbox_memory_mb: int = 1024
    sandbox_cpu_percent: int = 100

    # ── Worker ───────────────────────────────────────────
    worker_enabled: bool = True
    worker_poll_interval: float = 2.0
    worker_concurrency: int = 2
    worker_lease_seconds: int = 900
    worker_max_attempts: int = 3
    # 无人值守调度器（与 worker 同进程启动；可独立关闭）
    scheduler_enabled: bool = True
    scheduler_poll_interval: float = 10.0
    sla_escalation_enabled: bool = True
    sla_escalation_interval_seconds: float = 3600.0
    digest_sweep_enabled: bool = True
    digest_sweep_interval_seconds: float = 3600.0
    repair_sweep_enabled: bool = True
    repair_sweep_interval_seconds: float = 1800.0
    repair_sweep_limit: int = 5

    # ── 指标 ─────────────────────────────────────────────
    metrics_db_enabled: bool = True
    metrics_flush_after_requests: int = 50
    metrics_flush_interval_seconds: float = 5.0

    # ── 通知（SMTP）────────────────────────────────────────
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    notification_email: str = ""

    # ── 安全 ──────────────────────────────────────────────
    encryption_key: str = ""  # 缺省时自动生成并落到 data_dir
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):  # 支持 "a,b" 形式
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    return settings


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


# 供 alembic / 脚本直接使用
settings = get_settings()

# 兼容旧调用
os.environ.setdefault("SHAKEDOWN_DATA_DIR", str(settings.data_dir))
