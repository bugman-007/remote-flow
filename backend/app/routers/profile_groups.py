"""Profile groups (PRO-11): named, coloured sets of Profiles; one group per Profile."""

from __future__ import annotations

import colorsys
import random

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, manager_required
from app.errors import APIError
from app.models import AuditLog, Profile, ProfileGroup, User
from app.schemas import ProfileGroupCreate, ProfileGroupMembers, ProfileGroupUpdate

router = APIRouter(prefix="/profile-groups", tags=["profiles"], dependencies=[Depends(csrf_protect)])

#: Distinct, readable label colours; a new group takes one no other group uses.
PALETTE = (
    "#ef4444", "#f97316", "#f59e0b", "#84cc16", "#22c55e", "#14b8a6", "#06b6d4", "#3b82f6",
    "#6366f1", "#8b5cf6", "#a855f7", "#d946ef", "#ec4899", "#f43f5e", "#0ea5e9", "#10b981",
)


def pick_color(used: set[str]) -> str:
    free = [color for color in PALETTE if color not in used]
    if free:
        return random.choice(free)
    red, green, blue = colorsys.hls_to_rgb(random.random(), 0.5, 0.65)
    return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def group_out(group: ProfileGroup, profile_ids: list[str]) -> dict:
    return {
        "id": group.id,
        "name": group.name,
        "color": group.color,
        "profile_ids": profile_ids,
        "profile_count": len(profile_ids),
        "created_at": group.created_at,
    }


async def _members(session: AsyncSession, group_id: str) -> list[str]:
    return list(
        (await session.execute(select(Profile.id).where(Profile.group_id == group_id).order_by(Profile.name)))
        .scalars()
        .all()
    )


async def _load(session: AsyncSession, group_id: str) -> ProfileGroup:
    group = await session.get(ProfileGroup, group_id)
    if group is None:
        raise APIError("not_found", "Group not found.", status_code=404)
    return group


async def _by_name(session: AsyncSession, name: str) -> ProfileGroup | None:
    return (
        await session.execute(select(ProfileGroup).where(func.lower(ProfileGroup.name) == name.lower()))
    ).scalar_one_or_none()


async def _profiles(session: AsyncSession, profile_ids: list[str]) -> list[Profile]:
    wanted = list(dict.fromkeys(profile_ids))
    found = (await session.execute(select(Profile).where(Profile.id.in_(wanted)))).scalars().all()
    if len(found) != len(wanted):
        raise APIError("not_found", "One or more profiles were not found.", status_code=404)
    return list(found)


@router.get("")
async def list_groups(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    groups = (await session.execute(select(ProfileGroup).order_by(ProfileGroup.name))).scalars().all()
    return {"items": [group_out(group, await _members(session, group.id)) for group in groups]}


@router.post("", status_code=201)
async def save_group(
    payload: ProfileGroupCreate,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """Create the group, or add to the group that already has this name.

    A Profile belongs to one group, so the chosen Profiles leave their old group.
    """
    name = payload.name.strip()
    if not name:
        raise APIError("validation_error", "Enter a group name.", status_code=422)
    profiles = await _profiles(session, payload.profile_ids)
    group = await _by_name(session, name)
    created = group is None
    if group is None:
        used = set((await session.execute(select(ProfileGroup.color))).scalars().all())
        group = ProfileGroup(name=name, color=pick_color(used), created_by=user.id)
        session.add(group)
        await session.flush()
    for profile in profiles:
        profile.group_id = group.id
    session.add(
        AuditLog(
            actor_id=user.id,
            action="profile_group.create" if created else "profile_group.add",
            entity_type="profile_group",
            entity_id=group.id,
            after={"name": group.name, "profile_ids": [profile.id for profile in profiles]},
            ip=client_ip(request),
        )
    )
    await session.commit()
    return group_out(group, await _members(session, group.id))


@router.patch("/{group_id}")
async def rename_group(
    group_id: str,
    payload: ProfileGroupUpdate,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    group = await _load(session, group_id)
    name = payload.name.strip()
    if not name:
        raise APIError("validation_error", "Enter a group name.", status_code=422)
    clash = await _by_name(session, name)
    if clash is not None and clash.id != group.id:
        raise APIError("duplicate_name", "Another group already has this name.", status_code=409)
    before = group.name
    group.name = name
    session.add(
        AuditLog(actor_id=user.id, action="profile_group.rename", entity_type="profile_group", entity_id=group.id,
                 before={"name": before}, after={"name": name}, ip=client_ip(request))
    )
    await session.commit()
    return group_out(group, await _members(session, group.id))


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """Delete the group; its Profiles simply have no group afterwards."""
    group = await _load(session, group_id)
    members = await _members(session, group.id)
    await session.execute(update(Profile).where(Profile.group_id == group.id).values(group_id=None))
    session.add(
        AuditLog(actor_id=user.id, action="profile_group.delete", entity_type="profile_group", entity_id=group.id,
                 before={"name": group.name, "profile_ids": members}, ip=client_ip(request))
    )
    await session.delete(group)
    await session.commit()
    return {"ok": True}


@router.post("/{group_id}/remove")
async def remove_from_group(
    group_id: str,
    payload: ProfileGroupMembers,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    group = await _load(session, group_id)
    await session.execute(
        update(Profile)
        .where(Profile.group_id == group.id, Profile.id.in_(payload.profile_ids))
        .values(group_id=None)
    )
    session.add(
        AuditLog(actor_id=user.id, action="profile_group.remove", entity_type="profile_group", entity_id=group.id,
                 after={"profile_ids": payload.profile_ids}, ip=client_ip(request))
    )
    await session.commit()
    return group_out(group, await _members(session, group.id))
