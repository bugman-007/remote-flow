"""API-key encryption at rest: AES-256-GCM with MASTER_KEY (SET-5, SEC-4)."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_VERSION = b"\x01"


def _key() -> bytes:
    return get_settings().resolved_master_key()


def encrypt_secret(plaintext: str) -> bytes:
    if plaintext is None:
        raise ValueError("plaintext required")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _VERSION + nonce + ciphertext


def decrypt_secret(payload: bytes | None) -> str | None:
    if not payload:
        return None
    raw = bytes(payload)
    if raw[:1] != _VERSION:
        raise ValueError("unsupported ciphertext version")
    nonce, ciphertext = raw[1:13], raw[13:]
    return AESGCM(_key()).decrypt(nonce, ciphertext, None).decode("utf-8")


def mask_secret(plaintext: str | None, *, visible: int = 4) -> str:
    """Return the ``••••…ab12`` form used in the Settings UI (SET-2)."""
    if not plaintext:
        return ""
    if len(plaintext) <= visible:
        return "•" * len(plaintext)
    return "•" * 8 + "…" + plaintext[-visible:]


def encode_master_key(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
