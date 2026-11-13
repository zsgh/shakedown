"""Agent 从「人能看懂的文档」里抽接口。

非标准接口文档（wiki 页面、手写说明、聊天记录里的接口表）没有固定格式，
纯规则抽不全；这里让 LLM 像人一样读文档，产出接口清单，并保留原文引用作为依据。

离线 provider 用宽松的规则抽取兜底，保证没有模型时也能工作。
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.providers.base import LLMError, LLMProvider
from app.ingest.docapi import extract_doc_endpoints
from app.logging import get_logger

logger = get_logger("shakedown.agent.docextract")

DOC_ENDPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "summary": {"type": "string"},
                    "params": {"type": "array"},
                    "evidence": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "quote": {"type": "string"},
                        },
                    },
                },
                "required": ["method", "path"],
            },
        }
    },
    "required": ["endpoints"],
}

SYSTEM_PROMPT = """你是接口文档分析专家。给你若干段项目文档片段（可能是 wiki、手写说明、表格、代码块），
请把里面描述的 HTTP 接口都抽出来。

要求：
1. method 用 GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS；path 从原文里抄，不要自己编；
2. summary 用一句话说明这个接口干什么（依据原文）；
3. evidence.quote 必须是原文里的一段话（用来追溯），evidence.ref 写文档位置；
4. 只输出 JSON，格式：{"endpoints": [{"method": "...", "path": "...", "summary": "...", "evidence": {"ref": "...", "quote": "..."}}]}
5. 原文里没有的接口不要输出；不确定的不要输出。
"""

# 宽松匹配：正文里任何位置的 "METHOD /path"
_INLINE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b[ \t]*`?[ \t]*(/[A-Za-z0-9_\-{}/.:<>]*)",
    re.IGNORECASE,
)


def _normalize_path(path: str) -> str:
    text = path.strip().strip("`").rstrip(".,;:)")
    if "?" in text:
        text = text.split("?", 1)[0]
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"<([^>]+)>", r"{\1}", text)
    return text


def rule_based_extraction(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """离线兜底：宽松匹配正文里的 METHOD + path。

    先跑「端点清单」解析（表格 / 代码块，摘要更准），再用宽松正则补齐散落在正文里的提及。
    """
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # 1) 表格 / 代码块里的端点清单（带说明）
    for endpoint in extract_doc_endpoints(chunks):
        key = (endpoint.method, endpoint.path)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "method": endpoint.method,
                "path": endpoint.path,
                "summary": endpoint.summary,
                "evidence": {
                    "ref": f"{endpoint.locator.get('file_path', '')}:{endpoint.locator.get('line', '')}",
                    "quote": endpoint.summary or endpoint.path,
                },
            }
        )

    # 2) 正文里散落的提及
    for chunk in chunks:
        content = chunk.get("content") or ""
        locator = chunk.get("locator") or {}
        ref = locator.get("file_path") or ""
        for match in _INLINE.finditer(content):
            method = match.group(1).upper()
            path = _normalize_path(match.group(2))
            if path == "/" or len(path) < 2:
                continue
            last = path.rstrip("/").split("/")[-1]
            if last.startswith(".") or (("." in last) and last.rsplit(".", 1)[-1].isalpha()):
                continue
            # 排除 "GET / POST / PUT" 这种把方法名当路径的误报
            segments = [s for s in path.split("/") if s]
            if segments and all(s.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"} for s in segments):
                continue
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "method": method,
                    "path": path,
                    "summary": "",
                    "evidence": {
                        "ref": ref,
                        "quote": content[match.start() : match.end() + 80].strip()[:200],
                    },
                }
            )
    return results


def extract_endpoints_with_agent(
    provider: LLMProvider,
    chunks: list[dict[str, Any]],
    *,
    max_chunks: int = 24,
    max_chars: int = 40000,
    shrink: int = 1,
) -> list[dict[str, Any]]:
    """让 Agent 读文档抽接口（失败时退回规则抽取）。

    ``shrink`` 用于重试时缩小素材量（推理模型输出被截断时很有用）。
    """
    if not chunks:
        return []

    # 控制素材体积：先按块数，再按总字符数
    budget = max(4000, max_chars // max(1, shrink))
    material: list[dict[str, Any]] = []
    used = 0
    for chunk in chunks[: max(1, max_chunks // max(1, shrink))]:
        content = (chunk.get("content") or "")[:4000]
        if used + len(content) > budget and material:
            break
        material.append(
            {
                "ref": (chunk.get("locator") or {}).get("file_path") or "",
                "section": " > ".join((chunk.get("locator") or {}).get("section_path") or []),
                "content": content,
            }
        )
        used += len(content)

    try:
        data = provider.structured(
            SYSTEM_PROMPT,
            "请从下面这些文档片段里抽出接口清单。",
            DOC_ENDPOINT_SCHEMA,
            context={"task": "doc_endpoint_extraction", "chunks": material},
        )
        endpoints = data.get("endpoints") or []
        cleaned: list[dict[str, Any]] = []
        for item in endpoints:
            method = str(item.get("method") or "").upper()
            path = _normalize_path(str(item.get("path") or ""))
            if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
                continue
            if not path.startswith("/") or path == "/":
                continue
            cleaned.append(
                {
                    "method": method,
                    "path": path,
                    "summary": str(item.get("summary") or "")[:300],
                    "evidence": item.get("evidence") or {},
                }
            )
        if cleaned:
            return cleaned
        logger.info("Agent 未从文档中抽到接口，退回规则抽取")
    except LLMError as exc:
        logger.warning("Agent 抽接口失败（%s），退回规则抽取", exc)
    return rule_based_extraction(chunks)
