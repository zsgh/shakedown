"""Markdown 文档切块：按标题层级，保留章节路径作为来源位置。"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*```")


@dataclass
class MarkdownChunk:
    content: str
    section_path: list[str] = field(default_factory=list)
    heading: str = ""
    level: int = 0
    file_path: str = ""
    start_line: int = 1
    end_line: int = 1
    content_hash: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def split_markdown(text: str, file_path: str = "") -> list[MarkdownChunk]:
    """按标题层级切块；同一标题下的正文归为一块。"""
    lines = text.splitlines()
    chunks: list[MarkdownChunk] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    current_heading = ""
    current_level = 0
    start_line = 1
    in_fence = False

    def flush(end_line: int) -> None:
        nonlocal current, current_heading, current_level, start_line
        content = "\n".join(current).strip()
        if content:
            section = [title for _level, title in stack]
            chunk = MarkdownChunk(
                content=content,
                section_path=section,
                heading=current_heading,
                level=current_level,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                meta={"section": " > ".join(section) if section else ""},
            )
            chunks.append(chunk)
        current = []

    for idx, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        match = _HEADING.match(line) if not in_fence else None
        if match:
            flush(idx - 1)
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_heading = title
            current_level = level
            start_line = idx
            current.append(line)
        else:
            if not current and not line.strip():
                continue
            current.append(line)
    flush(len(lines))

    if not chunks and text.strip():
        content = text.strip()
        chunks.append(
            MarkdownChunk(
                content=content,
                section_path=[],
                heading="",
                level=0,
                file_path=file_path,
                start_line=1,
                end_line=len(lines),
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
    return chunks


def iter_markdown_files(root: Path, recursive: bool = True) -> list[Path]:
    root = Path(root).expanduser().resolve()
    if root.is_file():
        return [root] if root.suffix.lower() in {".md", ".markdown", ".mdx"} else []
    if not root.exists():
        return []
    pattern = "**/*.md" if recursive else "*.md"
    files = [p for p in root.glob(pattern) if p.is_file()]
    extra = [p for p in root.glob("**/*.markdown") if p.is_file()]
    files.extend(extra)
    return sorted(set(files))


class MarkdownSourceError(Exception):
    """Markdown 知识源定位失败（错误信息要能直接照做）。"""


def is_url(location: str) -> bool:
    return str(location).strip().lower().startswith(("http://", "https://"))


def github_raw_url(url: str) -> str:
    """把 GitHub 网页地址转成 raw 地址（其它 URL 原样返回）。

    https://github.com/owner/repo/blob/main/todo.md
      → https://raw.githubusercontent.com/owner/repo/main/todo.md
    """
    text = url.strip()
    if "github.com" not in text or "raw.githubusercontent.com" in text:
        return text
    for prefix in ("https://github.com/", "http://github.com/"):
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            parts = rest.split("/", 3)
            if len(parts) >= 4 and parts[2] in {"blob", "raw"}:
                owner, repo, _, path = parts
                return f"https://raw.githubusercontent.com/{owner}/{repo}/{path}"
            if len(parts) == 3 and parts[2] in {"blob", "raw"}:
                owner, repo = parts[0], parts[1]
                return f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"
    return text


def fetch_remote_markdown(url: str, timeout: float = 20.0, retries: int = 3) -> tuple[str, str]:
    """拉取远程文档，返回 ``(正文, 来源标识)``（带重试，应对偶发网络抖动）。

    支持：Markdown / 纯文本 / **HTML 页面**（自动转 Markdown，人能看懂的网页都能导）。
    """
    import httpx

    raw_url = github_raw_url(url)
    last_error = ""
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(raw_url, headers={"Accept": "text/markdown, text/html, text/plain, */*"})
            if resp.status_code >= 400:
                raise MarkdownSourceError(
                    f"拉取 {url} 失败：HTTP {resp.status_code}。"
                    "如果是 GitHub 网页地址，请确认文件存在；也可以先 clone 仓库再导入本地路径。"
                )
            content_type = resp.headers.get("content-type", "")
            body = resp.text
            name = raw_url.rstrip("/").split("/")[-1] or "remote.md"
            if "html" in content_type.lower() or body.lstrip()[:1] == "<":
                # HTML 页面 → Markdown（wiki / 文档站 / 渲染后的 README）
                from app.ingest.webdoc import html_to_markdown

                text, page_title = html_to_markdown(body)
                if not text.strip():
                    raise MarkdownSourceError(f"{url} 抓到的页面没有正文内容")
                header = f"# {page_title}\n\n" if page_title else ""
                return header + text, raw_url
            if not name.lower().endswith((".md", ".markdown", ".mdx", ".txt", ".html")):
                name = f"{name}.md"
            return body, raw_url
        except MarkdownSourceError:
            raise
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise MarkdownSourceError(
        f"拉取 {url} 失败（重试 {retries} 次）：{last_error}。"
        "网络不可达时，可以先 clone 仓库再导入本地路径。"
    )


def resolve_markdown_inputs(location: str, recursive: bool = True) -> list[tuple[str, str]]:
    """把「本地路径 / 远程 URL」统一解析成 ``[(正文, 来源标识), ...]``。"""
    text = str(location).strip()
    if is_url(text):
        return [fetch_remote_markdown(text)]

    root = Path(text).expanduser()
    if not root.exists():
        raise MarkdownSourceError(
            f"路径不存在：{root}。"
            "Markdown 知识源支持：①本地 .md 文件 ②本地目录（递归导入 .md）③远程单文件 URL。"
        )
    files = iter_markdown_files(root, recursive=recursive)
    if not files:
        raise MarkdownSourceError(
            f"{root} 下没有找到 Markdown 文件（支持 .md / .markdown / .mdx）。"
        )
    return [(path.read_text(encoding="utf-8", errors="replace"), str(path)) for path in files]


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def content_version(chunks: list[MarkdownChunk]) -> str:
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk.content_hash.encode())
    return hasher.hexdigest()[:32]
