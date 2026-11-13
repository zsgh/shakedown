"""代码相关工具：列目录、读文件、grep、按符号检索。

这四个工具覆盖了「Agent 读代码」的主要场景：
- 不知道有什么 → ``list_code_files``
- 知道文件想看某段 → ``read_code_file``
- 知道关键词想定位 → ``grep_code``
- 知道符号名想找定义 → ``find_symbol``
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.ingest.code import CODE_EXTENSIONS, SKIP_DIRS, chunk_code_file


class ListCodeFiles(Tool):
    """列出项目代码库中的文件。"""

    name = "list_code_files"
    description = (
        "列出代码库里的文件（可按子目录过滤）。"
        "当你不知道项目有哪些文件、或要找某个模块在哪时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对路径前缀，留空表示全部"},
            "limit": {"type": "integer", "description": "最多返回多少个文件（默认 200）"},
        },
        "required": [],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        prefix = (args.get("path") or "").strip("/")
        limit = int(args.get("limit") or 200)
        files: list[dict[str, Any]] = []
        for root in ctx.code_roots:
            base = root / prefix if prefix else root
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                if path.suffix.lower() not in CODE_EXTENSIONS:
                    continue
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    continue
                files.append({"path": rel, "size": path.stat().st_size})
                if len(files) >= limit:
                    break
            if len(files) >= limit:
                break
        return ToolResult(ok=True, data={"files": files, "count": len(files)})


class ReadCodeFile(Tool):
    """读取代码文件的指定行范围。"""

    name = "read_code_file"
    description = (
        "读取代码文件的指定行范围（返回带行号的代码）。"
        "当你已经知道文件路径、需要看具体实现时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对代码根目录的文件路径"},
            "start_line": {"type": "integer", "description": "起始行（1 起）"},
            "end_line": {"type": "integer", "description": "结束行（含）"},
        },
        "required": ["path"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = args.get("path") or ""
        if not path:
            return ToolResult(ok=False, error="缺少 path 参数")
        target = ctx.root_for(path)
        if target is None:
            return ToolResult(ok=False, error=f"文件不存在或不在代码库内：{path}")
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(ok=False, error=f"读取失败：{exc}")
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or min(len(lines), start + 200))
        end = min(end, len(lines))
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return ToolResult(ok=True, data={"path": path, "start_line": start, "end_line": end, "content": body})


class GrepCode(Tool):
    """在代码库里按正则搜索。"""

    name = "grep_code"
    description = (
        "在代码库中按正则表达式搜索，返回匹配行与位置。"
        "当你知道关键词但不知道在哪个文件时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "限定目录前缀"},
            "max_results": {"type": "integer", "description": "最多返回条数，默认 50"},
        },
        "required": ["pattern"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern") or ""
        if not pattern:
            return ToolResult(ok=False, error="缺少 pattern 参数")
        try:
            regex = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            return ToolResult(ok=False, error=f"正则不合法：{exc}")
        prefix = (args.get("path") or "").strip("/")
        limit = int(args.get("max_results") or 50)
        matches: list[dict[str, Any]] = []
        for root in ctx.code_roots:
            base = root / prefix if prefix else root
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
                    continue
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                try:
                    rel = str(path.relative_to(root))
                    content = path.read_text(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    continue
                for idx, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        matches.append({"path": rel, "line": idx, "text": line.strip()[:300]})
                        if len(matches) >= limit:
                            return ToolResult(ok=True, data={"matches": matches, "count": len(matches)})
        return ToolResult(ok=True, data={"matches": matches, "count": len(matches)})


class FindSymbol(Tool):
    """按类名 / 函数名检索代码定义。"""

    name = "find_symbol"
    description = (
        "按类名 / 函数名检索代码定义，返回文件与行号。"
        "当你知道符号名（函数名、类型名）想找它的实现位置时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "符号名（大小写不敏感）"}},
        "required": ["name"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult(ok=False, error="缺少 name 参数")
        lowered = name.lower()
        found: list[dict[str, Any]] = []
        for root in ctx.code_roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
                    continue
                if any(part in SKIP_DIRS for part in path.parts):
                    continue
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    continue
                for chunk in chunk_code_file(path, root):
                    symbol = chunk.symbol or ""
                    if symbol and lowered in symbol.lower():
                        found.append(
                            {
                                "path": rel,
                                "symbol": symbol,
                                "start_line": chunk.start_line,
                                "end_line": chunk.end_line,
                                "kind": chunk.kind,
                            }
                        )
                        break
                if len(found) >= 50:
                    return ToolResult(ok=True, data={"symbols": found, "count": len(found)})
        return ToolResult(ok=True, data={"symbols": found, "count": len(found)})


CODE_TOOLS = [ListCodeFiles(), ReadCodeFile(), GrepCode(), FindSymbol()]
