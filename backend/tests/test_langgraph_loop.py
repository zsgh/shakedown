"""LangGraph Agent 循环的测试。

用一个「会主动调工具」的假 provider 验证：
- 工具确实被调用（且注入了 ToolContext）；
- 结果回灌后模型能给出最终答案；
- 轨迹（tracer）被记录。
"""
from __future__ import annotations



from app.agent.chat_model import build_chat_model
from app.agent.loop import LangGraphAgentLoop
from app.agent.providers.base import LLMProvider, LLMResponse, TokenUsage, ToolCall
from app.agent.tools.base import Tool, ToolContext, ToolResult


class _EchoTool(Tool):
    name = "echo"
    description = "回显输入的内容"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要回显的文本"}},
        "required": ["text"],
    }

    def run(self, args, ctx):  # noqa: ANN001, ANN201
        # 把上下文也带出来，验证 ToolContext 注入成功
        return ToolResult(ok=True, data={"echo": args.get("text"), "project": ctx.project.name})


class _ScriptedProvider(LLMProvider):
    """第一次调用要求调工具，第二次给出最终答案。"""

    name = "scripted"
    model = "scripted-1"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._step = 0

    def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, reasoning_effort=None):  # noqa: ANN001, ANN201
        self.calls.append({"messages": messages, "tools": tools})
        self._step += 1
        if self._step == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call-1", name="echo", arguments={"text": "你好"})],
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                model=self.model,
            )
        return LLMResponse(
            content="最终答案：你好",
            usage=TokenUsage(input_tokens=20, output_tokens=7),
            model=self.model,
        )


def test_langgraph_loop_calls_tool_and_returns_final(sample_project):
    provider = _ScriptedProvider()
    traces: list[dict] = []
    loop = LangGraphAgentLoop(provider, tools=[_EchoTool()], tracer=traces.append)
    ctx = ToolContext(session=None, project=sample_project)  # type: ignore[arg-type]
    result = loop.run("请回显「你好」", ctx, max_turns=4)

    assert result.final == "最终答案：你好"
    assert provider._step == 2  # 模型被调用了两次（一次要工具，一次给答案）
    assert result.tool_calls and result.tool_calls[0]["name"] == "echo"
    assert result.usage["input_tokens"] == 30
    # 工具结果回灌进了 messages
    tool_messages = [m for m in result.messages if getattr(m, "type", "") == "tool"]
    assert tool_messages
    assert "你好" in str(tool_messages[0].content)
    # 轨迹被记录
    assert traces
    assert any(t.get("tool_calls") for t in traces)


def test_langgraph_loop_stops_at_max_turns(sample_project):
    """模型一直要工具时，不能无限循环。"""

    class _LoopProvider(LLMProvider):
        name = "loop"
        model = "loop-1"

        def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, reasoning_effort=None):  # noqa: ANN001, ANN201
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id="x", name="echo", arguments={"text": "again"})],
                usage=TokenUsage(),
                model=self.model,
            )

    loop = LangGraphAgentLoop(_LoopProvider(), tools=[_EchoTool()])
    ctx = ToolContext(session=None, project=sample_project)  # type: ignore[arg-type]
    result = loop.run("一直调工具", ctx, max_turns=3)
    assert result.turns <= 4  # 有上限


def test_chat_model_maps_provider_response(sample_project):
    """provider 响应 → LangChain AIMessage（tool_calls / usage）。"""
    provider = _ScriptedProvider()
    model = build_chat_model(provider)
    message = model.invoke([{"role": "user", "content": "hi"}])
    assert message.tool_calls
    assert message.tool_calls[0]["name"] == "echo"
    assert message.usage_metadata["input_tokens"] == 10


def test_tool_schema_to_langchain_args_schema():
    from app.agent.loop import _args_schema_from_json

    model = _args_schema_from_json(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        }
    )
    assert "a" in model.model_fields
    assert model.model_fields["a"].annotation is str
    assert model.model_fields["b"].annotation is int
