"""项目白名单命令执行：默认进沙箱，宿主执行必须显式开启。

安全边界：
- 命令只能来自项目 settings / target_start，不接受 MCP 任意 command；
- 默认 ``sandbox``：berth 模式下进 berth；local 沙箱没有隔离，默认拒绝；
- ``host`` 模式必须同时设置 ``settings.automation.command_execution = "host"``
  与 ``settings.automation.allow_host_commands = true``，结果会标记 ``execution=host``；
- 返回值始终带 ``execution`` / ``sandboxed``，避免把宿主执行误报成沙箱执行。
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any

from app.logging import get_logger
from app.models import Project
from app.sandbox import LocalSandbox, get_sandbox

logger = get_logger("shakedown.command_runner")

EXECUTION_MODES = {"sandbox", "host"}


def _automation(project: Project) -> dict[str, Any]:
    value = (project.settings_json or {}).get("automation")
    return value if isinstance(value, dict) else {}


def command_execution_mode(project: Project) -> str:
    automation = _automation(project)
    raw = str(automation.get("command_execution") or "sandbox").strip().lower()
    return raw if raw in EXECUTION_MODES else "sandbox"


def _denied(reason: str, *, started: float, mode: str) -> dict[str, Any]:
    return {
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": reason,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "execution": "denied",
        "sandboxed": False,
        "requested_mode": mode,
    }


def run_project_command(
    project: Project,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str = "",
    timeout: float = 120.0,
    task_id: str = "",
    purpose: str = "command",
) -> dict[str, Any]:
    """按项目策略执行一条白名单命令，并返回统一结果。"""
    started = time.perf_counter()
    mode = command_execution_mode(project)
    automation = _automation(project)
    command = [str(item) for item in args if str(item)]
    if not command:
        return _denied("命令为空", started=started, mode=mode)

    if mode == "host":
        if automation.get("allow_host_commands") is not True:
            return _denied(
                "宿主执行未开启：需要 settings.automation.command_execution=host 且 allow_host_commands=true",
                started=started,
                mode=mode,
            )
        full_env = dict(os.environ)
        full_env.update({str(k): str(v) for k, v in (env or {}).items()})
        try:
            proc = subprocess.run(
                command,
                cwd=cwd or None,
                env=full_env,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("宿主命令失败：project=%s purpose=%s error=%s", project.id, purpose, exc)
            return {
                "ok": False,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "execution": "host",
                "sandboxed": False,
                "requested_mode": mode,
            }
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-8000:],
            "stderr": (proc.stderr or "")[-8000:],
            "error": "",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "execution": "host",
            "sandboxed": False,
            "requested_mode": mode,
        }

    sandbox = get_sandbox()
    if isinstance(sandbox, LocalSandbox) and automation.get("allow_local_sandbox_commands") is not True:
        return _denied(
            "local 沙箱没有 cgroup / 网络隔离，默认拒绝执行项目命令；"
            "请使用 SHAKEDOWN_SANDBOX_MODE=berth，或显式开启 automation.allow_local_sandbox_commands",
            started=started,
            mode=mode,
        )
    result = sandbox.run_command(
        command,
        env={str(k): str(v) for k, v in (env or {}).items()},
        cwd=cwd,
        timeout=max(1, int(timeout)),
        task_id=task_id,
    )
    execution = str((result.raw or {}).get("execution") or sandbox.name)
    return {
        "ok": bool(result.ok),
        "exit_code": result.exit_code,
        "stdout": (result.stdout or "")[-8000:],
        "stderr": (result.stderr or "")[-8000:],
        "error": result.error,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "execution": execution,
        "sandboxed": execution == "berth",
        "requested_mode": mode,
        "sandbox_task_id": result.task_id,
    }
