"""Small shared helpers: UUIDv7, time and slug utilities."""

from __future__ import annotations

import os
import re
import secrets
import time
import unicodedata
import uuid
from datetime import UTC, datetime

_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "lpt1", "lpt2",
    "lpt3", ".", "..", "",
}


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUID (UUIDv7, RFC 9562)."""
    unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = unix_ms << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def new_id() -> str:
    """String form of a fresh UUIDv7, used for primary keys."""
    return str(uuid7())


def utcnow() -> datetime:
    return datetime.now(UTC)


def dashify(value: str | None) -> str:
    """Slugify a value the way the desktop generator does (spaces/punct -> dash)."""
    if not value:
        return ""
    normalised = unicodedata.normalize("NFKD", str(value))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.replace("&", " and ")
    ascii_only = re.sub(r"[^\w\s-]", "", ascii_only)
    ascii_only = re.sub(r"[\s_]+", "-", ascii_only.strip())
    return re.sub(r"-{2,}", "-", ascii_only).strip("-")


def slugify(value: str | None, *, fallback: str = "resume", max_length: int = 80) -> str:
    """STO-2: dashify + length cap + reserved-name guard. Never returns a path segment."""
    slug = dashify(value)[:max_length].strip("-")
    if not slug or slug.lower() in _RESERVED_NAMES:
        slug = fallback
    if slug in {".", ".."} or os.sep in slug or "/" in slug:
        slug = fallback
    return slug


def sha256_file(path: str, *, chunk_size: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
