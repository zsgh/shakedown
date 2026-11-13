"""从文档里抽「接口清单」。

很多项目的 API 文档不是 OpenAPI，而是 Markdown 里的端点表 / 代码块：

```
GET    /api/health             {version, uptime, pid, port}
PATCH  /api/env/vars/{name}    改值/备注/启用状态
| POST | /api/users | 创建用户 |
```

这类清单是「文档式接口规范」，抽出来就能当接口来源（source=doc）参与三方对照，
也给 Agent 提供可引用的依据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

# 行首（可选表格竖线）+ 方法 + 可选竖线 + 路径 + 可选说明
_ENDPOINT_LINE = re.compile(
    r"^[ \t>|]*`?(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)`?[ \t]*\|?[ \t]*`?(?P<path>/[^\s`|]+)`?(?P<rest>[^\n]*)$",
    re.IGNORECASE,
)

# 明显不是接口清单的章节标题
_SKIP_SECTION = re.compile(r"(?i)^(日志|log|变更|changelog|目录|table of contents|历史)")
_API_SECTION = re.compile(r"(?i)(api|接口|端点|endpoint|路由|route)")


@dataclass
class DocEndpoint:
    method: str
    path: str
    summary: str = ""
    locator: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.locator is None:
            self.locator = {}


_HTTP_METHOD_NAMES = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# 开发日志类文档：记录改动过程，不是接口规范，不从中抽接口
_DEV_LOG_FILES = {
    "todo.md",
    "todos.md",
    "changelog.md",
    "changes.md",
    "history.md",
    "journal.md",
    "开发日志.md",
    "待办.md",
    "计划.md",
    "变更记录.md",
}


def is_dev_log(file_path: str) -> bool:
    """判断是不是开发日志 / 变更记录类文档。"""
    if not file_path:
        return False
    name = str(file_path).replace("\\", "/").split("/")[-1].strip().lower()
    if name in _DEV_LOG_FILES:
        return True
    # todo-2024-xx.md / changelog_v2.md 这类命名
    stem = name.rsplit(".", 1)[0]
    return any(stem.startswith(prefix) for prefix in ("todo-", "todo_", "changelog_", "changelog-"))


def _looks_like_method_list(path: str) -> bool:
    """排除 "GET / POST / PUT" 这种把方法名当路径的误报。"""
    segments = [s for s in path.split("/") if s]
    if not segments:
        return True
    return all(segment.upper() in _HTTP_METHOD_NAMES for segment in segments)


def _clean_path(path: str) -> str:
    path = path.strip().strip("`").rstrip(".,;")
    # 去掉行尾的参数说明（?format=json 之类保留，路径后的说明另取）
    return path


def extract_doc_endpoints(chunks: list[dict[str, Any]]) -> list[DocEndpoint]:
    """从文档切片里抽取端点清单。"""
    found: list[DocEndpoint] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        content = chunk.get("content") or ""
        locator = chunk.get("locator") or {}
        file_path = locator.get("file_path") or ""
        if is_dev_log(file_path):
            continue  # 开发日志不作为接口规范
        section_path = locator.get("section_path") or []
        in_api_section = bool(_API_SECTION.search(" ".join(str(s) for s in section_path)))
        in_code_block = False

        for index, line in enumerate(content.splitlines()):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if _SKIP_SECTION.match(stripped) and stripped.startswith("#"):
                continue
            match = _ENDPOINT_LINE.match(line)
            if not match:
                continue
            method = match.group("method").upper()
            path = _clean_path(match.group("path"))
            if not path.startswith("/") or path == "/":
                continue
            if _looks_like_method_list(path):
                continue
            # 太宽松的路径（像文件路径）不收
            last = path.rstrip("/").split("/")[-1]
            if last.startswith(".") or (("." in last) and last.rsplit(".", 1)[-1].isalpha()):
                continue
            # 要么在 API 章节里，要么在代码块 / 表格行里
            table_row = stripped.startswith("|")
            if not (in_api_section or in_code_block or table_row):
                continue
            rest = (match.group("rest") or "").strip().strip("|").strip()
            summary = rest.split("  ")[0].strip() if rest else ""
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            start_line = int(locator.get("start_line") or 1) + index
            found.append(
                DocEndpoint(
                    method=method,
                    path=path,
                    summary=summary[:200],
                    locator={
                        "file_path": file_path,
                        "section_path": list(section_path),
                        "line": start_line,
                    },
                )
            )
    return found


def looks_like_api_doc(chunks: list[dict[str, Any]]) -> bool:
    """判断一份文档里是否含端点清单（用于自动导入时的提示）。"""
    return len(extract_doc_endpoints(chunks)) >= 2
