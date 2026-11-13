"""知识源解析：Markdown / 代码库 / OpenAPI + 一致性对照。"""
from __future__ import annotations

from app.ingest.code import chunk_code_file, extract_routes, iter_source_files
from app.ingest.consistency import check_consistency, extract_doc_mentions, normalize_path
from app.ingest.embeddings import Embedder, hash_embedding
from app.ingest.markdown import split_markdown
from app.ingest.openapi import (
    NormalizedEndpoint,
    OpenAPIError,
    content_hash,
    diff_specs,
    load_spec,
    normalize,
    resolve_refs,
    validate_spec,
)
from app.ingest.service import ingest_source, recompute_consistency, upsert_endpoint

__all__ = [
    "Embedder",
    "NormalizedEndpoint",
    "OpenAPIError",
    "check_consistency",
    "chunk_code_file",
    "content_hash",
    "diff_specs",
    "extract_doc_mentions",
    "extract_routes",
    "hash_embedding",
    "ingest_source",
    "iter_source_files",
    "load_spec",
    "normalize",
    "normalize_path",
    "recompute_consistency",
    "resolve_refs",
    "split_markdown",
    "upsert_endpoint",
    "validate_spec",
]
