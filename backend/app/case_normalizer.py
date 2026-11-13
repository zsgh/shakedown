"""用例归一化：把历史/LLM 生成用例里的账号占位符、登录表单、鉴权头统一成平台能执行的写法。

只做确定性、可审计的替换，不猜测业务语义：
- `{{admin_token}}` / `{{admin_password}}` / `{{admin_username}}` / `{{admin_session}}`
  → `{{account.<name>.<field>}}` 或 auth 模式；
- 登录表单缺 password / username 时，从项目账号补齐；
- 不覆盖已经有 auth 或显式字面量的步骤。
"""
from __future__ import annotations

import json
import re
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

TOKEN_PLACEHOLDERS = {"admin_token", "token_admin", "admin_api_token"}
SESSION_PLACEHOLDERS = {"admin_session", "admin_cookie"}
PASSWORD_PLACEHOLDERS = {"admin_password", "admin_old_password", "new_admin_password"}
USERNAME_PLACEHOLDERS = {"admin_username", "admin_user"}


def _pick_account(accounts: list[str]) -> str:
    if "admin" in accounts:
        return "admin"
    return accounts[0] if accounts else "admin"


def _replace(value: str, account: str) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in TOKEN_PLACEHOLDERS:
            return f"{{{{account.{account}.token}}}}"
        if key in PASSWORD_PLACEHOLDERS:
            return f"{{{{account.{account}.password}}}}"
        if key in USERNAME_PLACEHOLDERS:
            return f"{{{{account.{account}.username}}}}"
        return match.group(0)

    return PLACEHOLDER.sub(repl, value)


def normalize_case_auth(spec: dict[str, Any], accounts: list[str]) -> int:
    account = _pick_account(accounts)
    changed = 0
    for step in list(spec.get("steps") or []) + list(spec.get("cleanup") or []):
        if not isinstance(step, dict):
            continue
        path = str(step.get("path") or "")
        headers = step.get("headers")
        if isinstance(headers, dict):
            new_headers: dict[str, Any] = {}
            for key, value in headers.items():
                if not isinstance(value, str):
                    new_headers[key] = value
                    continue
                low = key.lower()
                if "{{admin_token}}" in value or "{{admin_user}}" in value:
                    if low in {"private-token", "private_token"}:
                        step["auth"] = {"account": account, "mode": "private_token"}
                        changed += 1
                        continue
                    if low == "authorization" and value.lower().startswith("bearer"):
                        step["auth"] = {"account": account, "mode": "bearer"}
                        changed += 1
                        continue
                if "{{admin_session}}" in value or "{{admin_cookie}}" in value:
                    if low == "cookie":
                        step["auth"] = {"account": account, "mode": "basic"}
                        changed += 1
                        continue
                rendered = _replace(value, account)
                if rendered != value:
                    changed += 1
                new_headers[key] = rendered
            step["headers"] = new_headers

        body = step.get("body")
        if isinstance(body, dict) and path.rstrip("/").endswith("/login"):
            body = dict(body)
            for field in ("username", "password"):
                if field in body and isinstance(body[field], str):
                    rendered = _replace(body[field], account)
                    if rendered != body[field]:
                        body[field] = rendered
                        changed += 1
            if "password" not in body:
                body["password"] = f"{{{{account.{account}.password}}}}"
                changed += 1
            if "username" not in body:
                body["username"] = f"{{{{account.{account}.username}}}}"
                changed += 1
            step["body"] = body

        for key in ("path", "description"):
            value = step.get(key)
            if isinstance(value, str):
                rendered = _replace(value, account)
                if rendered != value:
                    step[key] = rendered
                    changed += 1
    return changed


__all__ = ["normalize_case_auth"]
