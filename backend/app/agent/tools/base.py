"""Agent 工具基类。

工具是 Agent 访问项目知识的唯一入口。约定：

1. **只读**：所有工具都没有副作用（列目录 / 读文件 / grep / 检索），
   写操作一律由平台代码执行，不交给 Agent；
2. **有描述**：``description`` 会直接进模型上下文，要写清楚「什么时候用这个工具」；
3. **有入参 schema**：JSON Schema 形式，会被转成 LangChain 的 ``args_schema``；
4. **返回结构化结果**：``ToolResult.as_text()`` 是给模型看的文本（会被截断到 8k）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import Project


@dataclass
class ToolResult:
    """工具执行结果。"""

    ok: bool
    data: Any = None
    error: str = ""

    def as_text(self) -> str:
        """转成给模型看的文本（失败时直接说明原因）。"""
        if not self.ok:
            return f"ERROR: {self.error}"
        if isinstance(self.data, str):
            return self.data
        if isinstance(self.data, (dict, list)):
            import json

            return json.dumps(self.data, ensure_ascii=False, default=str)
        return str(self.data)


@dataclass
class ToolContext:
    """工具执行上下文（由平台注入，Agent 不能伪造）。"""

    session: Session
    project: Project
    # 代码库的根目录列表（可多个仓库）
    code_roots: list[Path] = field(default_factory=list)
    # 只读文件白名单（预留）
    read_only_files: set[str] = field(default_factory=set)

    def root_for(self, relative: str) -> Path | None:
        """把相对路径解析到某个代码根目录内（防目录穿越）。"""
        for root in self.code_roots:
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.exists():
                return candidate
        return None


class Tool(ABC):
    """工具基类。"""

    # 工具名（模型看到的就是它，必须是合法的函数名）
    name: str = "tool"
    # 工具说明（进模型上下文，要写清楚使用时机）
    description: str = ""
    # 入参 JSON Schema
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。"""

    def schema(self) -> dict[str, Any]:
        """OpenAI 风格的工具 schema（provider 侧用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
