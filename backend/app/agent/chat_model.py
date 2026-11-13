"""把 Shakedown 自己的 LLM provider 适配成 LangChain 的 ChatModel。

为什么要这层适配：
- Shakedown 的 provider 抽象（``LLMProvider``）是我们自己的接口，换模型不受影响；
- LangGraph 需要一个 LangChain 风格的模型对象（能 ``bind_tools``、能返回 ``AIMessage``）；
- 这层适配让 **离线 mock provider 和真实模型共用同一张图**，测试与生产行为一致。

映射关系：

| Shakedown            | LangChain                          |
|----------------------|------------------------------------|
| ``LLMResponse.content`` | ``AIMessage.content``            |
| ``ToolCall``         | ``AIMessage.tool_calls``             |
| ``TokenUsage``       | ``AIMessage.usage_metadata``         |
| provider.tools 参数   | ``model.bind_tools(tools)``          |
"""
from __future__ import annotations

from typing import Any, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from app.agent.providers.base import LLMProvider, LLMResponse


class ProviderChatModel(BaseChatModel):
    """把 ``LLMProvider`` 包成 LangChain ChatModel。

    注意：LangChain 的 BaseChatModel 是 pydantic 模型，所以字段要用 ``PrivateAttr`` 存。
    """

    # provider 实例（不放进 pydantic 字段，避免被当成模型配置序列化）
    provider: Any = None
    # 绑定的工具（LangChain 的 bind_tools 会返回 self.bind(tools=...)，这里用 model_config 承载）
    bound_tools: list[BaseTool] = []
    # 白盒等长任务可单独降低推理强度，避免每个工具轮次都跑 high reasoning
    reasoning_effort: str | None = None

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return f"shakedown-{getattr(self.provider, 'name', 'provider')}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": getattr(self.provider, "model", "")}

    # ── 工具绑定 ────────────────────────────────────────────
    def bind_tools(self, tools: Sequence[BaseTool], **kwargs: Any) -> "ProviderChatModel":
        """绑定工具（LangGraph 的 ToolNode 依赖这个约定）。"""
        bound = list(tools)
        clone = self.model_copy(update={"bound_tools": bound})
        return clone

    # ── 真正的调用 ──────────────────────────────────────────
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """把 LangChain 消息转成 provider 的 messages 格式并调用。"""
        payload: list[dict[str, Any]] = []
        for message in messages:
            role = _role_of(message)
            content = message.content if isinstance(message.content, str) else str(message.content)
            payload.append({"role": role, "content": content})

        # 工具定义转成 OpenAI 风格（provider 侧统一处理）
        tool_schemas: list[dict[str, Any]] = []
        for item in self.bound_tools:
            tool_schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": (item.description or "").strip(),
                        "parameters": _schema_of(item),
                    },
                }
            )

        response: LLMResponse = self.provider.chat(
            payload,
            tools=tool_schemas or None,
            reasoning_effort=self.reasoning_effort,
            **kwargs,
        )
        return ChatResult(
            generations=[ChatGeneration(message=_to_ai_message(response))],
            llm_output={"model": response.model},
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # pragma: no cover
        """异步版本：provider 是同步的，这里直接复用。"""
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _role_of(message: BaseMessage) -> str:
    """LangChain 消息 → provider 的 role。"""
    role = getattr(message, "type", "human")
    return {"human": "user", "ai": "assistant", "system": "system", "tool": "user"}.get(role, "user")


def _schema_of(item: BaseTool) -> dict[str, Any]:
    """取工具的入参 schema（LangChain 的 args_schema）。"""
    schema = getattr(item, "args_schema", None)
    if schema is None:
        return {"type": "object", "properties": {}}
    try:
        return schema.model_json_schema()
    except Exception:  # noqa: BLE001
        return {"type": "object", "properties": {}}


def _to_ai_message(response: LLMResponse) -> AIMessage:
    """provider 响应 → LangChain 的 AIMessage（含 tool_calls）。"""
    tool_calls = [
        {
            "id": call.id,
            "name": call.name,
            "args": call.arguments,
            "type": "tool_call",
        }
        for call in response.tool_calls
    ]
    usage = response.usage.as_dict() if response.usage else {}
    return AIMessage(
        content=response.content or "",
        tool_calls=tool_calls,
        response_metadata={"model": response.model},
        usage_metadata={
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    )


def build_chat_model(provider: LLMProvider, reasoning_effort: str | None = None) -> ProviderChatModel:
    """构造 LangChain 模型对象。"""
    return ProviderChatModel(provider=provider, bound_tools=[], reasoning_effort=reasoning_effort)
