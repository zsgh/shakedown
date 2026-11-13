"""高风险操作识别：删除数据 / 修改配置等需要人工确认才能启用。"""
from __future__ import annotations

import re
from typing import Any

DESTRUCTIVE_METHODS = {"DELETE", "PUT", "PATCH"}


def _normalize(path: str) -> str:
    path = (path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path


def is_whitelisted(step: dict[str, Any], whitelist: list[str]) -> bool:
    """白名单形如 ``DELETE /api/test-data/*``（支持 * 通配）。"""
    method = (step.get("method") or "GET").upper()
    path = _normalize(step.get("path") or "/")
    for rule in whitelist:
        rule = str(rule).strip()
        if not rule:
            continue
        parts = rule.split(None, 1)
        rule_method = parts[0].upper() if parts else "*"
        rule_path = parts[1] if len(parts) > 1 else "*"
        if rule_method not in {"*", method}:
            continue
        pattern = "^" + re.escape(rule_path).replace(r"\*", ".*") + "$"
        if re.match(pattern, path):
            return True
    return False


def high_risk_reasons(spec: dict[str, Any], settings: dict[str, Any] | None = None) -> list[str]:
    """返回该用例的高风险原因列表（空列表 = 不高风险）。"""
    spec = spec or {}
    settings = settings or {}
    whitelist = list(settings.get("risk_whitelist") or [])
    reasons: list[str] = []

    if str(spec.get("risk", "")).lower() == "high":
        reasons.append("用例显式标记 risk=high")

    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        method = (step.get("method") or "").upper()
        if method in DESTRUCTIVE_METHODS and not is_whitelisted(step, whitelist):
            reasons.append(f"{method} {step.get('path', '')}（删除数据 / 修改配置）")
        if step.get("destructive") and not is_whitelisted(step, whitelist):
            reasons.append(f"{method or 'STEP'} {step.get('path', '')}（标记为 destructive）")
    return reasons
