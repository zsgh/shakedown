"""被测环境生命周期：start / stop / readiness / 每次 run 的数据准备。

默认不执行任何命令；只有项目显式设置
``settings.automation.allow_target_commands = true`` 且 target_start 配置了
相应命令时才执行。命令由项目维护者配置，不接受 MCP 任意提交。
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
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if isinstance(command, list):
        args = [str(item) for item in command]
    else:
        args = shlex.split(str(command or ""))
    if not args:
        return {"ok": False, "error": "命令为空", "execution": "denied", "sandboxed": False}
    env = {}
    extra_env_values = target.get("env")
    if isinstance(extra_env_values, dict):
        env.update({str(k): str(v) for k, v in extra_env_values.items()})
    if extra_env:
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


def _run_init_script(project: Project, script: str, dsn: str) -> dict[str, Any]:
    """执行项目配置的初始化 SQL；只在 allow_execute 打开时运行。"""
    from pathlib import Path as _Path

    path = _Path(str(script)).expanduser()
    if not path.is_file():
        return {"ok": False, "error": f"初始化脚本不存在：{path}", "path": str(path)}
    if not dsn.lower().startswith("postgresql"):
        return {"ok": False, "error": "init_script 目前只支持 PostgreSQL DSN", "path": str(path)}
    try:
        import psycopg  # type: ignore

        sql = path.read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)
        return {"ok": True, "path": str(path), "bytes": len(sql)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "path": str(path)}


def prepare_data(project: Project, *, dsn: str = "") -> dict[str, Any]:
    """每次 run 前执行 migrate / seed / account 准备命令。

    与 start_environment 分开，避免把“启动 SUT”误当成“准备每次 run 的数据”。
    ``dsn`` 会以常见环境变量名注入命令，方便 seed 脚本连接本次 run 的库。
    """
    target = _target_start(project)
    if not target:
        return {"prepared": False, "reason": "target_start 未配置", "steps": []}
    if not _command_allowed(project, target):
        return {
            "prepared": False,
            "reason": "未允许执行数据准备命令（需要 allow_execute + automation.allow_target_commands）",
            "steps": [],
        }
    commands = [name for name in ("migrate_command", "seed_command", "account_command") if target.get(name)]
    init_script = str((project.isolation or {}).get("init_script") or "").strip()
    if not commands and not init_script:
        return {"prepared": False, "reason": "未配置 migrate/seed/account/init_script", "steps": []}
    extra_env = {
        "SHAKEDOWN_PROJECT_ID": str(project.id),
        "SHAKEDOWN_TARGET_DSN": dsn,
        "TARGET_DSN": dsn,
        "DATABASE_URL": dsn,
    }
    steps: list[dict[str, Any]] = []
    if init_script:
        steps.append({"name": "init_script", **_run_init_script(project, init_script, dsn)})
        if not steps[-1].get("ok"):
            return {"prepared": False, "steps": steps, "command_execution": command_execution_mode(project)}
    for name in commands:
        step = _run_command(
            project,
            target[name],
            target,
            extra_env=extra_env,
        )
        steps.append({"name": name.replace("_command", ""), **step})
        if not step.get("ok"):
            break
    return {
        "prepared": all(step.get("ok") for step in steps) and bool(steps),
        "steps": steps,
        "command_execution": command_execution_mode(project),
    }


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
        data = prepare_data(project)
        steps.extend(data.get("steps") or [])
    logger.info("环境启动：project=%s ok=%s ready=%s", project.id, result.get("ok"), readiness.get("ok"))
    return {
        "started": bool(result.get("ok")),
        "command": result,
        "steps": steps,
        "readiness": readiness,
    }


def stop_environment(project: Project) -> dict[str, Any]:
    target = _target_start(project)
    if not target:
        return {"stopped": False, "reason": "target_start 未配置"}
    if not _command_allowed(project, target):
        return {"stopped": False, "reason": "未允许执行 target_start 命令"}
    external = str(target.get("type") or "external") == "external"
    steps: list[dict[str, Any]] = []
    if not external:
        command = target.get("stop_command")
        if not command:
            return {"stopped": False, "reason": "未配置 stop_command"}
        result = _run_command(project, command, target)
        steps.append({"name": "stop", **result})
        if not result.get("ok"):
            return {"stopped": False, "command": result, "steps": steps}
    if target.get("cleanup_command"):
        cleanup = _run_command(project, target["cleanup_command"], target)
        steps.append({"name": "cleanup", **cleanup})
    return {
        "stopped": not external,
        "cleaned": all(step.get("ok") for step in steps if step.get("name") == "cleanup"),
        "steps": steps,
        "reason": "external 环境不停止，只执行 cleanup_command" if external else "",
    }


def environment_status(project: Project) -> dict[str, Any]:
    target = _target_start(project)
    return {
        "type": str(target.get("type") or "external"),
        "auto_start": bool(target.get("auto_start", False)),
        "auto_stop": bool(target.get("auto_stop", False)),
        "commands_allowed": _command_allowed(project, target) if target else False,
        "command_execution": command_execution_mode(project),
        "allow_host_commands": _automation(project).get("allow_host_commands") is True,
        "has_data_commands": any(
            target.get(name) for name in ("migrate_command", "seed_command", "account_command")
        ),
        "readiness": check_target(project),
    }
