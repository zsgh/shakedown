"""CLI 契约目录：把 `-h` / 代码里的 flag 变成可复用的命令目录与固定模板。

设计：
- Agent 先探测 CLI help，生成目录（命令、参数、预期输出），存进项目 profile；
- 用例生成只允许「目录里的命令 + 平台固定模板」，不再让 LLM 自由拼 shell；
- 复杂场景（备份/恢复/重置保护）用模板，模板本身可审计、可复用。
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import collect_assertions
from app.models import Project, TestCase
from app.onboarding import _exec_shell

TEMPLATES: dict[str, dict[str, Any]] = {
    "version": {"keywords": ("version", "版本"), "args": ["-version"]},
    "backup": {"keywords": ("backup", "备份"), "args": ["-backup"]},
    "restore": {"keywords": ("restore", "恢复"), "args": ["-restore"]},
    "reset": {"keywords": ("reset-password", "重置", "口令"), "args": ["-reset-password", "admin"]},
}


def _runner(project: Project) -> list[str]:
    settings = project.settings_json or {}
    adapter = settings.get("exec_adapter") if isinstance(settings.get("exec_adapter"), dict) else {}
    if adapter.get("type") == "lima":
        return ["limactl", "shell", str(adapter.get("instance") or "debian12"), "--"]
    return []


def _cli(project: Project) -> dict[str, Any]:
    settings = project.settings_json or {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    return {
        "binary": str((profile.get("environment") or {}).get("cli_binary") or settings.get("cli_binary") or ""),
        "setup": str(settings.get("cli_setup_script") or ""),
        "config": str(settings.get("cli_config_path") or ""),
        "data": str(settings.get("cli_data_dir") or ""),
        "runner": _runner(project),
    }


def extract_contract(project: Project) -> dict[str, Any]:
    cli = _cli(project)
    binary = cli["binary"]
    if not binary:
        return {"error": "profile 里没有 cli.binary，先跑 onboarding"}
    outcome = _exec_shell(project, f"{binary} -h 2>&1 || true", timeout=20)
    text = (outcome.get("stdout") or "") + (outcome.get("stderr") or "")
    flags: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    for line in text.splitlines():
        match = re.match(r"\s+-([A-Za-z0-9-]+)(?:\s+([A-Za-z0-9_.-]+))?\s*$", line)
        if match:
            pending = {"name": match.group(1), "type": match.group(2) or "bool", "description": ""}
            flags.append(pending)
            continue
        if pending is not None and line.strip() and not line.startswith("Usage"):
            pending["description"] = (pending["description"] + " " + line.strip()).strip()[:200]
    names = {flag["name"] for flag in flags}
    commands: list[dict[str, Any]] = []
    for template, spec in TEMPLATES.items():
        flag = spec["args"][0].lstrip("-")
        if flag not in names and template != "version":
            continue
        commands.append(
            {
                "name": template,
                "args": list(spec["args"]),
                "exit_code": 0,
                "stdout_contains": {
                    "version": "dm-git",
                    "backup": "备份完成",
                    "restore": "已恢复",
                    "reset": "已重置",
                }.get(template, ""),
                "description": next(
                    (flag_item["description"] for flag_item in flags if flag_item["name"] == flag),
                    "",
                ),
            }
        )
    contract = {
        "binary": binary,
        "runner": cli["runner"],
        "setup": cli["setup"],
        "config": cli["config"],
        "data": cli["data"],
        "flags": flags,
        "commands": commands,
        "help_excerpt": text[:3000],
    }
    settings = dict(project.settings_json or {})
    settings["cli_contract"] = contract
    project.settings_json = settings
    return contract


def _template_step(template: str, cli: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    binary = cli["binary"]
    setup = cli["setup"]
    config = cli["config"]
    data = cli["data"]
    runner = cli["runner"]

    def shell(script: str) -> list[str]:
        return [*runner, "bash", "-lc", script]

    def direct(args: list[str]) -> list[str]:
        return [*runner, binary, *args]

    def assertion(kind: str, expected: Any, ref: str, description: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "operator": "contains" if kind in {"stdout_contains", "stderr_contains"} else "eq",
            "expected": expected,
            "status": "confirmed",
            "evidence": {"source_type": "code", "ref": ref},
            "description": description,
        }

    if template == "version":
        return (
            [{"action": "command", "command": direct(["-version"]), "timeout_ms": 60000,
              "assert": [assertion("exit_code", 0, "cmd/dm-git/main.go", "版本命令成功退出"),
                         assertion("stdout_contains", "dm-git", "cmd/dm-git/main.go", "输出版本标识")]}],
            [],
        )
    if template == "backup":
        script = f"{setup} >/dev/null && {binary} -config {config} -backup"
        return (
            [{"action": "command", "command": shell(script), "timeout_ms": 120000,
              "assert": [assertion("exit_code", 0, "cmd/dm-git/main.go", "备份成功退出"),
                         assertion("stdout_contains", "备份完成", "internal/backup", "输出备份完成")]}],
            [],
        )
    if template == "restore":
        script = (
            f"{setup} >/dev/null && {binary} -config {config} -backup >/dev/null && "
            f"A=$(ls -t {data.rsplit('/', 1)[0]}/backups/*.tar.gz 2>/dev/null | head -1) && "
            f"rm -rf /tmp/dmgit-restore && mkdir -p /tmp/dmgit-restore && "
            f"python3 -c 'import json; d=json.load(open(\"{config}\")); d[\"dataDir\"]=\"/tmp/dmgit-restore\"; "
            f"json.dump(d,open(\"/tmp/dmgit-restore-config.json\",\"w\"))' && "
            f"{binary} -config /tmp/dmgit-restore-config.json -restore \"$A\" -force"
        )
        return (
            [{"action": "command", "command": shell(script), "timeout_ms": 120000,
              "assert": [assertion("exit_code", 0, "cmd/dm-git/main.go", "恢复成功退出"),
                         assertion("stdout_contains", "已恢复", "internal/backup", "输出已恢复")]}],
            [],
        )
    if template == "reset":
        script = f"{setup} >/dev/null && DMGIT_ADMIN_PASSWORD=NewPass123 {binary} -config {config} -reset-password admin"
        return (
            [{"action": "command", "command": shell(script), "timeout_ms": 120000,
              "assert": [assertion("exit_code", 0, "cmd/dm-git/main.go", "重置成功退出"),
                         assertion("stdout_contains", "已重置", "cmd/dm-git/main.go", "输出已重置")]}],
            [],
        )
    raise ValueError(f"未知 CLI 模板：{template}")


def _classify(title: str) -> str:
    text = title.lower()
    # 只有明确是 CLI 的才映射，页面按钮 / 性能 / 定时 / 下载 / 配置类不硬套
    if any(word in title for word in ("运行中", "页面", "下载", "定时", "配置", "性能")):
        return ""
    if "reset-password" in text or "命令行重置" in title:
        return "reset"
    if "-restore" in text or "从备份文件恢复" in title:
        return "restore"
    if "-backup" in text or ("cli" in text and "备份" in title):
        return "backup"
    if "version" in text or "版本" in title:
        return "version"
    return ""


def generate_from_contract(
    session: Session,
    project: Project,
    *,
    limit: int = 20,
    case_ids: list[int] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    contract = (project.settings_json or {}).get("cli_contract")
    if not isinstance(contract, dict) or not contract.get("commands"):
        raise ValueError("还没有 CLI 契约目录，先调用 cli-contract")
    cli = _cli(project)
    stmt = select(TestCase).where(
        TestCase.project_id == project.id,
        TestCase.status == "pending",
    )
    if case_ids:
        stmt = stmt.where(TestCase.id.in_(case_ids))
    cases = list(session.scalars(stmt.order_by(TestCase.priority, TestCase.id)).all())
    mapped: list[tuple[TestCase, str]] = []
    for case in cases:
        if case.steps:
            continue
        template = _classify(case.title)
        if not template:
            continue
        if not any(command["name"] == template for command in contract["commands"]):
            continue
        mapped.append((case, template))
        if len(mapped) >= limit:
            break
    if dry_run:
        return {
            "project_id": project.id,
            "count": len(mapped),
            "items": [
                {"case_id": case.id, "template": template, "title": case.title}
                for case, template in mapped
            ],
        }
    for case, template in mapped:
        steps, top_assertions = _template_step(template, cli)
        spec = dict(case.spec or {})
        spec["steps"] = steps
        spec["assertions"] = top_assertions
        case.spec = spec
        case.steps = steps
        case.expected = {"assertions": collect_assertions(spec)}
        case.verification_mode = "process"
        case.tags = sorted(set((case.tags or []) + ["cli", f"cli-{template}"]))
    session.flush()
    return {
        "project_id": project.id,
        "updated": len(mapped),
        "case_ids": [case.id for case, _ in mapped],
    }


__all__ = ["extract_contract", "generate_from_contract"]
