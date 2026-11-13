"""敏感信息脱敏：用于执行结果留档前的可选 PII / secret 清理。"""
from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PHONE = re.compile(r"\b1[3-9]\d{9}\b")
_CARD = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
}


def redact_text(text: str) -> str:
    value = _BEARER.sub("Bearer ***", str(text or ""))
    value = _EMAIL.sub("***@example.com", value)
    value = _PHONE.sub("138****0000", value)
    value = _CARD.sub("****", value)
    return value


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                result[key] = "***"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
