"""系统设置 API：可视化配置全局参数（LLM、沙箱、worker…）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import settings_store
from app.agent.providers import get_provider, reset_provider_cache
from app.db import get_db
from app.errors import ValidationError
from app.sandbox import get_sandbox

router = APIRouter(prefix="/settings", tags=["settings"])

DESCRIPTIONS = {
    "llm_provider": "LLM 提供方：mock（离线规则版）/ openai（OpenAI 兼容）/ skipper",
    "llm_base_url": "OpenAI 兼容接口地址，如 http://localhost:11434/v1",
    "llm_api_key": "模型服务密钥",
    "llm_model": "模型名",
    "llm_temperature": "采样温度",
    "llm_max_tokens": "单次最大 token",
    "llm_reasoning_effort": "推理强度：auto / low / medium / high（推理模型批量生成时用 low）",
    "embedding_provider": "向量提供方：hash（离线）/ openai",
    "embedding_base_url": "embedding 接口地址",
    "embedding_api_key": "embedding 密钥",
    "embedding_model": "embedding 模型名",
    "sandbox_mode": "沙箱模式：local（本地）/ berth（沙箱）",
    "berth_base_url": "berth 服务地址",
    "berth_token": "berth API 鉴权 token",
    "worker_enabled": "是否启动后台 worker",
    "worker_concurrency": "worker 并发数",
    "cors_origins": "允许的前端来源（逗号分隔）",
    "encryption_key": "敏感配置加密密钥（改后需重新录入账号）",
    "github_token": "GitHub Personal Access Token（需要 repo / issues 权限）",
    "github_repo": "全局默认 GitHub 仓库；项目设置留空时使用",
    "github_api_url": "GitHub API 地址（GitHub Enterprise 可改）",
    "github_labels": "全局默认 Issue 标签；项目设置留空时使用",
    "jira_base_url": "Jira 地址，例如 https://your-team.atlassian.net",
    "jira_email": "Jira 登录邮箱（Cloud API Token 方式）",
    "jira_api_token": "Jira API Token",
    "jira_project_key": "全局默认 Jira 项目 Key；项目设置留空时使用",
    "jira_issue_type": "全局默认 Jira 工单类型；项目设置留空时使用",
    "mcp_require_auth": "MCP HTTP 是否要求 Bearer Token（建议保持 true）",
    "mcp_allowed_hosts": "MCP HTTP 允许的 Host，逗号分隔；反向代理域名也要加进来",
}


@router.get("")
def read_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    """当前生效的配置（密钥打码）+ 可配置项说明。"""
    effective = settings_store.effective_settings()
    return {
        "effective": settings_store.masked(effective),
        "overrides": settings_store.overrides(),
        "schema": [
            {"key": key, "label": key, "description": DESCRIPTIONS.get(key, "")}
            for key in settings_store.MANAGED_KEYS
        ],
    }


@router.put("")
def write_settings(payload: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    """保存配置（覆盖环境变量）。传 null 表示恢复环境变量的值。"""
    values = payload.get("values") or {}
    from app.config import Settings

    known_fields = set(Settings.model_fields)
    unknown = [
        key
        for key in values
        if key not in settings_store.MANAGED_KEYS
        and not key.startswith("_")
        and key not in known_fields
    ]
    if unknown:
        raise ValidationError(f"不支持的配置项：{unknown}")
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if key not in settings_store.MANAGED_KEYS:
            continue
        if value == "" or value is None:
            cleaned[key] = None
            continue
        if key in {"llm_max_tokens", "worker_concurrency"}:
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{key} 需要是整数") from exc
        if key == "llm_temperature":
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("llm_temperature 需要是数字") from exc
        if key == "llm_temperature" and not 0 <= float(value) <= 2:
            raise ValidationError("llm_temperature 需要在 0 ~ 2 之间")
        if key == "cors_origins" and isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        cleaned[key] = value

    settings_store.set_values(db, cleaned)
    db.commit()
    reset_provider_cache()
    effective = settings_store.effective_settings()
    return {
        "ok": True,
        "effective": settings_store.masked(effective),
        "overrides": settings_store.overrides(),
    }


@router.get("/probe")
def probe() -> dict[str, Any]:
    """试一下当前配置能不能连上（LLM / 沙箱）。"""
    provider = get_provider()
    sandbox = get_sandbox()
    return {"llm": provider.health(), "sandbox": sandbox.health()}


@router.post("/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, Any]:
    """清空所有覆盖，回到环境变量 / 默认值。"""
    for key in list(settings_store.overrides().keys()):
        settings_store.delete_value(db, key)
    db.commit()
    reset_provider_cache()
    return {"ok": True, "effective": settings_store.masked(settings_store.effective_settings())}
