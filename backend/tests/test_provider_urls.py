"""OpenAI 兼容 provider 的 URL / 请求头兼容性。"""
from __future__ import annotations

import os

from app.agent.providers.openai_compat import OpenAICompatProvider
from app.config import Settings, sanitize_proxy_env


def test_full_chat_endpoint_is_not_doubled():
    provider = OpenAICompatProvider(
        Settings(
            llm_base_url="https://opencode.ai/inference/go/openai/v1/chat/completions",
            llm_api_key="sk-test",
            llm_model="deepseek-v4.1-flash",
        )
    )
    assert provider.chat_url == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert provider.models_url == "https://opencode.ai/inference/go/v1/models"


def test_base_url_is_normalized():
    provider = OpenAICompatProvider(
        Settings(llm_base_url="https://api.example.com/v1/", llm_api_key="sk-test")
    )
    assert provider.chat_url == "https://api.example.com/v1/chat/completions"
    assert provider.models_url == "https://api.example.com/v1/models"


def test_opencode_headers_include_session_and_user_agent():
    provider = OpenAICompatProvider(
        Settings(
            llm_base_url="https://opencode.ai/inference/go/openai/v1",
            llm_api_key="sk-test",
        )
    )
    headers = provider._headers()
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["User-Agent"].startswith("shakedown/")
    assert headers["x-opencode-session"] == provider.session_id
    # 普通 OpenAI 兼容服务不需要 OpenCode 的会话头
    other = OpenAICompatProvider(Settings(llm_base_url="https://api.example.com/v1"))
    assert "x-opencode-session" not in other._headers()


def test_structured_unwraps_type_value_wrapper():
    schema = {"type": "object", "properties": {"category": {"type": "string"}, "reason": {"type": "string"}}}
    wrapped = {"type": "object", "value": {"category": "defect", "reason": "ok"}}
    assert OpenAICompatProvider._unwrap(wrapped, schema) == {"category": "defect", "reason": "ok"}
    assert OpenAICompatProvider._unwrap({"category": "case"}, schema) == {"category": "case"}


def test_proxy_env_ipv6_brackets_are_sanitized():
    """OpenCode 注入的 NO_PROXY=[::1] 会让 httpx 报 Invalid port。"""
    old_upper, old_lower = os.environ.get("NO_PROXY"), os.environ.get("no_proxy")
    try:
        os.environ["NO_PROXY"] = "localhost,[::1]"
        os.environ["no_proxy"] = "localhost,[::1]"
        sanitize_proxy_env()
        assert os.environ["NO_PROXY"] == "localhost,::1"
        assert os.environ["no_proxy"] == "localhost,::1"
    finally:
        if old_upper is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = old_upper
        if old_lower is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = old_lower
