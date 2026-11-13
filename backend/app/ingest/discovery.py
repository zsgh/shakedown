"""仓库自动发现：给一个目录路径，自动找出里面有什么可以导入。

用户不该需要知道「代码库填这里、文档填那里、spec 填这里」——
给一个仓库路径，就应该一次把能用的东西都识别出来。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.ingest.code import CODE_EXTENSIONS, SKIP_DIRS
from app.logging import get_logger

logger = get_logger("shakedown.ingest.discovery")

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}
SPEC_EXTENSIONS = {".json", ".yaml", ".yml"}
MAX_SPEC_SIZE = 8 * 1024 * 1024
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "composer.lock"}


@dataclass
class Discovery:
    """一次仓库扫描的结果。"""

    path: str = ""
    is_git_repo: bool = False
    commit: str = ""
    code_files: list[str] = field(default_factory=list)
    markdown_files: list[str] = field(default_factory=list)
    openapi_files: list[str] = field(default_factory=list)
    api_doc_files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "is_git_repo": self.is_git_repo,
            "commit": self.commit,
            "code": {"count": len(self.code_files), "files": self.code_files[:50]},
            "markdown": {"count": len(self.markdown_files), "files": self.markdown_files[:50]},
            "openapi": {"count": len(self.openapi_files), "files": self.openapi_files[:20]},
            "api_docs": {"count": len(self.api_doc_files), "files": self.api_doc_files[:20]},
            "notes": self.notes,
        }


def _is_spec_file(path: Path) -> bool:
    """判断一个 json/yaml 文件是不是 OpenAPI / Swagger 文档。"""
    try:
        if path.stat().st_size > MAX_SPEC_SIZE:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head = text[:4096]
    if "openapi" in head or "swagger" in head:
        return True
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data)


def _has_api_section(path: Path) -> bool:
    """粗判：这个 md 里是否有 API 端点清单。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head = text[:20000].lower()
    return any(
        keyword in head
        for keyword in ("http api", "api 设计", "接口清单", "端点", "endpoints", "接口一览", "路由表")
    )


def discover_repository(path: str, *, max_files: int = 20000) -> Discovery:
    """扫描一个目录，返回可导入的东西。"""
    root = Path(path).expanduser()
    result = Discovery(path=str(root))
    if not root.exists():
        raise FileNotFoundError(f"路径不存在：{root}")
    if root.is_file():
        root = root.parent
        result.path = str(root)
    root = root.resolve()
    result.is_git_repo = (root / ".git").exists()
    if result.is_git_repo:
        from app.ingest.code import repo_version

        result.commit = repo_version(root)

    scanned = 0
    for current, dirs, files in __import__("os").walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            scanned += 1
            if scanned > max_files:
                result.notes.append("文件过多，只扫描了前一部分")
                break
            file_path = Path(current) / name
            try:
                rel = str(file_path.relative_to(root))
            except ValueError:
                continue
            suffix = file_path.suffix.lower()
            if suffix in MARKDOWN_EXTENSIONS:
                result.markdown_files.append(rel)
                if _has_api_section(file_path):
                    result.api_doc_files.append(rel)
            elif suffix in CODE_EXTENSIONS:
                result.code_files.append(rel)
            elif suffix in SPEC_EXTENSIONS and name not in SKIP_FILES:
                if _is_spec_file(file_path):
                    result.openapi_files.append(rel)
        if scanned > max_files:
            break

    if result.code_files:
        result.notes.append(f"检测到 {len(result.code_files)} 个代码文件 → 可作为「代码库」导入")
    if result.markdown_files:
        result.notes.append(f"检测到 {len(result.markdown_files)} 个 Markdown 文档 → 可作为「Markdown 文档」导入")
    if result.api_doc_files:
        result.notes.append(
            "以下文档里含接口清单（会自动解析成接口）：" + "、".join(result.api_doc_files[:5])
        )
    if result.openapi_files:
        result.notes.append(f"检测到 {len(result.openapi_files)} 个 OpenAPI/Swagger 文件 → 可作为「OpenAPI」导入")
    if not (result.code_files or result.markdown_files or result.openapi_files):
        result.notes.append("没找到可导入的内容（代码 / Markdown / OpenAPI）")
    return result
