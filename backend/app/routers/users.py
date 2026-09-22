"""User administration (USR-1…USR-8)."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, manager_required
from app.errors import APIError
from app.models import AuditLog, Interview, Profile, ProfileAssignment, User
from app.schemas import CsvImportRequest, UserRequest, UserUpdate
from app.security import generate_password, hash_password, normalise_email
from app.serializers import user_out
from app.services import intake, sessions as session_service, settings_store
from app.utils import utcnow

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(csrf_protect)])


async def _active_manager_count(session: AsyncSession, *, excluding: str | None = None) -> int:
    query = select(func.count(User.id)).where(User.role == "manager", User.is_active.is_(True))
    if excluding:
        query = query.where(User.id != excluding)
    return int((await session.execute(query)).scalar_one() or 0)


@router.get("")
async def list_users(
    role: str | None = None,
    status: str | None = None,
    q: str | None = None,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    query = select(User)
    if role:
        query = query.where(User.role == role)
    if status == "active":
        query = query.where(User.is_active.is_(True))
    elif status == "deactivated":
        query = query.where(User.is_active.is_(False))
    if q:
        query = query.where(or_(User.name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%")))
    users = (await session.execute(query.order_by(User.name))).scalars().all()
    day = await intake.server_today(session)
    items = []
    for user in users:
        payload = user_out(user)
        payload["sessions"] = await session_service.active_session_count(session, user.id)
        if user.role == "maker":
            assignment = await intake.active_assignment(session, user.id)
            profile = await session.get(Profile, assignment.profile_id) if assignment else None
            payload["profile_id"] = assignment.profile_id if assignment else None
            payload["profile_name"] = profile.name if profile else None
            payload["usage_today"] = await intake.usage_today(session, user.id, day)
        items.append(payload)
    return {"items": items}


@router.post("", status_code=201)
async def create_user(
    payload: UserRequest,
    request: Request,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    email = normalise_email(payload.email)
    existing = (await session.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if existing is not None:
        raise APIError("email_taken", "That email address already exists.", status_code=409)
    password = payload.password or generate_password()
    if len(password) < 10:
        raise APIError("weak_password", "Passwords must be at least 10 characters.", status_code=422)
    daily_limit = payload.daily_limit
    if payload.role == "maker" and daily_limit is None:
        daily_limit = int(await settings_store.get_setting(session, "default_daily_limit") or 0)
    user = User(
        name=payload.name,
        email=email,
        role=payload.role,
        password_hash=hash_password(password),
        must_change_password=payload.must_change_password,
        daily_limit=daily_limit if payload.role == "maker" else None,
    )
    session.add(user)
    await session.flush()
    if payload.role == "maker" and payload.profile_id:
        profile = await session.get(Profile, payload.profile_id)
        if profile is None:
            raise APIError("invalid_profile", "Profile not found.", status_code=400)
        session.add(ProfileAssignment(profile_id=profile.id, maker_id=user.id, assigned_by=actor.id))
    session.add(
        AuditLog(actor_id=actor.id, action="user.create", entity_type="user", entity_id=user.id,
                 after={"email": email, "role": payload.role}, ip=client_ip(request))
    )
    await session.commit()
    return {"user": user_out(user), "initial_password": password}


@router.patch("/{user_id}")
async def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise APIError("not_found", "User not found.", status_code=404)
    data = payload.model_dump(exclude_unset=True)
    if user.id == actor.id:
        if data.get("role") and data["role"] != "manager":
            raise APIError("self_demote", "You cannot change your own role.", status_code=409)
        if data.get("is_active") is False:
            raise APIError("self_deactivate", "You cannot deactivate your own account.", status_code=409)
    if data.get("role") and data["role"] != "manager" and user.role == "manager":
        if await _active_manager_count(session, excluding=user.id) == 0:
            raise APIError("last_manager", "The last active manager cannot be demoted.", status_code=409)
    if data.get("is_active") is False and user.role == "manager":
        if await _active_manager_count(session, excluding=user.id) == 0:
            raise APIError("last_manager", "The last active manager cannot be deactivated.", status_code=409)
    if "email" in data and data["email"]:
        email = normalise_email(data["email"])
        clash = (await session.execute(select(User).where(func.lower(User.email) == email, User.id != user.id))).scalar_one_or_none()
        if clash is not None:
            raise APIError("email_taken", "That email address already exists.", status_code=409)
        data["email"] = email
    if data.get("role") and data["role"] != user.role:
        if user.role == "maker":
            assignments = (
                await session.execute(
                    select(ProfileAssignment).where(
                        ProfileAssignment.maker_id == user.id, ProfileAssignment.ended_at.is_(None)
                    )
                )
            ).scalars().all()
            for assignment in assignments:
                assignment.ended_at = utcnow()
        if user.role == "reviewer":
            open_interviews = (
                await session.execute(
                    select(func.count(Interview.id)).where(
                        Interview.reviewer_id == user.id, Interview.status == "scheduled"
                    )
                )
            ).scalar_one()
            if open_interviews:
                raise APIError(
                    "open_interviews",
                    "Reassign this reviewer's scheduled interviews first.",
                    status_code=409,
                    details={"open_interviews": int(open_interviews)},
                )
        if data["role"] != "maker":
            user.daily_limit = None
    if data.get("is_active") is False:
        data["is_active"] = False
    before = {"role": user.role, "is_active": user.is_active, "daily_limit": user.daily_limit}
    for key, value in data.items():
        setattr(user, key, value)
    if data.get("is_active") is False:
        await session_service.revoke_all_sessions(session, user.id)
    session.add(
        AuditLog(actor_id=actor.id, action="user.update", entity_type="user", entity_id=user.id,
                 before=before, after=data, ip=client_ip(request))
    )
    await session.commit()
    return user_out(user)


@router.post("/{user_id}/deactivate")
async def deactivate_user(
    user_id: str,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise APIError("not_found", "User not found.", status_code=404)
    if user.id == actor.id:
        raise APIError("self_deactivate", "You cannot deactivate your own account.", status_code=409)
    if user.role == "manager" and await _active_manager_count(session, excluding=user.id) == 0:
        raise APIError("last_manager", "The last active manager cannot be deactivated.", status_code=409)
    user.is_active = False
    await session_service.revoke_all_sessions(session, user.id)
    session.add(AuditLog(actor_id=actor.id, action="user.deactivate", entity_type="user", entity_id=user.id))
    await session.commit()
    return {"ok": True}


@router.post("/{user_id}/reactivate")
async def reactivate_user(
    user_id: str,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise APIError("not_found", "User not found.", status_code=404)
    user.is_active = True
    session.add(AuditLog(actor_id=actor.id, action="user.reactivate", entity_type="user", entity_id=user.id))
    await session.commit()
    return {"ok": True}


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise APIError("not_found", "User not found.", status_code=404)
    password = generate_password()
    user.password_hash = hash_password(password)
    user.must_change_password = True
    await session_service.revoke_all_sessions(session, user.id)
    session.add(AuditLog(actor_id=actor.id, action="user.reset_password", entity_type="user", entity_id=user.id))
    await session.commit()
    return {"ok": True, "temporary_password": password}


@router.post("/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: str,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise APIError("not_found", "User not found.", status_code=404)
    count = await session_service.revoke_all_sessions(session, user.id)
    session.add(AuditLog(actor_id=actor.id, action="user.revoke_sessions", entity_type="user", entity_id=user.id,
                         after={"count": count}))
    await session.commit()
    return {"ok": True, "revoked": count}


@router.post("/import")
async def import_users(
    payload: CsvImportRequest,
    actor: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """USR-8: bulk create from CSV with per-row errors."""
    reader = csv.DictReader(io.StringIO(payload.csv.strip()))
    created: list[dict] = []
    errors: list[dict] = []
    default_limit = int(await settings_store.get_setting(session, "default_daily_limit") or 0)
    for index, row in enumerate(reader, start=1):
        email = normalise_email(row.get("email") or row.get("Email") or "")
        role = (row.get("role") or row.get("Role") or "maker").strip().lower()
        name = (row.get("name") or row.get("Name") or "").strip()
        if not email or role not in {"maker", "manager", "reviewer"}:
            errors.append({"row": index, "error": "email and a valid role are required"})
            continue
        existing = (await session.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
        if existing is not None:
            errors.append({"row": index, "error": "email already exists", "email": email})
            continue
        limit_value = row.get("daily_limit") or row.get("limit")
        password = generate_password()
        user = User(
            name=name or email.split("@")[0],
            email=email,
            role=role,
            password_hash=hash_password(password),
            must_change_password=True,
            daily_limit=(int(limit_value) if limit_value else default_limit) if role == "maker" else None,
        )
        session.add(user)
        await session.flush()
        profile_name = (row.get("profile") or "").strip()
        if role == "maker" and profile_name:
            profile = (
                await session.execute(select(Profile).where(Profile.name == profile_name))
            ).scalar_one_or_none()
            if profile is None:
                errors.append({"row": index, "error": f"profile not found: {profile_name}", "email": email})
            else:
                session.add(ProfileAssignment(profile_id=profile.id, maker_id=user.id, assigned_by=actor.id))
        created.append({"email": email, "role": role, "temporary_password": password})
    session.add(AuditLog(actor_id=actor.id, action="user.import", entity_type="user",
                         after={"created": len(created), "errors": len(errors)}))
    await session.commit()
    return {"created": created, "errors": errors}
