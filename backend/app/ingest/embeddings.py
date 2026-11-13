"""向量检索支持：离线 hash embedding + OpenAI 兼容 embedding。"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

import httpx

from app.config import Settings, get_settings
from app.logging import get_logger

logger = get_logger("shakedown.ingest.embeddings")

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[一-鿿]")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def hash_embedding(text: str, dim: int = 1536) -> list[float]:
    """确定性离线 embedding：词哈希投影 + L2 归一化。

    质量不如真模型，但保证无网络也能跑通检索链路，且完全可复现。
    """
    vector = [0.0] * dim
    tokens = _tokens(text)
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
        # 第二个投影，降低碰撞影响
        index2 = int.from_bytes(digest[4:8], "big") % dim
        vector[index2] += sign * 0.5
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


class Embedder:
    """按配置返回 embedding 向量。"""

    def __init__(self, settings: Settings | None = None):
        if settings is None:
            from app.settings_store import effective_settings

            settings = effective_settings()
        self.settings = settings
        self.dim = self.settings.embedding_dim
        self.provider = self.settings.embedding_provider

    def embed(self, text: str) -> list[float]:
        if self.provider == "openai":
            try:
                return self._embed_openai(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("embedding 服务不可用，回退到 hash embedding：%s", exc)
        return hash_embedding(text, self.dim)

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]

    def _embed_openai(self, text: str) -> list[float]:
        base_url = (self.settings.embedding_base_url or self.settings.llm_base_url).rstrip("/")
        # 用户常把 LLM 地址填成完整 endpoint；embedding 要退回根路径。
        for suffix in ("/chat/completions", "/completions"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break
        headers = {"Content-Type": "application/json"}
        key = self.settings.embedding_api_key or self.settings.llm_api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {"model": self.settings.embedding_model, "input": text}
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{base_url}/embeddings", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        vector = data["data"][0]["embedding"]
        if len(vector) != self.dim:
            # 维度不一致时截断/补零，保证能写进 pgvector 列
            fixed = [0.0] * self.dim
            for i, value in enumerate(vector[: self.dim]):
                fixed[i] = float(value)
            return fixed
        return [float(v) for v in vector]
