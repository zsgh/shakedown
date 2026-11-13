"""敏感配置加密存储（项目测试账号、被测库连接串等）。

使用 Fernet 对称加密；密钥来自 ``SHAKEDOWN_ENCRYPTION_KEY`` 环境变量，
未配置时自动生成并持久化到 ``data_dir/secret.key``（仅限本地开发）。
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_PREFIX = "enc:v1:"


def _key() -> bytes:
    settings = get_settings()
    if settings.encryption_key:
        raw = settings.encryption_key.strip()
        try:
            Fernet(raw.encode() if isinstance(raw, str) else raw)
            return raw.encode() if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001 —— 允许传入任意口令
            digest = hashlib.sha256(raw.encode()).digest()
            return base64.urlsafe_b64encode(digest)
    key_file: Path = settings.data_dir / "secret.key"
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    return key_file.read_bytes().strip()


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith(_PREFIX):
        return value
    token = Fernet(_key()).encrypt(value.encode()).decode()
    return _PREFIX + token


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        # 历史明文数据
        return value
    try:
        raw = value[len(_PREFIX) :]
        return Fernet(_key()).decrypt(raw.encode()).decode()
    except InvalidToken:
        return None


def encrypt_json(payload: dict | None) -> str | None:
    if payload is None:
        return None
    import json

    return encrypt(json.dumps(payload, ensure_ascii=False))


def decrypt_json(value: str | None) -> dict | None:
    if not value:
        return None
    import json

    plain = decrypt(value)
    if plain is None:
        return None
    try:
        data = json.loads(plain)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
