"""Agent 工具调用循环 —— 基于 LangGraph 状态图实现。

图结构::

                 ┌──────────────────────────────┐
                 │                              │
      START ──▶ agent ──▶ tools ──▶ agent ──▶ …──▶ END
                 │          (有 tool_calls 时)   │
                 └──────────────────────────────┘
                        (模型给出最终答案时直接 END)

状态（``AgentState``）：
- ``messages``   对话消息（含工具调用与工具结果）
- ``iterations`` 已迭代轮数（防止死循环）
- ``tool_calls`` 最近一次的工具调用（用于轨迹记录）

设计要点：
1. **工具只读**：所有工具都没有副作用（列目录 / 读文件 / grep / 检索），
   写操作一律由平台代码执行，不交给 Agent；
2. **可观测**：每一轮都写 ``agent_traces``（输入 / 输出 / 工具调用 / token / 耗时）；
3. **可替换**：模型由 ``LLMProvider`` 抽象，离线 mock 与真实模型共用同一张图；
4. **有上限**：``max_turns`` 防止模型无限循环。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.agent.chat_model import build_chat_model
from app.agent.providers.base import LLMProvider
from app.agent.tools.base import ToolContext
from app.logging import get_logger

logger = get_logger("shakedown.agent.graph")


class AgentState(TypedDict):
    """LangGraph 的状态定义。"""

    # 消息列表（LangGraph 用 reducer 累积）
    messages: Annotated[list[BaseMessage], add_messages]
    # 已迭代轮数
    iterations: int
    # 最近一次的工具调用（用于写轨迹）
    tool_calls: list[dict[str, Any]]


@dataclass
class LoopResult:
    """循环结果（对上层业务保持原来的返回形态）。"""

    final: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    turns: int = 0
    messages: list[BaseMessage] = field(default_factory=list)


Tracer = Callable[[dict[str, Any]], None]


class _SchemaOnlyTool(BaseTool):
    """只声明 schema 的工具（真实执行在 ``tools_node`` 里）。

    LangChain 的 ``BaseTool`` 是抽象类，必须实现 ``_run``；
    我们把它实现成空函数，因为真正跑工具的是 Shakedown 自己的 ``Tool.run``。
    """

    def _run(self, *args: Any, **kwargs: Any) -> str:
        return ""


def _to_langchain_tools(tools: Sequence[Any]) -> list[BaseTool]:
    """把 Shakedown 的工具对象转成 LangChain 的 BaseTool（**只用于声明 schema**）。

    Shakedown 的工具签名是 ``run(args: dict, ctx: ToolContext)``，
    需要注入上下文，所以不能用 LangChain 的 ToolNode 统一调用，
    我们在 ``tools_node`` 里自己执行。
    """
    langchain_tools: list[BaseTool] = []
    for tool in tools:
        schema = getattr(tool, "parameters", None) or {"type": "object", "properties": {}}
        langchain_tools.append(
            _SchemaOnlyTool(
                name=tool.name,
                description=tool.description,
                args_schema=_args_schema_from_json(schema),
            )
        )
    return langchain_tools


def _args_schema_from_json(schema: dict[str, Any]) -> Any:
    """把 JSON Schema 转成 pydantic 模型（LangChain 的 args_schema 要求 pydantic）。"""
    from pydantic import create_model

    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for name, spec in properties.items():
        field_type = _json_type_to_python(spec)
        default = ... if name in required else spec.get("default", None)
        fields[name] = (field_type, default)
    return create_model(f"{schema.get('title', 'Args')}Model", **fields)  # type: ignore[call-overload]


def _json_type_to_python(spec: dict[str, Any]) -> Any:
    """JSON Schema 类型 → Python 类型注解。"""
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    json_type = spec.get("type")
    if isinstance(json_type, list):
        json_type = next((t for t in json_type if t != "null"), "string")
    return mapping.get(json_type, str)


# 工具与上下文的绑定（LangGraph 图在编译时绑定）
_TOOL_REGISTRY: dict[str, Any] = {}
_TOOL_CONTEXT: dict[str, Any] = {}


class LangGraphAgentLoop:
    """用 LangGraph 实现的 Agent 循环。"""

    def __init__(
        self,
        provider: LLMProvider,
        tools: Sequence[Any] | None = None,
        tracer: Tracer | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.provider = provider
        self.tools = list(tools or [])
        self.tracer = tracer
        self.reasoning_effort = reasoning_effort
        self._graph = self._build_graph()

    # ── 图构建 ──────────────────────────────────────────────
    def _build_graph(self) -> Any:
        """构建状态图：agent → tools → agent …"""
        _TOOL_REGISTRY.clear()
        for tool in self.tools:
            _TOOL_REGISTRY[tool.name] = tool

        langchain_tools = _to_langchain_tools(self.tools)
        model = build_chat_model(self.provider, reasoning_effort=self.reasoning_effort).bind_tools(langchain_tools)

        def agent_node(state: AgentState) -> dict[str, Any]:
            """调用模型：有 tool_calls 就继续走 tools 节点，否则结束。"""
            started = time.time()
            messages = state["messages"]
            try:
                response = model.invoke(messages)
            except Exception as exc:  # noqa: BLE001
                logger.warning("模型调用失败：%s", exc)
                self._trace(state, None, [], error=str(exc), duration_ms=int((time.time() - started) * 1000))
                return {"messages": [AIMessage(content=f"模型调用失败：{exc}")], "iterations": state["iterations"] + 1}

            calls = [
                {"id": call.get("id", ""), "name": call.get("name", ""), "arguments": call.get("args") or {}}
                for call in getattr(response, "tool_calls", []) or []
            ]
            self._trace(state, response, calls, duration_ms=int((time.time() - started) * 1000))
            return {
                "messages": [response],
                "iterations": state.get("iterations", 0) + 1,
                # 累积所有轮次的工具调用（最后一轮没有调用时不要把之前的清空）
                "tool_calls": list(state.get("tool_calls") or []) + calls,
            }

        def tools_node(state: AgentState) -> dict[str, Any]:
            """执行工具调用（Shakedown 工具需要 ToolContext）。"""
            last = state.get("messages", [])[-1] if state.get("messages") else None
            calls = getattr(last, "tool_calls", []) or []
            outputs: list[ToolMessage] = []
            for call in calls:
                tool = _TOOL_REGISTRY.get(call.get("name", ""))
                if tool is None:
                    outputs.append(
                        ToolMessage(
                            content=f"未知工具：{call.get('name')}",
                            tool_call_id=call.get("id", ""),
                            name=call.get("name", ""),
                        )
                    )
                    continue
                ctx: ToolContext = _TOOL_CONTEXT.get("ctx")
                try:
                    result = tool.run(call.get("args") or {}, ctx)
                    outputs.append(
                        ToolMessage(
                            content=result.as_text()[:8000],
                            tool_call_id=call.get("id", ""),
                            name=call.get("name", ""),
                            status="error" if not result.ok else "success",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    outputs.append(
                        ToolMessage(
                            content=f"工具执行失败：{exc}",
                            tool_call_id=call.get("id", ""),
                            name=call.get("name", ""),
                            status="error",
                        )
                    )
            return {"messages": outputs}

        def should_continue(state: AgentState) -> str:
            """决定下一步：还有工具调用且未超轮数 → tools，否则结束。"""
            messages = state.get("messages") or []
            last = messages[-1] if messages else None
            has_calls = bool(getattr(last, "tool_calls", None))
            if has_calls and state.get("iterations", 0) < self.max_turns:
                return "tools"
            return END

        graph = StateGraph(AgentState)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", END: END},
        )
        graph.add_edge("tools", "agent")
        return graph.compile()

    max_turns: int = 8

    # ── 轨迹 ────────────────────────────────────────────────
    def _trace(
        self,
        state: AgentState,
        response: Any,
        tool_calls: list[dict[str, Any]],
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """写 agent_traces（输入 / 输出 / 工具调用 / token / 耗时）。"""
        if self.tracer is None:
            return
        usage = getattr(response, "usage_metadata", None) or {}
        payload = {
            "input": {"messages": _summarize_messages(state.get("messages") or [])},
            "output": {"content": str(getattr(response, "content", ""))[:8000]},
            "tool_calls": tool_calls,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
            "error": error,
            "duration_ms": duration_ms,
        }
        try:
            self.tracer(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写 agent_trace 失败：%s", exc)

    # ── 运行 ────────────────────────────────────────────────
    def run(
        self,
        task: str,
        ctx: ToolContext,
        *,
        system: str = "你是一个严谨的软件测试分析助手。",
        max_turns: int = 8,
    ) -> LoopResult:
        """跑一轮 Agent 循环，返回最终答案。"""
        self.max_turns = max_turns
        _TOOL_CONTEXT["ctx"] = ctx
        state: AgentState = {
            "messages": [SystemMessage(content=system), HumanMessage(content=task)],
            "iterations": 0,
            "tool_calls": [],
        }
        final_state = self._graph.invoke(state, {"recursion_limit": max_turns * 3 + 6})
        messages = final_state.get("messages") or []
        final = ""
        for message in reversed(messages):
            if isinstance(message, AIMessage) and message.content:
                final = str(message.content)
                break
        # 到达轮数上限时模型可能还在「我再查查」——强制收尾：不带工具再问一次
        last = messages[-1] if messages else None
        if getattr(last, "tool_calls", None):
            final = self._force_final(messages) or final
        usage = {"input_tokens": 0, "output_tokens": 0}
        for message in messages:
            meta = getattr(message, "usage_metadata", None) or {}
            usage["input_tokens"] += int(meta.get("input_tokens", 0) or 0)
            usage["output_tokens"] += int(meta.get("output_tokens", 0) or 0)
        return LoopResult(
            final=final,
            tool_calls=final_state.get("tool_calls") or [],
            usage=usage,
            turns=int(final_state.get("iterations", 0)),
            messages=messages,
        )

    def _force_final(self, messages: list[BaseMessage]) -> str:
        """不带工具地问最后一次：要最终答案（JSON / 结论）。"""
        model = build_chat_model(self.provider, reasoning_effort=self.reasoning_effort)  # 注意：这里**不** bind_tools
        nudge = HumanMessage(
            content=(
                "不要再调用任何工具。请基于上面已经查到的信息，"
                "直接给出最终答案（如果要求 JSON，就只输出 JSON）。"
            )
        )
        try:
            response = model.invoke(list(messages) + [nudge])
        except Exception as exc:  # noqa: BLE001
            logger.warning("强制收尾失败：%s", exc)
            return ""
        return str(getattr(response, "content", "") or "")


def _summarize_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """消息摘要（写轨迹用，避免把全文塞进库）。"""
    summary: list[dict[str, Any]] = []
    for message in messages:
        content = message.content if isinstance(message.content, str) else str(message.content)
        item: dict[str, Any] = {"role": getattr(message, "type", "?"), "content": content[:2000]}
        calls = getattr(message, "tool_calls", None)
        if calls:
            item["tool_calls"] = [
                {"name": c.get("name"), "args": str(c.get("args"))[:500]} for c in calls
            ]
        summary.append(item)
    return summary
