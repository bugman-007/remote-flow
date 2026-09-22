"""JD intake and job endpoints (§7)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, current_user, maker_required, manager_required
from app.errors import APIError
from app.models import AuditLog, DocSet, Generation, GenerationAttempt, Job, User
from app.schemas import RetryRequest, SubmitJobRequest
from app.serializers import attempt_out, generation_out, job_out
from app.services import dispatch, events, intake, pipeline, release, settings_store
from app.services.derived import derived_status
from app.services.search import job_text_search_clause
from app.utils import utcnow

router = APIRouter(tags=["jobs"], dependencies=[Depends(csrf_protect)])

PROCESSING_EXPIRED = {"queued", "llm_running", "rendering"}


def _status_filter(status: str | None):
    if not status or status == "all":
        return None
    return status


async def _duplicate_seqs(session: AsyncSession, jobs: list[Job]) -> dict[str, int]:
    """JD-8: resolve ``duplicate_of`` job ids to their seq numbers for the badge."""
    ids = {job.duplicate_of for job in jobs if job.duplicate_of}
    if not ids:
        return {}
    rows = (await session.execute(select(Job.id, Job.seq_no).where(Job.id.in_(ids)))).all()
    return {row[0]: row[1] for row in rows}


async def _doc_sets_by_job(session: AsyncSession, job_ids: list[str]) -> dict[str, DocSet]:
    if not job_ids:
        return {}
    rows = (await session.execute(select(DocSet).where(DocSet.job_id.in_(job_ids)))).scalars().all()
    return {row.job_id: row for row in rows}


@router.post("/jobs", status_code=201)
async def submit_job(
    payload: SubmitJobRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(maker_required),
    session: AsyncSession = Depends(get_session),
):
    try:
        job, created = await intake.submit_jd(
            session, maker=user, jd_text=payload.jd_text, idempotency_key=idempotency_key
        )
    except intake.IntakeError as exc:
        raise APIError(exc.code, exc.message, status_code=exc.status_code) from exc
    await session.commit()
    await dispatch.flush_dispatches(session)
    return {"job_id": job.id, "seq_no": job.seq_no, "created": created, "duplicate_of": job.duplicate_of}


@router.get("/jobs")
async def list_jobs(
    day: date | None = Query(default=None, alias="date"),
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    q: str | None = None,
    maker_id: str | None = None,
    profile_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import Pagination

    pagination = Pagination(page, page_size)
    query = select(Job)
    if user.role == "maker":
        query = query.where(Job.maker_id == user.id)
    elif maker_id:
        query = query.where(Job.maker_id == maker_id)
    if user.role == "reviewer":
        raise APIError("forbidden", "Reviewers cannot browse doc sets.", status_code=403)
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
        doc_set_query = select(DocSet.job_id).where(
            or_(
                DocSet.company_name.ilike(f"%{term}%"),
                DocSet.job_title.ilike(f"%{term}%"),
                DocSet.candidate_name.ilike(f"%{term}%"),
            )
        )
        clauses = [job_text_search_clause(Job, term), Job.id.in_(doc_set_query)]
        cleaned = term.lstrip("#")
        if cleaned.isdigit():
            clauses.append(Job.seq_no == int(cleaned))
        query = query.where(or_(*clauses))
    jobs = (await session.execute(query.order_by(Job.submitted_date.desc(), Job.seq_no.desc()))).scalars().all()
    doc_sets = await _doc_sets_by_job(session, [job.id for job in jobs])
    duplicate_seqs = await _duplicate_seqs(session, jobs)
    blocker_cache: dict[str, dict | None] = {}
    items = []
    wanted = _status_filter(status)
    for job in jobs:
        doc_set = doc_sets.get(job.id)
        if job.maker_id not in blocker_cache:
            blocker_cache[job.maker_id] = await release.maker_blocker(session, job.maker_id)
        initial = await release.initial_generation(session, job)
        derived = derived_status(job, initial, blocker=blocker_cache[job.maker_id])
        if wanted and not _matches_status_filter(wanted, job, derived, doc_set, initial):
            continue
        items.append(job_out(job, derived=derived, doc_set=doc_set, duplicate_seq=duplicate_seqs.get(job.duplicate_of or "")))
    total = len(items)
    paged = items[pagination.offset : pagination.offset + pagination.page_size]
    return {"items": paged, "pagination": pagination.as_dict(total)}


def _matches_status_filter(wanted: str, job: Job, derived: dict, doc_set: DocSet | None, initial: Generation | None) -> bool:
    if wanted == "ready":
        return job.delivery_status == "released"
    if wanted == "skipped":
        return job.delivery_status == "skipped"
    if wanted == "processing":
        return job.delivery_status == "pending" and initial is not None and initial.status in PROCESSING_EXPIRED
    if wanted == "retrying":
        return job.delivery_status == "pending" and initial is not None and initial.status == "retry_wait"
    if wanted == "attention":
        return job.delivery_status == "pending" and initial is not None and initial.status == "needs_attention"
    if wanted == "expired":
        return bool(doc_set and doc_set.files_expired_at)
    if wanted == "selected":
        return bool(doc_set and doc_set.is_selected)
    if wanted == "duplicates":
        return job.duplicate_of is not None
    return True


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import ensure_maker_scope, get_job as fetch_job

    job = await fetch_job(session, job_id)

    ensure_maker_scope(user, job)
    doc_set = (await session.execute(select(DocSet).where(DocSet.job_id == job.id))).scalar_one_or_none()
    initial = await release.initial_generation(session, job)
    generation_count = int(
        (await session.execute(select(func.count(Generation.id)).where(Generation.job_id == job.id))).scalar_one() or 0
    )
    derived = derived_status(job, initial, generation_count=generation_count)
    payload = job_out(job, derived=derived, doc_set=doc_set)
    if user.role == "manager":
        payload["jd_text"] = job.jd_text
        payload["jd_hash"] = job.jd_hash
    return payload


@router.get("/jobs/{job_id}/generations")
async def job_generations(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import ensure_maker_scope, get_job

    job = await get_job(session, job_id)
    ensure_maker_scope(user, job)
    doc_set = (await session.execute(select(DocSet).where(DocSet.job_id == job.id))).scalar_one_or_none()
    generations = await release_generations(session, job.id)
    if user.role == "maker":
        generations = [g for g in generations if doc_set and g.id == doc_set.current_generation_id]
    return {"items": [generation_out(generation) for generation in generations]}


async def release_generations(session: AsyncSession, job_id: str):
    return (
        await session.execute(
            select(Generation).where(Generation.job_id == job_id).order_by(Generation.generation_no)
        )
    ).scalars().all()


@router.get("/jobs/{job_id}/attempts")
async def job_attempts(
    job_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import get_job

    await get_job(session, job_id)
    attempts = (
        await session.execute(
            select(GenerationAttempt)
            .join(Generation, Generation.id == GenerationAttempt.generation_id)
            .where(Generation.job_id == job_id)
            .order_by(GenerationAttempt.started_at)
        )
    ).scalars().all()
    return {"items": [attempt_out(attempt) for attempt in attempts]}


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    payload: RetryRequest,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import get_job

    job = await get_job(session, job_id)
    try:
        generation = await pipeline.retry_now(session, job=job, mode=payload.mode, actor_id=user.id)
    except ValueError as exc:
        raise APIError("invalid_retry", str(exc), status_code=409) from exc
    session.add(
        AuditLog(actor_id=user.id, action=f"job.retry.{payload.mode}", entity_type="job", entity_id=job.id, ip=client_ip(request))
    )
    await session.commit()
    await dispatch.flush_dispatches(session)
    return {"ok": True, "generation_id": generation.id if generation else None}


@router.post("/jobs/{job_id}/skip")
async def skip_job(
    job_id: str,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    from app.routers.common import get_job

    job = await get_job(session, job_id)
    await pipeline.skip_job(session, job=job, actor_id=user.id)
    session.add(AuditLog(actor_id=user.id, action="job.skip", entity_type="job", entity_id=job.id, ip=client_ip(request)))
    await session.commit()
    await dispatch.flush_dispatches(session)
    return {"ok": True}


@router.get("/me/limit")
async def my_limit(user: User = Depends(maker_required), session: AsyncSession = Depends(get_session)):
    day = await intake.server_today(session)
    used = await intake.usage_today(session, user.id, day)
    settings = await settings_store.all_settings(session)
    return {
        "daily_limit": user.daily_limit,
        "used": used,
        "remaining": None if user.daily_limit is None else max(user.daily_limit - used, 0),
        "date": day,
        "paused": (await settings_store.intake_paused(session))[0],
        "min_jd_chars": int(settings["min_jd_chars"]),
        "max_jd_chars": int(settings["max_jd_chars"]),
    }


@router.get("/me/eta")
async def my_eta(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    pending = (
        await session.execute(
            select(func.count(Job.id)).where(Job.maker_id == user.id, Job.delivery_status == "pending")
        )
    ).scalar_one()
    estimate = await estimate_pipeline(session, int(pending or 0))
    estimate["pending_for_maker"] = int(pending or 0)
    return estimate


async def estimate_pipeline(session: AsyncSession, queue_depth: int) -> dict:
    """CONC-7 / JD-11 estimate from measured latency and provider concurrency."""
    from app.models import LLMProvider

    mean_llm_ms = (
        await session.execute(
            select(func.avg(GenerationAttempt.latency_ms)).where(
                GenerationAttempt.stage == "llm", GenerationAttempt.latency_ms.is_not(None)
            )
        )
    ).scalar_one()
    mean_render_ms = (
        await session.execute(
            select(func.avg(GenerationAttempt.latency_ms)).where(
                GenerationAttempt.stage == "render", GenerationAttempt.latency_ms.is_not(None)
            )
        )
    ).scalar_one()
    concurrency = (
        await session.execute(
            select(func.coalesce(func.sum(LLMProvider.max_concurrency), 8)).where(LLMProvider.is_enabled.is_(True))
        )
    ).scalar_one()
    llm_s = (float(mean_llm_ms) if mean_llm_ms else 30_000) / 1000
    render_s = (float(mean_render_ms) if mean_render_ms else 1500) / 1000
    settings = await settings_store.all_settings(session)
    total_pending = (
        await session.execute(select(func.count(Job.id)).where(Job.delivery_status == "pending"))
    ).scalar_one()
    depth = max(int(total_pending or 0), queue_depth)
    eta = max(depth * llm_s / max(int(concurrency or 8), 1), depth * render_s / max(int(settings.get("render_concurrency", 1) or 1), 1))
    return {
        "pending_jobs": depth,
        "eta_seconds": int(eta),
        "mean_llm_ms": int(llm_s * 1000),
        "effective_provider_concurrency": int(concurrency or 8),
        "estimate": True,
    }
