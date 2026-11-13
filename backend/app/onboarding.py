"""Agent 原生 onboarding：只给地址 + admin 凭据，自己发现接口 / 鉴权 / 环境。

设计原则：
- LLM 负责「看懂这个系统」：从代码、文档、探测结果推断 profile；
- 平台负责「受控执行」：登录、建测试账号、建 token，都有超时 / 错误处理 / 审计；
- profile 是 Agent 的输出，不是人写的配置；
- 低置信度不阻塞，写进 notes/confidence，后续由人工或 repair 闭环补。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.agents import record_trace
from app.agent.providers import get_provider
from app.crypto import encrypt_json
from app.exceptions import record_exception
from app.logging import get_logger
from app.models import Chunk, Endpoint, Feature, Project

logger = get_logger("shakedown.onboarding")

TEST_USER = "shakedown-bot"
TEST_PASSWORD = "shakedown-bot-123"

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interfaces": {
            "type": "object",
            "properties": {
                "http": {"type": "boolean"},
                "cli": {"type": "boolean"},
                "browser": {"type": "boolean"},
            },
        },
        "auth": {
            "type": "object",
            "properties": {
                "login_path": {"type": "string"},
                "user_field": {"type": "string"},
                "password_field": {"type": "string"},
                "token_endpoint": {"type": "string"},
                "token_header": {"type": "string"},
                "token_verify_path": {"type": "string"},
                "admin_user_endpoint": {"type": "string"},
                "admin_user_method": {"type": "string"},
                "verify_path": {"type": "string"},
            },
        },
        "environment": {
            "type": "object",
            "properties": {
                "readiness_path": {"type": "string"},
                "cli_binary_hint": {"type": "string"},
                "cli_runner_hint": {"type": "string"},
            },
        },
        "sla": {
            "type": "object",
            "properties": {"performance_ms": {"type": "integer"}},
        },
        "confidence": {
            "type": "object",
            "properties": {
                "interfaces": {"type": "number"},
                "auth": {"type": "number"},
                "cli": {"type": "number"},
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["interfaces", "auth", "confidence"],
}

SYSTEM_PROMPT = """你是 Shakedown 的 onboarding agent。目标：只根据下面的探测结果、代码片段、端点清单，
推断这个被测系统该如何接入自动化测试。不要编造；看不出来的字段留空，并在 notes 说明。

重点推断：
1. 有 HTTP 接口吗？有 CLI 吗？需要浏览器吗？
2. 鉴权方式：登录表单路径/字段名、token 创建端点、token 头、会话 cookie、管理员建用户端点；
3. 环境 readiness 路径；
4. CLI 二进制/runner 的线索（只写代码或文档里出现的提示，不要编造路径）；
5. 合理的性能 SLA（从文档/配置找，找不到就给 200，并在 notes 说明是默认值）。

输出严格 JSON。"""


def _probe(base_url: str, path: str, *, timeout: float = 5.0) -> dict[str, Any]:
    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = client.get(url)
        body = response.text or ""
        return {
            "path": path,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "body": body[:1500],
        }
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "status": None, "error": f"{type(exc).__name__}: {exc}"}


def _code_evidence(session: Session, project: Project) -> list[dict[str, Any]]:
    rows = session.scalars(select(Chunk).where(Chunk.project_id == project.id)).all()
    keywords = ("login", "token", "flag", "backup", "restore", "reset-password", "password", "openapi")
    picked: list[dict[str, Any]] = []
    for row in rows:
        content = row.content or ""
        if not any(word in content.lower() for word in keywords):
            continue
        locator = row.locator or {}
        picked.append(
            {
                "file": locator.get("file_path") or "",
                "lines": f"{locator.get('start_line', '')}-{locator.get('end_line', '')}",
                "content": content[:1800],
            }
        )
        if len(picked) >= 8:
            break
    return picked


def collect_evidence(session: Session, project: Project) -> dict[str, Any]:
    base_url = project.base_url or ""
    probes = [
        _probe(base_url, "/"),
        _probe(base_url, "/api/health"),
        _probe(base_url, "/openapi.json"),
        _probe(base_url, "/docs"),
        _probe(base_url, "/login"),
    ]
    endpoints = [
        {"method": row.method, "path": row.path}
        for row in session.scalars(select(Endpoint).where(Endpoint.project_id == project.id)).all()
    ][:80]
    features = [
        row.name
        for row in session.scalars(select(Feature).where(Feature.project_id == project.id)).all()
    ][:30]
    return {
        "project": {"id": project.id, "name": project.name, "base_url": base_url},
        "probes": probes,
        "endpoints": endpoints,
        "features": features,
        "code": _code_evidence(session, project),
    }


def _normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    profile = dict(raw or {})
    interfaces = profile.get("interfaces") if isinstance(profile.get("interfaces"), dict) else {}
    auth = profile.get("auth") if isinstance(profile.get("auth"), dict) else {}
    environment = profile.get("environment") if isinstance(profile.get("environment"), dict) else {}
    sla = profile.get("sla") if isinstance(profile.get("sla"), dict) else {}
    confidence = profile.get("confidence") if isinstance(profile.get("confidence"), dict) else {}
    return {
        "interfaces": {
            "http": bool(interfaces.get("http")),
            "cli": bool(interfaces.get("cli")),
            "browser": bool(interfaces.get("browser")),
        },
        "auth": {
            "login_path": str(auth.get("login_path") or ""),
            "user_field": str(auth.get("user_field") or "username"),
            "password_field": str(auth.get("password_field") or "password"),
            "token_endpoint": str(auth.get("token_endpoint") or ""),
            "token_header": str(auth.get("token_header") or "PRIVATE-TOKEN"),
            "token_verify_path": str(auth.get("token_verify_path") or auth.get("verify_path") or ""),
            "admin_user_endpoint": str(auth.get("admin_user_endpoint") or ""),
            "admin_user_method": str(auth.get("admin_user_method") or "POST"),
        },
        "environment": {
            "readiness_path": str(environment.get("readiness_path") or ""),
            "cli_binary_hint": str(environment.get("cli_binary_hint") or ""),
            "cli_runner_hint": str(environment.get("cli_runner_hint") or ""),
        },
        "sla": {"performance_ms": int(sla.get("performance_ms") or 200)},
        "confidence": {
            "interfaces": float(confidence.get("interfaces") or 0),
            "auth": float(confidence.get("auth") or 0),
            "cli": float(confidence.get("cli") or 0),
        },
        "notes": str(profile.get("notes") or "")[:2000],
    }


def _login(client: httpx.Client, base: str, profile: dict[str, Any], username: str, password: str) -> bool:
    path = str((profile.get("auth") or {}).get("login_path") or "")
    if not path:
        return False
    try:
        response = client.post(
            base + path,
            data={
                (profile["auth"].get("user_field") or "username"): username,
                (profile["auth"].get("password_field") or "password"): password,
            },
            headers={"Origin": base, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code in {302, 303}:
            return True
        if response.status_code != 200:
            return False
        body = (response.text or "").lower()
        # 失败登录通常还是 200（重新渲染登录页），必须看正文
        return not any(word in body for word in ("不正确", "incorrect", "invalid", "失败", "error"))
    except Exception:  # noqa: BLE001
        return False


def _bootstrap(
    base_url: str,
    profile: dict[str, Any],
    admin_username: str,
    admin_password: str,
) -> dict[str, Any]:
    auth = profile.get("auth") or {}
    base = base_url.rstrip("/")
    result: dict[str, Any] = {
        "login": None,
        "user_created": False,
        "token": "",
        "verified": False,
        "token_owner": "",
    }
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as admin:
        admin_ok = _login(admin, base, profile, admin_username, admin_password)
        result["login"] = {"ok": admin_ok}
        if not admin_ok:
            admin.auth = (admin_username, admin_password)

        # 建测试用户：profile 推断的端点可能是表单路由，逐个候选验证
        chosen = ""
        candidates: list[str] = []
        for item in (auth.get("admin_user_endpoint") or "", "/api/v1/users", "/api/users"):
            if item and item not in candidates:
                candidates.append(item)
        form_payload = {"username": TEST_USER, "password": TEST_PASSWORD, "displayName": "Shakedown Onboarding"}
        for endpoint in candidates:
            try:
                response = admin.request(
                    auth.get("admin_user_method") or "POST",
                    base + endpoint,
                    json={**form_payload, "isAdmin": False},
                    headers={"Origin": base, "Content-Type": "application/json"},
                )
            except Exception as exc:  # noqa: BLE001
                result["user_error"] = f"{type(exc).__name__}: {exc}"
                continue
            result["user_status"] = response.status_code
            if response.status_code in {200, 201}:
                chosen = endpoint
                result["user_created"] = True
                break
            if response.status_code == 409:
                chosen = endpoint
                result["user_created"] = True
                result["user_existed"] = True
                break
            # 表单路由兜底
            try:
                form = admin.post(
                    base + endpoint,
                    data=form_payload,
                    headers={"Origin": base, "Content-Type": "application/x-www-form-urlencoded"},
                )
                if form.status_code in {200, 201}:
                    chosen = endpoint
                    result["user_created"] = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if chosen:
            profile["auth"]["admin_user_endpoint"] = chosen

        # 用测试用户自己登录并建 token，确保 token 属于 bot 而不是 admin
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as bot:
            bot_ok = _login(bot, base, profile, TEST_USER, TEST_PASSWORD)
            result["bot_login"] = bot_ok
            token_client = bot if bot_ok else admin
            token_endpoint = auth.get("token_endpoint") or ""
            if token_endpoint:
                try:
                    response = token_client.post(
                        base + token_endpoint,
                        json={"name": "shakedown-onboarding"},
                        headers={"Content-Type": "application/json"},
                    )
                    payload: dict[str, Any] = {}
                    try:
                        payload = response.json()
                    except Exception:  # noqa: BLE001
                        payload = {}
                    token = str(
                        payload.get("token")
                        or payload.get("value")
                        or payload.get("access_token")
                        or ""
                    )
                    result["token"] = token
                    result["token_status"] = response.status_code
                    result["token_owner"] = "bot" if bot_ok else "admin"
                except Exception as exc:  # noqa: BLE001
                    result["token_error"] = f"{type(exc).__name__}: {exc}"

        verify_path = auth.get("token_verify_path") or ""
        if result["token"] and verify_path:
            try:
                # 必须用无 Cookie 的新 client：服务端可能优先认会话，会把 bot 的 token 验成 admin
                with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as verify_client:
                    verify = verify_client.get(
                        base + verify_path,
                        headers={auth.get("token_header") or "PRIVATE-TOKEN": result["token"]},
                    )
                result["verify_status"] = verify.status_code
                result["verified"] = verify.status_code == 200
                if result["verified"]:
                    try:
                        result["verify_user"] = str((verify.json() or {}).get("username") or "")
                    except Exception:  # noqa: BLE001
                        result["verify_user"] = ""
            except Exception as exc:  # noqa: BLE001
                result["verify_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _exec_shell(project: Project, shell_command: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """平台受控执行：只接受平台构造的命令，不把 LLM 输出直接当 shell。"""
    settings = project.settings_json or {}
    adapter = settings.get("exec_adapter") if isinstance(settings.get("exec_adapter"), dict) else {}
    if adapter.get("type") == "lima":
        command = [
            "limactl",
            "shell",
            str(adapter.get("instance") or "debian12"),
            "--",
            "bash",
            "-lc",
            shell_command,
        ]
    else:
        command = ["bash", "-lc", shell_command]
    started = time.time()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "stdout": (completed.stdout or "")[-4000:],
            "stderr": (completed.stderr or "")[-4000:],
            "duration_ms": (time.time() - started) * 1000,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "duration_ms": (time.time() - started) * 1000,
        }


def _discover_cli(project: Project, profile: dict[str, Any]) -> dict[str, Any]:
    """在受控执行环境里找 CLI 二进制；找不到就返回空，不编造路径。"""
    hint = str((profile.get("environment") or {}).get("cli_binary_hint") or "")
    match = re.search(r"[A-Za-z0-9_.-]{2,}", hint)
    name = match.group(0) if match else ""
    if not name:
        return {}
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", name)
    if not safe:
        return {}
    command = (
        f"command -v {safe} 2>/dev/null || "
        f"find /var/lib/berth/volumes /opt /usr/local -maxdepth 4 -name {safe} -type f 2>/dev/null | head -1"
    )
    outcome = _exec_shell(project, command)
    lines = [line.strip() for line in (outcome.get("stdout") or "").splitlines() if line.strip()]
    binary = lines[-1] if lines else ""
    if not binary.startswith("/"):
        return {"error": outcome.get("stderr") or "not found"}
    profile["environment"]["cli_binary"] = binary
    return {"binary": binary, "raw": outcome.get("stdout", "")[-500:]}


def run_onboarding(
    session: Session,
    project: Project,
    *,
    admin_username: str,
    admin_password: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    provider = get_provider()
    evidence = collect_evidence(session, project)
    payload = provider.structured(
        SYSTEM_PROMPT,
        "根据 evidence 输出这个项目的 onboarding profile。",
        PROFILE_SCHEMA,
        context=evidence,
        max_tokens=8192,
        reasoning_effort="medium",
    )
    if payload.get("__error__"):
        raise RuntimeError(payload["__error__"])
    profile = _normalize_profile(payload)
    discovery: dict[str, Any] = {}
    if profile["interfaces"].get("cli"):
        discovery = _discover_cli(project, profile)
    thresholds = {"interfaces": 0.6, "auth": 0.7, "cli": 0.7}
    low_confidence = [
        key
        for key, threshold in thresholds.items()
        if float(profile["confidence"].get(key) or 0) < threshold
    ]
    profile["review_required"] = bool(low_confidence)
    profile["low_confidence"] = low_confidence

    report: dict[str, Any] = {
        "project_id": project.id,
        "dry_run": dry_run,
        "profile": profile,
        "evidence": {
            "probes": [
                {"path": item.get("path"), "status": item.get("status"), "error": item.get("error")}
                for item in evidence["probes"]
            ],
            "endpoints": len(evidence["endpoints"]),
            "code_snippets": len(evidence["code"]),
        },
        "bootstrap": {},
        "discovery": discovery,
        "low_confidence": low_confidence,
    }
    if dry_run:
        return report

    bootstrap = _bootstrap(project.base_url or "", profile, admin_username, admin_password)
    report["bootstrap"] = {
        "login": bootstrap.get("login"),
        "user_created": bootstrap.get("user_created"),
        "user_status": bootstrap.get("user_status"),
        "bot_login": bootstrap.get("bot_login"),
        "token_status": bootstrap.get("token_status"),
        "token_owner": bootstrap.get("token_owner"),
        "verified": bootstrap.get("verified"),
        "verify_user": bootstrap.get("verify_user"),
        "errors": {
            key: value
            for key, value in bootstrap.items()
            if key.endswith("error") or key == "token_error"
        },
    }
    settings = dict(project.settings_json or {})
    settings["profile"] = profile
    if profile["sla"].get("performance_ms"):
        settings["performance_sla_ms"] = int(profile["sla"]["performance_ms"])
    project.settings_json = settings

    token_owner = str(bootstrap.get("token_owner") or "")
    verify_user = str(bootstrap.get("verify_user") or "")
    if bootstrap.get("token") and bootstrap.get("verified") and token_owner == "bot" and verify_user == TEST_USER:
        from app.crypto import decrypt_json

        accounts = decrypt_json(project.test_accounts_encrypted) or {}
        accounts[TEST_USER] = {
            "username": TEST_USER,
            "password": TEST_PASSWORD,
            "token": bootstrap["token"],
        }
        project.test_accounts_encrypted = encrypt_json(accounts)
        report["account"] = {"name": TEST_USER, "has_token": True, "verified": True}
    if low_confidence:
        record_exception(
            session,
            project,
            kind="onboarding_low_confidence",
            target_type="project",
            target_id=project.id,
            severity="low",
            note="onboarding 置信度不足：" + "、".join(low_confidence),
            payload={"confidence": profile["confidence"], "low_confidence": low_confidence},
        )

    record_trace(
        session,
        project_id=project.id,
        kind="onboarding",
        step="discover+bootstrap",
        payload={
            "input": {"base_url": project.base_url, "dry_run": dry_run},
            "output": {"profile": profile, "bootstrap": report["bootstrap"]},
        },
    )
    session.commit()
    logger.info(
        "onboarding 完成：project=%s http=%s cli=%s auth=%s verified=%s",
        project.id,
        profile["interfaces"]["http"],
        profile["interfaces"]["cli"],
        profile["auth"].get("login_path"),
        bootstrap.get("verified"),
    )
    return report


__all__ = ["run_onboarding", "collect_evidence"]
