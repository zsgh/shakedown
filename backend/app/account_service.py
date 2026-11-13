"""测试账号预检：登录 / token 有效性 / 角色矩阵健康度。

预检只做轻量 HTTP 请求，不修改账号；结果用于 run 前提示，不把账号问题
误判成产品缺陷。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.models import Project


def _profile_auth(project: Project) -> dict[str, Any]:
    profile = (project.settings_json or {}).get("profile")
    if not isinstance(profile, dict):
        return {}
    auth = profile.get("auth")
    return auth if isinstance(auth, dict) else {}


def _token_headers(token: str, profile: dict[str, Any]) -> dict[str, str]:
    header = str(profile.get("token_header") or "Authorization")
    if header.lower() in {"authorization", "bearer"}:
        value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
        return {"Authorization": value}
    return {header: token}


def validate_account(
    project: Project,
    name: str,
    account: dict[str, Any],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    base_url = str(project.base_url or "").rstrip("/")
    auth = _profile_auth(project)
    started = time.perf_counter()
    result: dict[str, Any] = {
        "name": name,
        "role": str(account.get("role") or ""),
        "has_username": bool(account.get("username")),
        "has_password": bool(account.get("password")),
        "has_token": bool(account.get("token")),
        "status": "skipped",
        "ok": None,
        "latency_ms": 0.0,
        "detail": "",
    }
    if not base_url:
        result.update(status="skipped", detail="base_url 未配置")
        return result

    token = str(account.get("token") or "")
    verify_path = str(auth.get("token_verify_path") or "")
    if token and verify_path:
        url = base_url + (verify_path if verify_path.startswith("/") else "/" + verify_path)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.get(url, headers=_token_headers(token, auth))
            ok = 200 <= response.status_code < 300
            result.update(
                status="token_ok" if ok else "token_invalid",
                ok=ok,
                detail=f"HTTP {response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001
            result.update(status="unreachable", ok=False, detail=f"{type(exc).__name__}: {exc}")
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    login_path = str(auth.get("login_path") or "")
    if account.get("username") and account.get("password") and login_path:
        url = base_url + (login_path if login_path.startswith("/") else "/" + login_path)
        payload = {
            str(auth.get("user_field") or "username"): account.get("username"),
            str(auth.get("password_field") or "password"): account.get("password"),
        }
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = client.post(
                    url,
                    data=payload,
                    headers={"Origin": base_url, "Content-Type": "application/x-www-form-urlencoded"},
                )
            body = (response.text or "").lower()
            failed_markers = ("invalid", "incorrect", "不正确", "失败")
            ok = response.status_code < 400 and not any(word in body for word in failed_markers)
            result.update(
                status="login_ok" if ok else "login_failed",
                ok=ok,
                detail=f"HTTP {response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001
            result.update(status="unreachable", ok=False, detail=f"{type(exc).__name__}: {exc}")
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    result["detail"] = "没有可用的 token_verify_path / login_path，未发起预检"
    return result


def preflight_accounts(
    project: Project,
    accounts: dict[str, Any] | None = None,
    *,
    names: list[str] | None = None,
) -> dict[str, Any]:
    from app.crypto import decrypt_json

    values = accounts if accounts is not None else (decrypt_json(project.test_accounts_encrypted) or {})
    selected = set(names or [])
    items: list[dict[str, Any]] = []
    for name, raw in values.items():
        if selected and name not in selected:
            continue
        if not isinstance(raw, dict):
            continue
        items.append(validate_account(project, str(name), raw))
    failures = [item for item in items if item.get("ok") is False]
    skipped = [item for item in items if item.get("status") == "skipped"]
    return {
        "project_id": project.id,
        "total": len(items),
        "ok": len(items) - len(failures) - len(skipped),
        "failed": len(failures),
        "skipped": len(skipped),
        "items": items,
        "healthy": not failures,
    }


__all__ = ["preflight_accounts", "validate_account"]
