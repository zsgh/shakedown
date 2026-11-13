"""OpenAI 兼容 provider（OpenAI / vLLM / Ollama / GLM 等）。

只依赖 HTTP，不绑定任何 SDK，便于替换模型。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from app.agent.providers.base import LLMError, LLMProvider, LLMResponse, TokenUsage, ToolCall, extract_json
from app.config import Settings


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.llm_model
        base = settings.llm_base_url.rstrip("/")
        # 兼容用户直接填完整 endpoint 的情况：.../v1/chat/completions
        if base.endswith("/chat/completions"):
            self.chat_url = base
            root = base[: -len("/chat/completions")]
        else:
            self.chat_url = base + "/chat/completions"
            root = base
        # OpenCode Go 的模型列表挂在没有 /openai 前缀的路径下
        if "opencode.ai" in root:
            self.models_url = root.split("/openai/v1")[0] + "/v1/models"
        else:
            self.models_url = root + "/models"
        self.api_key = settings.llm_api_key
        # OpenCode Go（以及部分代理）要求稳定的会话头，便于路由与 prompt cache
        self.session_id = uuid.uuid4().hex
        self.user_agent = "shakedown/0.1 (test-agent; +https://github.com/zsgh/shakedown)"

    # ── 内部 ──────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if "opencode.ai" in self.chat_url:
            headers["x-opencode-session"] = self.session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.chat_url
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                with httpx.Client(timeout=self.settings.llm_timeout) as client:
                    resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:500]}")
                return resp.json()
            except (httpx.HTTPError, LLMError) as exc:  # pragma: no cover - 网络路径
                last_error = exc
                time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"LLM 调用失败：{last_error}")

    def _apply_reasoning(self, payload: dict[str, Any], effort: str | None = None) -> dict[str, Any]:
        """设置推理强度（OpenRouter / 部分服务商支持）。

        推理模型在「生成大量结构化内容」时会花掉海量 reasoning token，
        导致输出被截断、速度极慢；把 effort 降到 low 能显著改善。
        """
        chosen = (effort if effort is not None else self.settings.llm_reasoning_effort or "").strip()
        if not chosen or chosen == "auto":
            return payload
        return {**payload, "reasoning": {"effort": chosen}}

    def _post_json_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        """优先用 JSON 模式（模型直接输出可解析 JSON）；不支持时退回普通模式。"""
        json_payload = {**payload, "response_format": {"type": "json_object"}}
        try:
            return self._post(json_payload)
        except LLMError as exc:
            if "response_format" not in str(exc) and "json_object" not in str(exc):
                raise
            return self._post(payload)

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 返回没有 choices：{json.dumps(data)[:300]}")
        message = choices[0].get("message") or {}
        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
            tool_calls.append(ToolCall(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args))
        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
        )
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=usage,
            model=data.get("model", ""),
            raw=data,
        )

    # ── 接口 ──────────────────────────────────────────────
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return self._parse(self._post(self._apply_reasoning(payload, reasoning_effort)))

    def structured(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        material = json.dumps(context, ensure_ascii=False, indent=2) if context else ""
        prompt = f"{user}\n\n== 输入素材（JSON） ==\n{material}\n\n== 输出要求 ==\n只输出一个 JSON 对象，符合以下 schema：\n{json.dumps(schema, ensure_ascii=False)}"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            # 结构化输出可能很长（推理模型还要额外花 reasoning token），给足额度
            "max_tokens": max_tokens or max(self.settings.llm_max_tokens, 8192),
        }
        try:
            data = self._post_json_mode(self._apply_reasoning(payload, reasoning_effort))
        except LLMError as exc:
            raise LLMError(f"结构化输出调用失败：{exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        message = choice.get("message") or {}
        content = message.get("content")
        if not content:
            reasoning = message.get("reasoning") or message.get("reasoning_details")
            if finish == "length":
                raise LLMError(
                    "模型输出被截断（max_tokens 用尽）。请调大 llm_max_tokens，或把素材切小分批处理。"
                )
            raise LLMError(
                "模型返回空内容"
                + ("（该模型只输出了 reasoning，没有正文；可调大 max_tokens）" if reasoning else "")
            )
        if finish == "length":
            raise LLMError("模型输出被截断（max_tokens 用尽），请调大 llm_max_tokens 或分批处理")
        data = extract_json(content)
        return self._unwrap(data, schema)

    @staticmethod
    def _unwrap(data: Any, schema: dict[str, Any]) -> Any:
        """兼容模型偶尔多包一层 {"type":"object","value":{...}} 的情况。"""
        if not isinstance(data, dict):
            return data
        properties = set((schema or {}).get("properties") or {})
        if properties and not (properties & set(data.keys())):
            for key in ("value", "result", "data", "output"):
                nested = data.get(key)
                if isinstance(nested, dict) and (properties & set(nested.keys())):
                    return nested
        return data

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self.models_url, headers=self._headers())
            return {"provider": self.name, "model": self.model, "ok": resp.status_code < 400, "status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "model": self.model, "ok": False, "error": str(exc)}
