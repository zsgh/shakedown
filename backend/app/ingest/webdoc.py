"""网页文档抓取与清洗。

人能看懂的接口文档，往往不是 OpenAPI，而是：

- 内部 wiki / 文档站的一个 HTML 页面；
- GitHub / Gitea 上渲染出来的 README；
- 一份手写 Markdown。

这里做三件事：
1. 抓页面（带重试、识别内容类型）；
2. HTML → Markdown（去脚本/样式/导航，保留标题、表格、列表、代码块）；
3. 给 Agent 用的纯文本版本（去掉导航噪音，保留正文）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

import httpx

from app.logging import get_logger

logger = get_logger("shakedown.ingest.webdoc")

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 8 * 1024 * 1024

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "iframe", "meta", "link"}
# 空元素：没有结束标签，不能参与嵌套计数
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "main", "ul", "ol", "table", "tr", "pre",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
}


class WebDocError(Exception):
    """网页文档抓取失败。"""


@dataclass
class WebDoc:
    url: str
    title: str = ""
    text: str = ""
    content_type: str = ""
    is_html: bool = False


class _HtmlToMarkdown(HTMLParser):
    """把 HTML 转成 Markdown 子集（标题/段落/列表/表格/代码块/链接/行内代码）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_pre = False
        self._in_code = False
        self._list_stack: list[str] = []
        self._row: list[str] = []
        self._table_rows: list[list[str]] = []
        self._in_table = False
        self.title = ""
        self._in_title = False
        self._link_href = ""

    # ── 工具 ─────────────────────────────────────────────
    def _flush(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer = []
        if not text:
            return
        prefix = ""
        for marker in self._list_stack:  # 嵌套列表的缩进前缀
            prefix += marker
        self.lines.append(f"{prefix}{text}".rstrip())

    def _newline(self) -> None:
        if self._buffer:
            self._flush()
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    # ── 解析 ─────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:  # 空元素没有结束标签
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._newline()
            self._buffer.append("#" * int(tag[1]) + " ")
        elif tag == "pre":
            self._newline()
            self._in_pre = True
            self.lines.append("```")
        elif tag == "code" and not self._in_pre:
            self._in_code = True
            self._buffer.append("`")
        elif tag == "br":
            self._buffer.append("\n")
        elif tag == "li":
            self._flush()
        elif tag in {"ul", "ol"}:
            self._flush()
            self._list_stack.append("- " if tag == "ul" else "1. ")
        elif tag == "table":
            self._flush()
            self._in_table = True
            self._table_rows = []
        elif tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._row.append("")
        elif tag == "a":
            self._link_href = dict(attrs).get("href") or ""
        elif tag in {"strong", "b"}:
            self._buffer.append("**")
        elif tag in {"em", "i"}:
            self._buffer.append("*")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section", "article"}:
            self._flush()
            self._newline()
        elif tag == "pre":
            self._in_pre = False
            self.lines.append("```")
            self._newline()
        elif tag == "code" and not self._in_pre:
            self._in_code = False
            self._buffer.append("`")
        elif tag == "li":
            self._flush()
        elif tag in {"ul", "ol"}:
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
        elif tag == "a":
            text = "".join(self._buffer).strip()
            self._buffer = []
            if self._link_href:
                self._buffer.append(f"[{text}]({self._link_href})")
            else:
                self._buffer.append(text)
        elif tag in {"strong", "b"}:
            self._buffer.append("**")
        elif tag in {"em", "i"}:
            self._buffer.append("*")
        elif tag == "tr":
            if self._row:
                self._table_rows.append([cell.strip() for cell in self._row])
            self._row = []
        elif tag == "table":
            self._in_table = False
            self._render_table()
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if self._in_pre:
            self.lines.append(data.rstrip("\n"))
            return
        if self._in_table and self._row:
            self._row[-1] += data
            return
        if data.strip():
            self._buffer.append(data if self._in_code else re.sub(r"\s+", " ", data))

    def _render_table(self) -> None:
        rows = [row for row in self._table_rows if any(cell for cell in row)]
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        self.lines.append("| " + " | ".join(rows[0]) + " |")
        self.lines.append("| " + " | ".join(["---"] * width) + " |")
        for row in rows[1:]:
            self.lines.append("| " + " | ".join(row) + " |")

    def result(self) -> str:
        self._flush()
        out: list[str] = []
        for line in self.lines:
            if line == "" and out and out[-1] == "":
                continue
            out.append(line.rstrip())
        text = "\n".join(out)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str) -> tuple[str, str]:
    """HTML → (Markdown 正文, 页面标题)。"""
    parser = _HtmlToMarkdown()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("HTML 解析异常，退回纯文本：%s", exc)
        return _strip_tags(html), ""
    return parser.result(), parser.title.strip()


def _strip_tags(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s{2,}", " ", text).strip()


def fetch_web_doc(url: str, timeout: float = DEFAULT_TIMEOUT, retries: int = 3) -> WebDoc:
    """抓一个网页文档，返回清洗后的正文。"""
    import time

    last_error = ""
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(
                    url,
                    headers={
                        "Accept": "text/markdown, text/html, text/plain, application/json;q=0.9, */*;q=0.8",
                        "User-Agent": "Shakedown/0.1 (+https://github.com/zsgh/shakedown)",
                    },
                )
            if resp.status_code >= 400:
                raise WebDocError(f"HTTP {resp.status_code}")
            content_type = resp.headers.get("content-type", "")
            raw = resp.text[:MAX_BYTES]
            if "html" in content_type.lower() or raw.lstrip()[:1] == "<":
                text, title = html_to_markdown(raw)
                return WebDoc(url=url, title=title, text=text, content_type=content_type, is_html=True)
            return WebDoc(url=url, text=raw, content_type=content_type, is_html=False)
        except WebDocError as exc:
            last_error = str(exc)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
    raise WebDocError(f"抓取 {url} 失败（重试 {retries} 次）：{last_error}")
