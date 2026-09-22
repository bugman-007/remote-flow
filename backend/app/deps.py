"""Shared FastAPI dependencies: authentication, role checks, CSRF (SEC-2)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import APIError
from app.models import User
from app.security import csrf_tokens_match
from app.services.sessions import access_token_from_request, decode_access

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    token = access_token_from_request(request)
    if not token:
        raise APIError("unauthorized", "Authentication required.", status_code=401)
    claims = decode_access(token)
    user = (await session.execute(select(User).where(User.id == claims["sub"]))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise APIError("unauthorized", "Authentication required.", status_code=401)
    request.state.access_claims = claims
    request.state.user_id = user.id
    return user


async def csrf_protect(request: Request) -> None:
    if request.method not in MUTATING_METHODS:
        return
    settings = get_settings()
    if not request.cookies.get(settings.access_cookie_name):
        return
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("x-csrf-token")
    if not csrf_tokens_match(cookie, header):
        raise APIError("csrf_failed", "Your session token is missing or stale. Reload the page.", status_code=403)


def ip_allowed(ip: str | None, allowlist: str) -> bool:
    """SEC-9: match an IP against a comma-separated list of addresses/CIDRs."""
    import ipaddress

    entries = [entry.strip() for entry in allowlist.split(",") if entry.strip()]
    if not entries:
        return True
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip in entries
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            if ip == entry:
                return True
    return False


def require_role(*roles: str) -> Callable:
    async def dependency(request: Request, user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise APIError("forbidden", "Your role cannot perform this action.", status_code=403)
        if "manager" in roles:
            settings = get_settings()
            if not ip_allowed(client_ip(request), settings.manager_ip_allowlist):
                raise APIError(
                    "ip_not_allowed", "Your network is not allowed to use manager tools.", status_code=403
                )
        return user

    return dependency


manager_required = require_role("manager")
maker_required = require_role("maker")
reviewer_required = require_role("reviewer")
