"""Bulk Resumes (BULK-1): upload a CSV, then generate for a group or some Profiles."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, manager_required
from app.errors import APIError
from app.models import AuditLog, BulkBatch, Profile, ProfileGroup, User
from app.schemas import BulkGenerateRequest
from app.services import bulk, dispatch, intake

router = APIRouter(prefix="/bulk-resumes", tags=["bulk"], dependencies=[Depends(csrf_protect)])


def _api_error(exc: bulk.BulkError | intake.IntakeError) -> APIError:
    return APIError(exc.code, exc.message, status_code=exc.status_code)


@router.get("")
async def list_batches(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    batches = (
        await session.execute(select(BulkBatch).order_by(BulkBatch.created_at.desc()).limit(20))
    ).scalars().all()
    return {"items": [bulk.batch_out(batch) for batch in batches]}


@router.post("/upload", status_code=201)
async def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    data = await file.read(bulk.MAX_CSV_BYTES + 1)
    try:
        batch = await bulk.create_batch(session, data=data, filename=file.filename, actor_id=user.id)
    except bulk.BulkError as exc:
        raise _api_error(exc) from None
    session.add(
        AuditLog(actor_id=user.id, action="bulk.upload", entity_type="bulk_batch", entity_id=batch.id,
                 after={"filename": batch.filename, "usable_rows": len(batch.rows), "rejected_rows": len(batch.rejected)},
                 ip=client_ip(request))
    )
    await session.commit()
    return bulk.batch_out(batch)


@router.post("/{batch_id}/generate")
async def generate(
    batch_id: str,
    payload: BulkGenerateRequest,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    batch = (
        await session.execute(select(BulkBatch).where(BulkBatch.id == batch_id).with_for_update())
    ).scalar_one_or_none()
    if batch is None:
        raise APIError("not_found", "Upload not found.", status_code=404)
    if payload.group_id:
        group = await session.get(ProfileGroup, payload.group_id)
        if group is None:
            raise APIError("not_found", "Group not found.", status_code=404)
        profiles = list(
            (await session.execute(select(Profile).where(Profile.group_id == group.id).order_by(Profile.name)))
            .scalars()
            .all()
        )
    elif payload.profile_ids:
        wanted = list(dict.fromkeys(payload.profile_ids))
        found = {
            profile.id: profile
            for profile in (await session.execute(select(Profile).where(Profile.id.in_(wanted)))).scalars().all()
        }
        if len(found) != len(wanted):
            raise APIError("not_found", "One or more profiles were not found.", status_code=404)
        profiles = [found[profile_id] for profile_id in wanted]
    else:
        raise APIError("validation_error", "Choose a group or at least one profile.", status_code=422)
    if not profiles:
        raise APIError("empty_group", "That group has no profiles.", status_code=422)
    if payload.count > len(batch.rows or []):
        raise APIError(
            "validation_error", f"The upload has {len(batch.rows or [])} usable rows.", status_code=422
        )
    try:
        summary = await bulk.generate(session, batch, profiles=profiles, count=payload.count)
    except (bulk.BulkError, intake.IntakeError) as exc:
        await session.rollback()
        raise _api_error(exc) from None
    session.add(
        AuditLog(actor_id=user.id, action="bulk.generate", entity_type="bulk_batch", entity_id=batch.id,
                 after={"count": payload.count, "group_id": payload.group_id,
                        "profile_ids": [profile.id for profile in profiles], "created": summary["created"]},
                 ip=client_ip(request))
    )
    await session.commit()
    await dispatch.flush_dispatches(session)
    return summary
