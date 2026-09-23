"""Resumes page API: listing, detail, selection, regenerate and ZIP downloads (§5.3)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, current_user, manager_required
from app.errors import APIError
from app.models import (
    AuditLog,
    DocSet,
    FileArtifact,
    Generation,
    Interview,
    Job,
    PipelineEvent,
    User,
)
from app.routers.common import Pagination, attempts_for_generation, files_for_generation, get_doc_set, get_job
from app.routers.files import build_zip
from app.schemas import BulkSelectRequest, DocSetPatch, ZipRequest
from app.serializers import doc_set_summary, file_out, generation_out, job_out
from app.services import dispatch, events, pipeline, release, settings_store, storage
from app.services.derived import derived_status
from app.services.search import job_text_search_clause

router = APIRouter(tags=["doc-sets"], dependencies=[Depends(csrf_protect)])


async def row_for(
    session: AsyncSession,
    doc_set: DocSet,
    job: Job,
    *,
    user: User,
    blocker: dict | None = None,
    duplicate_seq: int | None = None,
    interview_count: int = 0,
) -> dict:
    initial = await release.initial_generation(session, job)
    generations = (
        await session.execute(
            select(Generation).where(Generation.job_id == job.id).order_by(Generation.generation_no)
        )
    ).scalars().all()
    derived = derived_status(job, initial, blocker=blocker, generation_count=len(generations))
    files: list[FileArtifact] = []
    if doc_set.current_generation_id:
        files = await files_for_generation(session, doc_set.current_generation_id)
        if user.role == "maker":
            files = [f for f in files if f.kind in {"pdf", "docx", "txt"}]
    row = job_out(job, derived=derived, doc_set=doc_set, duplicate_seq=duplicate_seq)
    row.update(
        {
            "doc_set_id": doc_set.id,
            "generation_count": len(generations),
            "files": [file_out(f) for f in files],
            "is_selected": doc_set.is_selected,
            "keep": doc_set.keep,
            "expired": doc_set.files_expired_at is not None,
            "downloaded_at": doc_set.downloaded_at,
            "interview_count": interview_count,
        }
    )
    return row


@router.get("/doc-sets")
async def list_doc_sets(
    day: date | None = Query(default=None, alias="date"),
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    q: str | None = None,
    maker_id: list[str] | None = Query(default=None),
    profile_id: str | None = None,
    selected: bool | None = None,
    duplicates: bool = False,
    provider_id: str | None = None,
    multi_generation: bool = False,
    kept: bool | None = None,
    attention: str | None = None,
    sort: str = "seq",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.role == "reviewer":
        raise APIError("forbidden", "Reviewers cannot browse doc sets.", status_code=403)
    pagination = Pagination(page, page_size)
    query = select(DocSet, Job).join(Job, Job.id == DocSet.job_id)
    if user.role == "maker":
        query = query.where(Job.maker_id == user.id)
    elif maker_id:
        query = query.where(Job.maker_id.in_(maker_id))
    if profile_id:
        query = query.where(Job.profile_id == profile_id)
    if day:
        query = query.where(Job.submitted_date == day)
    else:
        if date_from:
            query = query.where(Job.submitted_date >= date_from)
        if date_to:
            query = query.where(Job.submitted_date <= date_to)
    if q:
        term = q.strip()
        cleaned = term.lstrip("#")
        clauses = [
            DocSet.company_name.ilike(f"%{term}%"),
            DocSet.job_title.ilike(f"%{term}%"),
            DocSet.candidate_name.ilike(f"%{term}%"),
            DocSet.search_tsv.ilike(f"%{term.lower()}%"),
            job_text_search_clause(Job, term),
        ]
        if cleaned.isdigit():
            clauses.append(Job.seq_no == int(cleaned))
        query = query.where(or_(*clauses))
    if selected is not None:
        query = query.where(DocSet.is_selected.is_(selected))
    if kept is not None:
        query = query.where(DocSet.keep.is_(kept))
    if duplicates:
        query = query.where(Job.duplicate_of.is_not(None))
    if provider_id:
        query = query.join(Generation, Generation.job_id == Job.id).where(Generation.provider_id == provider_id)
    rows = (
        await session.execute(query.order_by(Job.submitted_date.desc(), Job.seq_no.desc()))
    ).all()
    from app.routers.jobs import _duplicate_seqs

    duplicate_seqs = await _duplicate_seqs(session, [job for _doc_set, job in rows])
    interview_counts = await _interview_counts(session, [doc_set.id for doc_set, _job in rows])
    blockers: dict[str, dict | None] = {}
    items: list[dict] = []
    for doc_set, job in rows:
        if job.maker_id not in blockers:
            blockers[job.maker_id] = await release.maker_blocker(session, job.maker_id)
        payload = await row_for(
            session,
            doc_set,
            job,
            user=user,
            blocker=blockers[job.maker_id],
            duplicate_seq=duplicate_seqs.get(job.duplicate_of or ""),
            interview_count=interview_counts.get(doc_set.id, 0),
        )
        if status and status != "all" and not _matches_status(status, job, payload, doc_set):
            continue
        if multi_generation and payload["generation_count"] <= 1:
            continue
        if attention:
            initial = await release.initial_generation(session, job)
            if initial is None or initial.status != "needs_attention":
                continue
            if attention == "regeneration" and initial.kind != "regenerate":
                continue
            if attention == "blocking" and initial.kind == "regenerate":
                continue
        items.append(payload)
    items = _sorted_rows(items, sort=sort, order=order)
    total = len(items)
    return {"items": items[pagination.offset : pagination.offset + pagination.page_size], "pagination": pagination.as_dict(total)}


async def _interview_counts(session: AsyncSession, doc_set_ids: list[str]) -> dict[str, int]:
    """INT-3: interviews that currently pin the doc set's generation."""
    if not doc_set_ids:
        return {}
    rows = (
        await session.execute(
            select(Interview.doc_set_id, func.count(Interview.id))
            .join(DocSet, DocSet.id == Interview.doc_set_id)
            .where(
                Interview.doc_set_id.in_(doc_set_ids),
                Interview.generation_id == DocSet.current_generation_id,
            )
            .group_by(Interview.doc_set_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


SORT_KEYS = {
    "seq": lambda row: (row["submitted_date"] or "", row["seq_no"]),
    "submitted": lambda row: (str(row.get("submitted_at") or row["submitted_date"] or ""), row["seq_no"]),
    "company": lambda row: ((row.get("doc_set") or {}).get("company_name") or "").lower(),
    "status": lambda row: ((row.get("status") or {}).get("status") or "", row["seq_no"]),
    "ready": lambda row: (row.get("released_at") or "", row["seq_no"]),
}


def _sorted_rows(items: list[dict], *, sort: str = "seq", order: str = "asc") -> list[dict]:
    """RES-13: sort the (already filtered) page rows; stable and None-safe."""
    key = SORT_KEYS.get(sort or "seq", SORT_KEYS["seq"])
    return sorted(items, key=key, reverse=(order or "asc").lower() == "desc")


def _matches_status(status: str, job: Job, payload: dict, doc_set: DocSet) -> bool:
    derived = payload.get("status") or {}
    mapping = {
        "ready": lambda: job.delivery_status == "released",
        "skipped": lambda: job.delivery_status == "skipped",
        "processing": lambda: derived.get("status") in {"queued", "llm_running", "rendering"},
        "retrying": lambda: derived.get("status") == "retry_wait",
        "attention": lambda: derived.get("status") == "needs_attention",
        "expired": lambda: doc_set.files_expired_at is not None,
        "selected": lambda: doc_set.is_selected,
        "waiting": lambda: derived.get("status") == "waiting",
        "new": lambda: doc_set.downloaded_at is None,
    }
    check = mapping.get(status)
    return check() if check else True


@router.get("/doc-sets/zip")
async def zip_date(
    request: Request,
    day: date | None = Query(default=None, alias="date"),
    date_from: date | None = None,
    date_to: date | None = None,
    maker_id: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.role == "reviewer":
        raise APIError("forbidden", "Reviewers cannot bulk download.", status_code=403)
    query = select(DocSet).join(Job, Job.id == DocSet.job_id).where(Job.delivery_status == "released")
    if day:
        query = query.where(Job.submitted_date == day)
    else:
        if date_from:
            query = query.where(Job.submitted_date >= date_from)
        if date_to:
            query = query.where(Job.submitted_date <= date_to)
    if user.role == "maker":
        query = query.where(Job.maker_id == user.id)
    elif maker_id:
        query = query.where(Job.maker_id == maker_id)
    doc_sets = (await session.execute(query.order_by(Job.seq_no))).scalars().all()
    if not doc_sets:
        raise APIError("nothing_to_download", "No ready doc sets for that date.", status_code=409)
    first_job = await session.get(Job, doc_sets[0].job_id)
    maker = await session.get(User, first_job.maker_id) if first_job else None
    response, _skipped = await build_zip(session, user, list(doc_sets), ip=client_ip(request))
    maker_name = maker.name if maker else "maker"
    filename = (
        storage.zip_name_for_date(maker_name, day)
        if day
        else storage.zip_name_for_range(maker_name, date_from, date_to)
    )
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@router.get("/doc-sets/{doc_set_id}")
async def doc_set_detail(
    doc_set_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    job = await get_job(session, doc_set.job_id)
    if user.role == "maker" and job.maker_id != user.id:
        raise APIError("not_found", "Doc set not found.", status_code=404)
    if user.role == "reviewer":
        raise APIError("forbidden", "Reviewers cannot browse doc sets.", status_code=403)
    generations = (
        await session.execute(
            select(Generation).where(Generation.job_id == job.id).order_by(Generation.generation_no)
        )
    ).scalars().all()
    generation_payloads = []
    for generation in generations:
        if user.role == "maker" and generation.id != doc_set.current_generation_id:
            continue
        attempts = await attempts_for_generation(session, generation.id) if user.role == "manager" else []
        files = await files_for_generation(session, generation.id)
        generation_payloads.append(generation_out(generation, attempts=attempts, files=[file_out(f) for f in files]))
    payload = {
        "doc_set": doc_set_summary(doc_set) | {
            "job_id": doc_set.job_id,
            "storage_dir": doc_set.storage_dir if user.role == "manager" else None,
            "is_selected": doc_set.is_selected,
            "extra": {"is_selected": doc_set.is_selected, "keep": doc_set.keep},
        },
        "job": job_out(job),
        "generations": generation_payloads,
        "interview_count": 0,
        "interviews": [],
    }
    interviews = (await session.execute(select(Interview).where(Interview.doc_set_id == doc_set.id))).scalars().all()
    payload["interview_count"] = len(interviews)
    payload["interviews"] = [
        {"id": interview.id, "generation_id": interview.generation_id, "status": interview.status,
         "reviewer_id": interview.reviewer_id, "meeting_at": interview.meeting_at}
        for interview in interviews
    ]
    if user.role == "manager":
        payload["jd_text"] = job.jd_text
        payload["renamed_by"] = doc_set.renamed_by
        timeline = (
            await session.execute(
                select(PipelineEvent)
                .where(PipelineEvent.job_id == job.id)
                .order_by(PipelineEvent.at)
                .limit(500)
            )
        ).scalars().all()
        payload["timeline"] = [
            {
                "from_state": item.from_state,
                "to_state": item.to_state,
                "stage": item.stage,
                "attempt_no": item.attempt_no,
                "actor": item.actor,
                "details": item.details,
                "at": item.at,
            }
            for item in timeline
        ]
        payload["llm_json"] = await _current_llm_json(session, doc_set)
    # SEC-7: doc sets hold personal data, so opening one is recorded.
    session.add(
        AuditLog(
            actor_id=user.id,
            action="docset.view",
            entity_type="doc_set",
            entity_id=doc_set.id,
            ip=client_ip(request),
        )
    )
    await session.commit()
    return payload


async def _current_llm_json(session: AsyncSession, doc_set: DocSet) -> dict | None:
    generation = await session.get(Generation, doc_set.current_generation_id) if doc_set.current_generation_id else None
    if generation is None:
        return None
    path = await pipeline.llm_json_path(session, generation)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@router.patch("/doc-sets/{doc_set_id}")
async def patch_doc_set(
    doc_set_id: str,
    payload: DocSetPatch,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    before = {"company_name": doc_set.company_name, "job_title": doc_set.job_title, "keep": doc_set.keep}
    if payload.company_name is not None:
        doc_set.company_name = payload.company_name
    if payload.job_title is not None:
        doc_set.job_title = payload.job_title
    if payload.keep is not None:
        doc_set.keep = payload.keep
    doc_set.renamed_by = user.id
    session.add(
        AuditLog(
            actor_id=user.id,
            action="docset.update",
            entity_type="doc_set",
            entity_id=doc_set.id,
            before=before,
            after={"company_name": doc_set.company_name, "job_title": doc_set.job_title, "keep": doc_set.keep},
            ip=client_ip(request),
        )
    )
    await session.commit()
    return {"ok": True, "doc_set": doc_set_summary(doc_set)}


async def _set_selected(session: AsyncSession, doc_set: DocSet, selected: bool, user: User) -> None:
    from app.utils import utcnow

    doc_set.is_selected = selected
    doc_set.selected_by = user.id if selected else None
    doc_set.selected_at = utcnow() if selected else None
    job = await session.get(Job, doc_set.job_id)
    await events.emit(
        session,
        audience=events.managers_only() | {"user_ids": [job.maker_id] if job else []},
        event_type="docset.selected",
        payload={"doc_set_id": doc_set.id, "is_selected": selected},
        job_id=doc_set.job_id,
    )


@router.post("/doc-sets/{doc_set_id}/select")
async def select_doc_set(
    doc_set_id: str,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    await _set_selected(session, doc_set, True, user)
    session.add(
        AuditLog(actor_id=user.id, action="docset.select", entity_type="doc_set", entity_id=doc_set.id, ip=client_ip(request))
    )
    await session.commit()
    return {"ok": True, "is_selected": True}


@router.delete("/doc-sets/{doc_set_id}/select")
async def unselect_doc_set(
    doc_set_id: str,
    confirm: bool = False,
    request: Request = None,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    interviews = (await session.execute(select(Interview).where(Interview.doc_set_id == doc_set.id))).scalars().all()
    if interviews and not confirm:
        raise APIError(
            "confirmation_required",
            "This doc set has interviews; unselecting does not delete them.",
            status_code=409,
            details={"interview_count": len(interviews)},
        )
    await _set_selected(session, doc_set, False, user)
    session.add(
        AuditLog(actor_id=user.id, action="docset.unselect", entity_type="doc_set", entity_id=doc_set.id,
                 ip=client_ip(request) if request else None)
    )
    await session.commit()
    return {"ok": True, "is_selected": False}


@router.post("/doc-sets/bulk-select")
async def bulk_select(
    payload: BulkSelectRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    updated = 0
    for doc_set_id in payload.ids:
        doc_set = await session.get(DocSet, doc_set_id)
        if doc_set is None:
            continue
        await _set_selected(session, doc_set, payload.selected, user)
        updated += 1
    session.add(
        AuditLog(actor_id=user.id, action="docset.bulk_select", entity_type="doc_set",
                 after={"ids": payload.ids, "selected": payload.selected})
    )
    await session.commit()
    return {"ok": True, "updated": updated}


@router.post("/doc-sets/{doc_set_id}/regenerate")
async def regenerate_doc_set(
    doc_set_id: str,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    job = await get_job(session, doc_set.job_id)
    try:
        generation = await pipeline.regenerate(session, doc_set=doc_set, job=job, actor_id=user.id)
    except ValueError as exc:
        raise APIError("invalid_regenerate", str(exc), status_code=409) from exc
    session.add(
        AuditLog(actor_id=user.id, action="docset.regenerate", entity_type="doc_set", entity_id=doc_set.id,
                 ip=client_ip(request))
    )
    await session.commit()
    await dispatch.flush_dispatches(session)
    return {"ok": True, "generation_id": generation.id, "generation_no": generation.generation_no}


@router.post("/generations/{generation_id}/cancel")
async def cancel_generation(
    generation_id: str,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    generation = await session.get(Generation, generation_id)
    if generation is None:
        raise APIError("not_found", "Generation not found.", status_code=404)
    try:
        await pipeline.cancel_generation(session, generation=generation, actor_id=user.id)
    except ValueError as exc:
        raise APIError("invalid_cancel", str(exc), status_code=409) from exc
    session.add(
        AuditLog(actor_id=user.id, action="generation.cancel", entity_type="generation", entity_id=generation.id,
                 ip=client_ip(request))
    )
    await session.commit()
    return {"ok": True}


@router.get("/doc-sets/{doc_set_id}/zip")
async def zip_doc_set(
    doc_set_id: str,
    request: Request,
    generation: str | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    doc_set = await get_doc_set(session, doc_set_id)
    job = await get_job(session, doc_set.job_id)
    if user.role == "maker" and job.maker_id != user.id:
        raise APIError("not_found", "Doc set not found.", status_code=404)
    if user.role == "reviewer":
        allowed = (
            await session.execute(
                select(Interview.id).where(
                    Interview.doc_set_id == doc_set.id,
                    Interview.reviewer_id == user.id,
                    Interview.generation_id == (generation or doc_set.current_generation_id),
                )
            )
        ).first()
        if allowed is None:
            raise APIError("forbidden", "Reviewers download only the pinned generation.", status_code=403)
    response, _skipped = await build_zip(session, user, [doc_set], generation_id=generation, ip=client_ip(request))
    return response


@router.post("/doc-sets/zip")
async def zip_selection(
    payload: ZipRequest,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    doc_sets = []
    for doc_set_id in payload.ids:
        doc_set = await session.get(DocSet, doc_set_id)
        if doc_set is None:
            continue
        job = await session.get(Job, doc_set.job_id)
        if job is None:
            continue
        if user.role == "maker" and job.maker_id != user.id:
            continue
        if user.role == "reviewer":
            raise APIError("forbidden", "Reviewers cannot bulk download.", status_code=403)
        doc_sets.append(doc_set)
    if not doc_sets:
        raise APIError("not_found", "No matching doc sets.", status_code=404)
    response, _skipped = await build_zip(session, user, doc_sets, generation_id=payload.generation, ip=client_ip(request))
    return response
