"""沙箱后端选择。"""
from __future__ import annotations

from app.config import get_settings
from app.sandbox.base import SandboxBackend, SandboxError, SandboxRequest, SandboxResult
from app.sandbox.berth import BerthClient, BerthError, BerthSandbox
from app.sandbox.local import LocalSandbox

__all__ = [
    "BerthClient",
    "BerthError",
    "BerthSandbox",
    "LocalSandbox",
    "SandboxBackend",
    "SandboxError",
    "SandboxRequest",
    "SandboxResult",
    "get_sandbox",
]


def get_sandbox(mode: str | None = None) -> SandboxBackend:
    settings = get_settings()
    mode = mode or settings.sandbox_mode
    if mode == "berth":
        return BerthSandbox()
    return LocalSandbox()
