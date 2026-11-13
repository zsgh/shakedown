"""LLM provider 抽象层。

上层（planner / generator / analyst / whitebox）只依赖这里定义的接口，
换模型只需要改配置（``SHAKEDOWN_LLM_PROVIDER``）。

统一能力：
- ``chat``：对话 + 工具调用；
- ``structured``：结构化输出（JSON Schema 约束），失败时抛 ``LLMError``。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """LLM 调用或解析失败。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total,
        }


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """所有 provider 的统一接口。"""

    name: str = "base"
    model: str = ""
    # 是否支持「模型主动发起工具调用」（离线 mock 不支持）
    supports_tools: bool = True

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """对话（可带工具定义）。"""

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
        """结构化输出：必须返回符合 ``schema`` 的 JSON 对象。

        ``context`` 是给「离线/mock provider」的结构化素材；对真实模型而言，
        实现方会把它序列化进 prompt。
        """
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "ok": True}


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON（容忍 ```json 包裹与前后废话）。"""
    if not text:
        raise LLMError("模型返回为空")
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出不是合法 JSON：{exc}") from exc
    raise LLMError("模型输出中找不到 JSON 对象")
