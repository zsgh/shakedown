"""构建产物独立校验：由 Shakedown 侧读取文件并计算 SHA-256。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.models import Project


def _root(project: Project) -> Path | None:
    root = str((project.settings_json or {}).get("artifact_root") or "").strip()
    if not root:
        return None
    path = Path(root).expanduser()
    return path.resolve() if path.exists() and path.is_dir() else None


def verify_artifacts(project: Project, paths: list[str]) -> dict[str, Any]:
    root = _root(project)
    if root is None:
        return {
            "configured": False,
            "root": "",
            "items": [],
            "error": "项目未配置 settings.artifact_root",
        }
    items: list[dict[str, Any]] = []
    for raw in paths[:200]:
        relative = str(raw or "").strip()
        if not relative:
            continue
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            items.append({"path": relative, "exists": False, "error": "路径越界"})
            continue
        if not candidate.is_file():
            items.append({"path": relative, "exists": False, "error": "文件不存在"})
            continue
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        items.append(
            {
                "path": relative,
                "exists": True,
                "sha256": digest.hexdigest(),
                "size": candidate.stat().st_size,
            }
        )
    return {"configured": True, "root": str(root), "items": items, "error": ""}
