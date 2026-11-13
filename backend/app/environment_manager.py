"""被测环境生命周期：start / stop / readiness。

默认不执行任何命令；只有项目显式设置
``settings.automation.allow_target_commands = true`` 且 target_start 配置了
start_command/stop_command 时才执行。命令由项目维护者配置，不接受 MCP 任意提交。
命令默认进沙箱；宿主执行需要 ``command_execution=host`` +
``allow_host_commands=true``，local 沙箱需显式 ``allow_local_sandbox_commands``。
"""
from __future__ import annotations

import shlex
import time
from typing import Any

from app.command_runner import command_execution_mode, run_project_command
from app.environment_check import check_target
from app.logging import get_logger
from app.models import Project

logger = get_logger("shakedown.environment_manager")


def _automation(project: Project) -> dict[str, Any]:
    value = (project.settings_json or {}).get("automation")
    return value if isinstance(value, dict) else {}


def _target_start(project: Project) -> dict[str, Any]:
    value = project.target_start or {}
    return value if isinstance(value, dict) else {}


def _command_allowed(project: Project, target: dict[str, Any]) -> bool:
    return bool(
        target.get("allow_execute")
        and _automation(project).get("allow_target_commands", False)
    )


def _run_command(
    project: Project,
    command: Any,
    target: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    if isinstance(command, list):
        args = [str(item) for item in command]
    else:
        args = shlex.split(str(command or ""))
    if not args:
        return {"ok": False, "error": "命令为空", "execution": "denied", "sandboxed": False}
    env = {}
    extra_env = target.get("env")
    if isinstance(extra_env, dict):
        env.update({str(k): str(v) for k, v in extra_env.items()})
    cwd = str(target.get("cwd") or "").strip()
    limit = float(timeout if timeout is not None else target.get("timeout", 60))
    return run_project_command(
        project,
        args,
        env=env,
        cwd=cwd,
        timeout=limit,
        task_id=f"environment-{project.id}",
        purpose="environment",
    )


def _wait_ready(project: Project, *, wait_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    result = check_target(project)
    while not result.get("ok") and time.monotonic() < deadline:
        time.sleep(1.0)
        result = check_target(project)
    return result


def start_environment(project: Project) -> dict[str, Any]:
    target = _target_start(project)
    if not target:
        return {"started": False, "reason": "target_start 未配置", "readiness": check_target(project)}
    if str(target.get("type") or "external") == "external":
        return {"started": False, "reason": "external 环境不由 Shakedown 启动", "readiness": check_target(project)}
    if not _command_allowed(project, target):
        return {
            "started": False,
            "reason": "未允许执行 target_start 命令（需要 allow_execute + automation.allow_target_commands）",
            "readiness": check_target(project),
        }
    command = target.get("start_command") or target.get("command")
    if not command:
        return {"started": False, "reason": "未配置 start_command", "readiness": check_target(project)}
    result = _run_command(project, command, target)
    steps = [{"name": "start", **result}]
    readiness = _wait_ready(project, wait_seconds=float(target.get("wait_seconds", 30)))
    if readiness.get("ok"):
        for name in ("migrate_command", "seed_command", "account_command"):
            if not target.get(name):
                continue
            step = _run_command(project, target[name], target)
            steps.append({"name": name.replace("_command", ""), **step})
            if not step.get("ok"):
                break
    logger.info("环境启动：project=%s ok=%s ready=%s", project.id, result.get("ok"), readiness.get("ok"))
    return {
        "started": bool(result.get("ok")),
        "command": result,
        "steps": steps,
        "readiness": readiness,
    }


def stop_environment(project: Project) -> dict[str, Any]:
    target = _target_start(project)
    if not target or str(target.get("type") or "external") == "external":
        return {"stopped": False, "reason": "external 环境不由 Shakedown 停止"}
    if not _command_allowed(project, target):
        return {"stopped": False, "reason": "未允许执行 target_start 命令"}
    command = target.get("stop_command")
    if not command:
        return {"stopped": False, "reason": "未配置 stop_command"}
    result = _run_command(project, command, target)
    steps = [{"name": "stop", **result}]
    if result.get("ok") and target.get("cleanup_command"):
        cleanup = _run_command(project, target["cleanup_command"], target)
        steps.append({"name": "cleanup", **cleanup})
    return {"stopped": bool(result.get("ok")), "command": result, "steps": steps}


def environment_status(project: Project) -> dict[str, Any]:
    target = _target_start(project)
    return {
        "type": str(target.get("type") or "external"),
        "auto_start": bool(target.get("auto_start", False)),
        "auto_stop": bool(target.get("auto_stop", False)),
        "commands_allowed": _command_allowed(project, target) if target else False,
        "command_execution": command_execution_mode(project),
        "allow_host_commands": _automation(project).get("allow_host_commands") is True,
        "readiness": check_target(project),
    }
