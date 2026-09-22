"""Cookie sessions: rotating refresh tokens with reuse detection (AUTH-5, SEC-5)."""

from __future__ import annotations

from fastapi import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import APIError
from app.models import RefreshToken, User
from app.security import (
    TokenError,
    create_access_token,
    decode_token,
    hash_refresh_token,
    new_refresh_token,
    refresh_expiry,
)
from app.utils import utcnow


def _cookie_kwargs(*, httponly: bool, max_age: int) -> dict:
    settings = get_settings()
    return {
        "httponly": httponly,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "max_age": max_age,
        "path": "/",
        "domain": settings.cookie_domain,
    }


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str, csrf_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.access_cookie_name,
        access_token,
        **_cookie_kwargs(httponly=True, max_age=settings.access_token_ttl_min * 60),
    )
    response.set_cookie(
        settings.refresh_cookie_name,
        refresh_token,
        **_cookie_kwargs(httponly=True, max_age=settings.refresh_token_ttl_days * 24 * 3600),
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        **_cookie_kwargs(httponly=False, max_age=settings.refresh_token_ttl_days * 24 * 3600),
    )


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    for name in (settings.access_cookie_name, settings.refresh_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", domain=settings.cookie_domain)


async def issue_session(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
    family_id: str | None = None,
) -> tuple[str, str]:
    raw, token_hash, generated_family = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            family_id=family_id or generated_family,
            expires_at=refresh_expiry(),
            user_agent=(user_agent or "")[:400],
            ip=ip,
        )
    )
    await session.flush()
    return create_access_token(user_id=user.id, role=user.role), raw


async def rotate_refresh_token(
    session: AsyncSession, raw_token: str, *, user_agent: str | None, ip: str | None
) -> tuple[User, str, str]:
    token_hash = hash_refresh_token(raw_token)
    row = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))).scalar_one_or_none()
    if row is None:
        raise APIError("invalid_refresh_token", "Session expired, please log in again.", status_code=401)
    if row.revoked_at is not None:
        family = (
            await session.execute(select(RefreshToken).where(RefreshToken.family_id == row.family_id))
        ).scalars().all()
        for sibling in family:
            sibling.revoked_at = sibling.revoked_at or utcnow()
        raise APIError("refresh_token_reused", "Session expired, please log in again.", status_code=401)
    if row.expires_at <= utcnow():
        row.revoked_at = utcnow()
        raise APIError("refresh_token_expired", "Session expired, please log in again.", status_code=401)
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise APIError("unauthorized", "Session expired, please log in again.", status_code=401)
    row.revoked_at = utcnow()
    access, new_raw = await issue_session(
        session, user, user_agent=user_agent, ip=ip, family_id=row.family_id
    )
    return user, access, new_raw


async def revoke_all_sessions(session: AsyncSession, user_id: str) -> int:
    rows = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        )
    ).scalars().all()
    moment = utcnow()
    for row in rows:
        row.revoked_at = moment
    return len(rows)


async def active_session_count(session: AsyncSession, user_id: str) -> int:
    rows = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > utcnow(),
            )
        )
    ).scalars().all()
    return len(rows)


def access_token_from_request(request) -> str | None:
    settings = get_settings()
    cookie = request.cookies.get(settings.access_cookie_name)
    if cookie:
        return cookie
    header = request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def decode_access(token: str) -> dict:
    try:
        return decode_token(token, expected_type="access")
    except TokenError as exc:
        raise APIError("unauthorized", "Please log in again.", status_code=401) from exc
