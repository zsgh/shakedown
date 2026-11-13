"""PII / secret 脱敏测试。"""
from __future__ import annotations

from app.redaction import redact, redact_text


def test_redact_nested_secrets_and_pii():
    value = {
        "email": "alice@example.com",
        "phone": "13812345678",
        "Authorization": "Bearer abc.def",
        "user": {"password": "p@ss", "note": "call 13900001111"},
        "items": [{"token": "secret-token"}],
    }
    result = redact(value)
    assert result["email"] == "***@example.com"
    assert result["phone"] == "138****0000"
    assert result["Authorization"] == "***"
    assert result["user"]["password"] == "***"
    assert "13900001111" not in result["user"]["note"]
    assert result["items"][0]["token"] == "***"


def test_redact_text():
    assert "alice@example.com" not in redact_text("mail alice@example.com")
    assert "Bearer" in redact_text("Authorization: Bearer abc")
    assert "abc" not in redact_text("Authorization: Bearer abc")
