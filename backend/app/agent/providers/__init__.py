"""provider 注册表：按配置创建 LLM 实现。

配置来源优先级：数据库里的可视化配置 > 环境变量 > 默认值。
改完设置即时生效（按设置版本号让缓存失效），不用重启服务。
"""
from __future__ import annotations

from app.agent.providers.base import LLMError, LLMProvider
from app.agent.providers.mock import MockProvider
from app.agent.providers.openai_compat import OpenAICompatProvider
from app.config import Settings

_cache: dict[str, object] = {}


def build_provider(settings: Settings | None = None) -> LLMProvider:
    if settings is None:
        from app import settings_store

        settings = settings_store.effective_settings()
    provider = settings.llm_provider
    if provider == "mock":
        return MockProvider(model=settings.llm_model)
    if provider == "openai":
        return OpenAICompatProvider(settings)
    if provider == "skipper":
        return _build_skipper_provider(settings)
    raise LLMError(f"未知的 LLM provider：{provider}")


def _build_skipper_provider(settings: Settings) -> LLMProvider:
    """复用 skipper 既有实现的 LLM 配置（接口待与 skipper 侧确认）。

    skipper 的 `create_llm_client()` 基于 OpenAI 兼容接口，配置项来自 skipper 的
    Settings（LLM_API_KEY / LLM_BASE_URL）。这里做「有 skipper 就用 skipper 的配置，
    没有就退回 Shakedown 自己的配置」的保守实现。
    """
    try:  # pragma: no cover - 取决于本机是否装了 skipper
        from skipper.config import get_settings as skipper_settings  # type: ignore
    except Exception:  # noqa: BLE001
        return OpenAICompatProvider(settings)

    try:  # pragma: no cover
        sk = skipper_settings()
        base_url = getattr(sk, "llm_base_url", "") or settings.llm_base_url
        api_key = getattr(sk, "llm_api_key", "") or settings.llm_api_key
        model = getattr(sk, "llm_model", "") or settings.llm_model
        merged = settings.model_copy(update={"llm_base_url": base_url, "llm_api_key": api_key, "llm_model": model})
        return OpenAICompatProvider(merged)
    except Exception:  # noqa: BLE001
        return OpenAICompatProvider(settings)


def get_provider() -> LLMProvider:
    """取当前 provider（带缓存；设置变了自动重建）。"""
    from app import settings_store

    version = settings_store.version()
    cached = _cache.get("provider")
    if cached is not None and _cache.get("version") == version:
        return cached  # type: ignore[return-value]
    provider = build_provider()
    _cache["provider"] = provider
    _cache["version"] = version
    return provider


def reset_provider_cache() -> None:
    _cache.clear()
