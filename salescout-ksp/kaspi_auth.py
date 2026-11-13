"""Хеш пароля и сессии. Сам пароль в базе не хранится.

Сессия подписана и лежит в cookie. Её можно проверить на любом экземпляре
сервера, без общей таблицы сессий.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ITERATIONS = 200_000
SESSION_DAYS = 14


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, digest_hex)


def new_session_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, token_hash(raw)


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def session_key(secret: str | None) -> bytes | None:
    """Ключ подписи только из SESSION_SECRET. Пустой и заведомо слабый секрет не принимается."""
    if secret is None:
        return None
    text = secret.strip().strip('"').strip("'")
    if len(text) < 16 or text.lower() in {"local-session", "changeme", "password", "secret"}:
        return None
    return hashlib.sha256(b"kaspi-session-v1\n" + text.encode("utf-8")).digest()


def issue_session(user: dict, key: bytes, now: int | None = None) -> str:
    moment = int(time.time() if now is None else now)
    payload = json.dumps(
        {
            "id": int(user["id"]),
            "username": user["username"],
            "exp": moment + SESSION_DAYS * 24 * 3600,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64(payload)
    signature = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def read_session(token: str | None, key: bytes, now: int | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    body, signature = token.split(".", 1)
    expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _b64decode(signature)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    moment = int(time.time() if now is None else now)
    if not isinstance(payload, dict) or int(payload.get("exp") or 0) <= moment:
        return None
    if not isinstance(payload.get("username"), str) or not isinstance(payload.get("id"), int):
        return None
    return payload
