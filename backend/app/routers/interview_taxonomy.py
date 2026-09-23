"""INT-14: the Manager's ordered interview steps and status labels."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import csrf_protect, manager_required
from app.errors import APIError
from app.models import InterviewStep, InterviewStatus, User
from app.schemas import TaxonomyRequest, TaxonomyUpdate
from app.services.taxonomy import status_out, step_out

router = APIRouter(tags=["interview-taxonomy"], dependencies=[Depends(csrf_protect)])

MODELS = {"steps": InterviewStep, "statuses": InterviewStatus}
OUT = {"steps": step_out, "statuses": status_out}


def _model(kind: str):
    model = MODELS.get(kind)
    if model is None:
        raise APIError("not_found", "Unknown taxonomy.", status_code=404)
    return model


async def _load(session: AsyncSession, kind: str, row_id: str):
    model = _model(kind)
    row = await session.get(model, row_id)
    if row is None:
        raise APIError("not_found", "Not found.", status_code=404)
    return row


@router.get("/interview-taxonomy")
async def list_taxonomy(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    """One call for the filter bar and pickers."""
    steps = (
        await session.execute(select(InterviewStep).order_by(InterviewStep.position, InterviewStep.name))
    ).scalars().all()
    statuses = (
        await session.execute(select(InterviewStatus).order_by(InterviewStatus.position, InterviewStatus.name))
    ).scalars().all()
    return {"steps": [step_out(row) for row in steps], "statuses": [status_out(row) for row in statuses]}


async def _list(kind: str, session: AsyncSession) -> dict:
    model = _model(kind)
    rows = (await session.execute(select(model).order_by(model.position, model.name))).scalars().all()
    return {"items": [OUT[kind](row) for row in rows]}


async def _create(kind: str, payload: TaxonomyRequest, user: User, session: AsyncSession) -> dict:
    model = _model(kind)
    existing = (
        await session.execute(select(model).where(model.name == payload.name.strip()))
    ).scalar_one_or_none()
    if existing is not None:
        raise APIError("duplicate_name", "That name is already in use.", status_code=409)
    highest = (
        await session.execute(select(model.position).order_by(model.position.desc()).limit(1))
    ).scalar_one_or_none()
    row = model(
        name=payload.name.strip(),
        color=payload.color,
        position=(int(highest or 0) + 1) if highest is not None else 0,
    )
    session.add(row)
    await session.commit()
    return OUT[kind](row)


async def _update(kind: str, row_id: str, payload: TaxonomyUpdate, session: AsyncSession) -> dict:
    row = await _load(session, kind, row_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"]:
        clash = (
            await session.execute(
                select(_model(kind)).where(_model(kind).name == data["name"].strip(), _model(kind).id != row.id)
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise APIError("duplicate_name", "That name is already in use.", status_code=409)
        row.name = data["name"].strip()
    if data.get("color") is not None:
        row.color = data["color"]
    if data.get("position") is not None:
        row.position = int(data["position"])
    if data.get("is_active") is not None:
        row.is_active = bool(data["is_active"])
    await session.commit()
    return OUT[kind](row)


async def _delete(kind: str, row_id: str, session: AsyncSession) -> dict:
    row = await _load(session, kind, row_id)
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/interview-steps")
async def list_steps(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    return await _list("steps", session)


@router.post("/interview-steps", status_code=201)
async def create_step(
    payload: TaxonomyRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _create("steps", payload, user, session)


@router.patch("/interview-steps/{row_id}")
async def update_step(
    row_id: str,
    payload: TaxonomyUpdate,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _update("steps", row_id, payload, session)


@router.delete("/interview-steps/{row_id}")
async def delete_step(
    row_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _delete("steps", row_id, session)


@router.get("/interview-statuses")
async def list_statuses(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    return await _list("statuses", session)


@router.post("/interview-statuses", status_code=201)
async def create_status(
    payload: TaxonomyRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _create("statuses", payload, user, session)


@router.patch("/interview-statuses/{row_id}")
async def update_status(
    row_id: str,
    payload: TaxonomyUpdate,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _update("statuses", row_id, payload, session)


@router.delete("/interview-statuses/{row_id}")
async def delete_status(
    row_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    return await _delete("statuses", row_id, session)
