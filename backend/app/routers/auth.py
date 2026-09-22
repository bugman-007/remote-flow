"""AUTH-1…AUTH-7."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, current_user
from app.errors import APIError
from app.models import AuditLog, User
from app.schemas import ChangePasswordRequest, LoginRequest
from app.security import (
    clear_login_failures,
    hash_password,
    is_login_blocked,
    needs_rehash,
    new_csrf_token,
    normalise_email,
    register_login_failure,
    verify_password,
)
from app.serializers import user_out
from app.services import sessions as session_service
from app.services.broker import get_broker
from app.utils import utcnow

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])

ROLE_HOME = {"maker": "/jd-upload", "manager": "/resumes", "reviewer": "/interviews"}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    email = normalise_email(payload.email)
    ip = client_ip(request)
    broker = get_broker()
    if await is_login_blocked(broker, email=email, ip=ip):
        raise APIError(
            "rate_limited",
            "Too many failed attempts. Try again in 15 minutes.",
            status_code=429,
        )
    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, payload.password):
        await register_login_failure(broker, email=email, ip=ip)
        raise APIError("invalid_credentials", "Email or password is incorrect.", status_code=401)
    if payload.as_admin and user.role != "manager":
        raise APIError("not_a_manager", "This account is not a manager.", status_code=403)
    await clear_login_failures(broker, email=email, ip=ip)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_login_at = utcnow()
    csrf = new_csrf_token()
    access, refresh = await session_service.issue_session(
        session, user, user_agent=request.headers.get("user-agent"), ip=ip
    )
    session.add(AuditLog(actor_id=user.id, action="auth.login", entity_type="user", entity_id=user.id, ip=ip))
    session_service.set_auth_cookies(response, access_token=access, refresh_token=refresh, csrf_token=csrf)
    await session.commit()
    return {
        "user": user_out(user),
        "csrf_token": csrf,
        "redirect": "/resumes" if payload.as_admin else ROLE_HOME.get(user.role, "/"),
    }


@router.post("/refresh")
async def refresh(request: Request, response: Response, session: AsyncSession = Depends(get_session)):
    from app.config import get_settings

    raw = request.cookies.get(get_settings().refresh_cookie_name)
    if not raw:
        raise APIError("invalid_refresh_token", "Session expired, please log in again.", status_code=401)
    user, access, new_raw = await session_service.rotate_refresh_token(
        session, raw, user_agent=request.headers.get("user-agent"), ip=client_ip(request)
    )
    csrf = new_csrf_token()
    session_service.set_auth_cookies(response, access_token=access, refresh_token=new_raw, csrf_token=csrf)
    await session.commit()
    return {"user": user_out(user), "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response, user: User = Depends(current_user),
                 session: AsyncSession = Depends(get_session)):
    await session_service.revoke_all_sessions(session, user.id)
    session_service.clear_auth_cookies(response)
    await session.commit()
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"user": user_out(user), "home": ROLE_HOME.get(user.role, "/")}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if not verify_password(user.password_hash, payload.current_password):
        raise APIError("invalid_credentials", "Current password is incorrect.", status_code=400)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    session.add(
        AuditLog(actor_id=user.id, action="auth.change_password", entity_type="user", entity_id=user.id, ip=client_ip(request))
    )
    await session.commit()
    return {"ok": True}
