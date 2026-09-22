"""Passwords, tokens, CSRF and login rate limiting (SEC-1, SEC-5, AUTH-4/5/7)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 10
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


class TokenError(Exception):
    pass


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return False


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalise_email(email: str) -> str:
    return email.strip().lower()


# ----------------------------------------------------------------------- JWT


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, get_settings().secret_key, algorithm="HS256")


def create_access_token(*, user_id: str, role: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return _encode(
        {
            "sub": user_id,
            "role": role,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=settings.access_token_ttl_min)).timestamp()),
            "jti": secrets.token_hex(8),
        }
    )


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    try:
        claims = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("type") != expected_type:
        raise TokenError("unexpected token type")
    return claims


def new_refresh_token() -> tuple[str, str, str]:
    """Return ``(raw_token, token_hash, family_id)``."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw), str(__import__("uuid").uuid4())


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=get_settings().refresh_token_ttl_days)


# ---------------------------------------------------------------------- CSRF


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# ------------------------------------------------------------ rate limiting


def _failure_key(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]
    return f"rf:login-fail:{kind}:{digest}"


async def login_failures(broker, *, email: str, ip: str | None) -> int:
    counts = [await broker.get_int(_failure_key("email", email))]
    if ip:
        counts.append(await broker.get_int(_failure_key("ip", ip)))
    return max(counts or [0])


async def register_login_failure(broker, *, email: str, ip: str | None) -> None:
    await broker.incr(_failure_key("email", email), ttl_seconds=LOGIN_WINDOW_SECONDS)
    if ip:
        await broker.incr(_failure_key("ip", ip), ttl_seconds=LOGIN_WINDOW_SECONDS)


async def clear_login_failures(broker, *, email: str, ip: str | None) -> None:
    await broker.delete(_failure_key("email", email))
    if ip:
        await broker.delete(_failure_key("ip", ip))


async def is_login_blocked(broker, *, email: str, ip: str | None) -> bool:
    return await login_failures(broker, email=email, ip=ip) >= LOGIN_MAX_FAILURES
