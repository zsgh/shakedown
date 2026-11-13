"""全局设置存储：数据库里的可视化配置，覆盖同名的环境变量。

- 读取：DB 值优先，其次环境变量，再其次默认值；
- 写入：只存用户改过的项（存 DB 覆盖值），环境变量作为兜底；
- 变更即时生效：provider 缓存按 settings 版本号失效。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.logging import get_logger
from app.models import AppSetting

logger = get_logger("shakedown.settings")

# 可视化配置支持的键（= 环境变量名去掉 SHAKEDOWN_ 前缀）
MANAGED_KEYS = [
    "llm_provider",
    "llm_base_url",
    "llm_api_key",
    "llm_model",
    "llm_temperature",
    "llm_max_tokens",
    "llm_reasoning_effort",
    "embedding_provider",
    "embedding_base_url",
    "embedding_api_key",
    "embedding_model",
    "sandbox_mode",
    "berth_base_url",
    "berth_token",
    "berth_image",
    "berth_cli_service",
    "worker_enabled",
    "worker_concurrency",
    "cors_origins",
    "encryption_key",
    "github_token",
    "github_repo",
    "github_api_url",
    "github_labels",
    "jira_base_url",
    "jira_email",
    "jira_api_token",
    "jira_project_key",
    "jira_issue_type",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_tls",
    "notification_email",
    "mcp_require_auth",
    "mcp_allowed_hosts",
]

SECRET_KEYS = {
    "llm_api_key",
    "embedding_api_key",
    "berth_token",
    "encryption_key",
    "github_token",
    "jira_api_token",
}

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_at: float = 0.0
_cache_version: str = ""
TTL = 2.0


def _load() -> dict[str, Any]:
    global _cache, _cache_at, _cache_version
    now = time.time()
    with _lock:
        if _cache and now - _cache_at < TTL:
            return _cache
    try:
        with SessionLocal() as session:
            rows = session.scalars(select(AppSetting)).all()
            data = {row.key: row.value for row in rows}
    except Exception as exc:  # noqa: BLE001 —— 数据库不可用时退回环境变量
        logger.warning("读取全局设置失败，改用环境变量：%s", exc)
        data = {}
    version = str(sorted(data.items()))
    with _lock:
        _cache = data
        _cache_at = now
        _cache_version = version
    return data


def version() -> str:
    """设置版本号（用于 provider 缓存失效）。"""
    _load()
    with _lock:
        return _cache_version


def overrides() -> dict[str, Any]:
    """数据库里的覆盖值。"""
    return dict(_load())


def get_value(key: str, default: Any = None) -> Any:
    data = _load()
    if key in data:
        return data[key]
    return default


def set_value(session, key: str, value: Any) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    invalidate()


def set_values(session, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key not in MANAGED_KEYS:
            continue
        # 前端回传的是打码值：表示「没改」，不要覆盖真实密钥
        if key in SECRET_KEYS and value == "********":
            continue
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    invalidate()


def delete_value(session, key: str) -> None:
    row = session.get(AppSetting, key)
    if row is not None:
        session.delete(row)
        invalidate()


def invalidate() -> None:
    global _cache, _cache_at
    with _lock:
        _cache = {}
        _cache_at = 0.0


def effective_settings() -> Settings:
    """环境变量 + 数据库覆盖 = 生效配置。"""
    base = get_settings().model_copy()
    data = _load()
    for key, value in data.items():
        if key not in MANAGED_KEYS:
            continue
        if value is None or value == "":
            continue
        field = key
        if not hasattr(base, field):
            continue
        current = getattr(base, field)
        try:
            if isinstance(current, bool):
                if isinstance(value, str):
                    setattr(base, field, value.strip().lower() in {"1", "true", "yes", "on"})
                else:
                    setattr(base, field, bool(value))
            elif isinstance(current, int) and not isinstance(current, bool):
                setattr(base, field, int(value))
            elif isinstance(current, float):
                setattr(base, field, float(value))
            elif isinstance(current, list):
                if isinstance(value, list):
                    setattr(base, field, value)
                elif isinstance(value, str):
                    setattr(base, field, [item.strip() for item in value.split(",") if item.strip()])
                else:
                    setattr(base, field, [value])
            else:
                setattr(base, field, value)
        except (TypeError, ValueError):
            continue
    return base


def masked(settings: Settings) -> dict[str, Any]:
    """给前端看的生效配置（密钥打码）。"""
    data = settings.model_dump()
    for key in SECRET_KEYS:
        if data.get(key):
            data[key] = "********"
    data["_overrides"] = sorted(overrides().keys())
    return data
