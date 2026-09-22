"""Ordered release (ORD-1…ORD-9) with a transactional pass under an advisory lock."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import acquire_serialization_lock
from app.models import DocSet, Generation, Job, User
from app.services import events, storage
from app.services.derived import derived_status
from app.utils import slugify, utcnow
from vendor.resume_builder import core

logger = logging.getLogger(__name__)


async def cursor_job(session: AsyncSession, maker_id: str) -> Job | None:
    """ORD-1: the blocker = smallest ``(submitted_date, seq_no)`` still pending."""
    return (
        await session.execute(
            select(Job)
            .where(Job.maker_id == maker_id, Job.delivery_status == "pending")
            .order_by(Job.submitted_date, Job.seq_no)
            .limit(1)
        )
    ).scalar_one_or_none()


async def maker_blocker(session: AsyncSession, maker_id: str) -> dict | None:
    """ORD-6: the maker's cursor job and the state it is stuck in (if any)."""
    blocker = await cursor_job(session, maker_id)
    if blocker is None:
        return None
    initial = await initial_generation(session, blocker)
    return {
        "job_id": blocker.id,
        "seq_no": blocker.seq_no,
        "status": initial.status if initial else "queued",
        "stage": initial.stage if initial else None,
        "attempt": (initial.llm_attempts if initial and initial.stage == "llm" else (initial.render_attempts if initial else 0)),
        "needs_attention": bool(initial and initial.status == "needs_attention"),
    }


async def blocker_info(session: AsyncSession, job: Job) -> dict | None:
    blocker = await maker_blocker(session, job.maker_id)
    if blocker is None or blocker["job_id"] == job.id:
        return None
    return blocker


async def initial_generation(session: AsyncSession, job: Job) -> Generation | None:
    if job.initial_generation_id:
        generation = await session.get(Generation, job.initial_generation_id)
        if generation is not None:
            return generation
    rows = (
        await session.execute(
            select(Generation)
            .where(Generation.job_id == job.id, Generation.kind == "initial")
            .order_by(Generation.generation_no)
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def doc_set_for_job(session: AsyncSession, job_id: str) -> DocSet | None:
    return (
        await session.execute(select(DocSet).where(DocSet.job_id == job_id))
    ).scalar_one_or_none()


async def release_pass(session: AsyncSession, maker_id: str, *, actor: str = "pipeline") -> list[str]:
    """ORD-2: walk from the cursor forward, releasing every ready job in one transaction."""
    await acquire_serialization_lock(session, f"release:{maker_id}")
    released: list[str] = []
    while True:
        job = await cursor_job(session, maker_id)
        if job is None:
            break
        generation = await initial_generation(session, job)
        if generation is None or generation.status != "ready":
            break
        now = utcnow()
        job.delivery_status = "released"
        job.released_at = now
        released.append(job.id)
        doc_set = await doc_set_for_job(session, job.id)
        await events.emit(
            session,
            audience=events.job_audience(job.maker_id),
            event_type="job.released",
            payload={
                "job_id": job.id,
                "seq_no": job.seq_no,
                "doc_set_id": doc_set.id if doc_set else None,
                "status": "released",
                "released_late": bool(job.released_late),
            },
            job_id=job.id,
            generation_id=generation.id,
            pipeline_event={
                "from_state": "pending",
                "to_state": "released",
                "stage": "release",
                "actor": actor,
                "details": {"generation_no": generation.generation_no},
            },
        )
    return released


async def late_release(session: AsyncSession, job: Job, generation: Generation, *, actor: str) -> None:
    """ORD-9 #1: a retried skipped job is delivered late, by explicit Manager action."""
    job.delivery_status = "released"
    job.released_at = utcnow()
    job.released_late = True
    doc_set = await doc_set_for_job(session, job.id)
    await events.emit(
        session,
        audience=events.job_audience(job.maker_id),
        event_type="job.released",
        payload={
            "job_id": job.id,
            "seq_no": job.seq_no,
            "doc_set_id": doc_set.id if doc_set else None,
            "status": "released",
            "released_late": True,
        },
        job_id=job.id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": "skipped",
            "to_state": "released",
            "stage": "release",
            "actor": actor,
            "details": {"released_late": True, "generation_no": generation.generation_no},
        },
    )


async def makers_with_ready_cursor(session: AsyncSession, *, older_than_seconds: int = 10) -> list[str]:
    """ORD-8: makers whose cursor job has a ready initial build (reconciliation scan)."""
    cutoff = utcnow() - timedelta(seconds=older_than_seconds)
    rows = (
        await session.execute(
            select(Generation.job_id)
            .join(Job, Job.id == Generation.job_id)
            .where(
                Generation.kind == "initial",
                Generation.status == "ready",
                Generation.ready_at.is_not(None),
                Generation.ready_at <= cutoff,
                Job.delivery_status == "pending",
            )
        )
    ).scalars().all()
    maker_ids: list[str] = []
    for job_id in rows:
        job = await session.get(Job, job_id)
        if job is None:
            continue
        cursor = await cursor_job(session, job.maker_id)
        if cursor is not None and cursor.id == job.id:
            maker_ids.append(job.maker_id)
    return list(dict.fromkeys(maker_ids))


async def status_payload(session: AsyncSession, job: Job) -> dict:
    initial = await initial_generation(session, job)
    blocker = await blocker_info(session, job)
    generation_count = None
    if job.delivery_status == "released":
        from sqlalchemy import func

        generation_count = int(
            (
                await session.execute(
                    select(func.count(Generation.id)).where(Generation.job_id == job.id)
                )
            ).scalar_one()
            or 0
        )
    derived = derived_status(
        job, initial, blocker_seq=blocker["seq_no"] if blocker else None, generation_count=generation_count
    )
    payload = {
        "job_id": job.id,
        "seq_no": job.seq_no,
        "maker_id": job.maker_id,
        "status": derived["status"],
        "label": derived["label"],
        "stage": derived.get("stage"),
        "attempt": derived.get("attempt"),
        "generation": derived.get("generation"),
        "released_late": derived.get("released_late", False),
    }
    if blocker:
        payload["blocker"] = blocker
    return payload


async def promote_doc_set_dir(session: AsyncSession, doc_set: DocSet, job: Job, company_name: str) -> None:
    """STO-1: rename ``{seq:03d}-pending`` to ``{seq:03d}-{company_slug}`` once known."""
    from sqlalchemy import text

    new_slug = slugify(company_name)
    if not doc_set.storage_dir or doc_set.slug == new_slug:
        return
    root = storage.storage_root()
    old_rel = doc_set.storage_dir
    old_abs = root / old_rel
    parent = old_abs.parent
    new_abs = parent / f"{job.seq_no:03d}-{new_slug}"
    if old_abs.exists() and not new_abs.exists():
        old_abs.rename(new_abs)
    new_rel = storage.relative_to_root(new_abs)
    old_prefix = old_rel
    for table, column in (
        ("files", "path"),
        ("generations", "storage_dir"),
        ("generation_attempts", "artifacts_dir"),
    ):
        await session.execute(
            text(
                f"UPDATE {table} SET {column} = replace({column}, :old, :new) "
                f"WHERE {column} LIKE :pattern"
            ),
            {"old": old_prefix, "new": new_rel, "pattern": f"{old_prefix}%"},
        )
    doc_set.storage_dir = new_rel
    doc_set.slug = new_slug


async def fill_doc_set_names(session: AsyncSession, job: Job, generation: Generation, data: dict) -> DocSet:
    """PIPE-8: names are filled from the first ready build."""
    doc_set = await doc_set_for_job(session, job.id)
    if doc_set is None:
        doc_set = DocSet(job_id=job.id)
        session.add(doc_set)
        await session.flush()
    name, basename = core.make_names(data)
    title = str(data.get("title") or "").strip()
    if not title:
        subtitle = str(data.get("subtitle") or "")
        title = subtitle.split("·")[0].split("|")[0].strip()
    doc_set.candidate_name = name
    doc_set.company_name = str(data.get("target_company") or "").strip() or None
    doc_set.job_title = title or None
    doc_set.docx_basename = basename
    if doc_set.company_name:
        await promote_doc_set_dir(session, doc_set, job, doc_set.company_name)
    await session.flush()
    return doc_set
