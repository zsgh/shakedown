"""沙箱抽象：执行器只依赖这里的接口，不直接知道 berth 的存在。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class SandboxError(RuntimeError):
    """沙箱不可用 / 执行失败。"""


@dataclass
class SandboxRequest:
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 600
    task_id: str = ""
    image: str = "base"
    memory_mb: int = 1024
    cpu_percent: int = 100
    workdir: str = "/workspace"
    # 通用命令执行（项目白名单验证 / 环境生命周期）；与 files 二选一。
    command: list[str] | None = None
    cwd: str = ""
    # 轮询取消：返回 True 时尽快终止执行（本地沙箱会 kill 子进程）。
    should_cancel: Callable[[], bool] | None = None


@dataclass
class SandboxResult:
    ok: bool = False
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = field(default_factory=list)
    task_id: str = ""
    duration: float = 0.0
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class SandboxBackend(ABC):
    """沙箱后端。"""

    name: str = "sandbox"

    @abstractmethod
    def run(self, request: SandboxRequest) -> SandboxResult:
        """执行一段脚本并返回结果。"""

    def run_command(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str = "",
        timeout: int = 120,
        task_id: str = "",
    ) -> SandboxResult:
        """执行项目白名单命令。默认走 ``run``，后端可覆写。"""
        return self.run(
            SandboxRequest(
                command=[str(item) for item in command],
                env=env or {},
                cwd=cwd,
                timeout=max(1, int(timeout)),
                task_id=task_id,
                image="base",
            )
        )

    def health(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True}
