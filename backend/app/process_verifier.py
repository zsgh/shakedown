"""项目配置的白名单验证命令：进程 / 权限 / 构建产物检查可由 Shakedown 独立执行。

命令只能来自项目 settings，不接受 MCP 任意 command；默认通过 command_runner 进沙箱，
宿主 / local 执行需要项目显式 opt-in，结果会标注 execution / sandboxed。
"""
from __future__ import annotations

import shlex
from typing import Any

from sqlalchemy.orm import Session

from app.command_runner import run_project_command
from app.models import CaseVerification, Project, TestCase
from app.verification_evidence import current_env_fingerprint, default_expiry


def verification_commands(project: Project) -> list[dict[str, Any]]:
    raw = (project.settings_json or {}).get("verification_commands")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"command-{index}")
        result.append(
            {
                "name": name,
                "description": str(item.get("description") or ""),
                "mode": str(item.get("mode") or "process"),
            }
        )
    return result


def _command(project: Project, name: str) -> dict[str, Any] | None:
    raw = (project.settings_json or {}).get("verification_commands")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return item
    return None


def run_verification_command(
    session: Session,
    project: Project,
    name: str,
    *,
    case_id: int | None = None,
) -> dict[str, Any]:
    spec = _command(project, name)
    if spec is None:
        raise ValueError(f"验证命令不存在：{name}")
    command = spec.get("command") or spec.get("cmd")
    args = [str(item) for item in command] if isinstance(command, list) else shlex.split(str(command or ""))
    if not args:
        raise ValueError(f"验证命令为空：{name}")
    env = {}
    extra = spec.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    cwd = str(spec.get("cwd") or "").strip()
    timeout = float(spec.get("timeout", 120))
    outcome = run_project_command(
        project,
        args,
        env=env,
        cwd=cwd,
        timeout=timeout,
        task_id=f"verify-{project.id}-{name}",
        purpose=f"verification:{name}",
    )
    passed = bool(outcome["ok"])
    stdout = outcome["stdout"]
    stderr = outcome["stderr"]
    error = outcome["error"]
    result = {
        "name": name,
        "passed": passed,
        "exit_code": outcome["exit_code"],
        "stdout": stdout,
        "stderr": stderr,
        "error": error,
        "duration_ms": outcome["duration_ms"],
        "execution": outcome["execution"],
        "sandboxed": outcome["sandboxed"],
    }

    if case_id is not None:
        case = session.get(TestCase, case_id)
        if case is None or case.project_id != project.id:
            raise ValueError(f"用例不存在或不属于当前项目：{case_id}")
        record = CaseVerification(
            project_id=project.id,
            case_id=case.id,
            finding_id=None,
            mode=str(spec.get("mode") or "process"),
            status="passed_external" if passed else "failed_external",
            actor="shakedown",
            source="auto",
            command=" ".join(args),
            result=(stdout or stderr or error)[-4000:],
            artifacts=[],
            note=f"白名单验证命令 {name}（execution={outcome['execution']}, sandboxed={outcome['sandboxed']}）",
            evidence_level="replayed",
            commit_sha="",
            artifact_hashes={},
            env_fingerprint=current_env_fingerprint(session, project),
            expires_at=default_expiry() if passed else None,
        )
        session.add(record)
        session.flush()
        result["verification_id"] = record.id
    return result
