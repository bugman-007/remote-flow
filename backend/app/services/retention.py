"""Retention and disk thresholds (STO-4, STO-5)."""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DocSet,
    FileArtifact,
    Generation,
    GenerationAttempt,
    Interview,
    Job,
    Setting,
)
from app.services import settings_store, storage
from app.services.broker import get_broker
from app.services.events import emit
from app.utils import utcnow

logger = logging.getLogger(__name__)


async def pinned_generation_ids(session: AsyncSession) -> set[str]:
    rows = (await session.execute(select(Interview.generation_id).where(Interview.generation_id.is_not(None)))).scalars().all()
    return {row for row in rows if row}


async def retention_plan(session: AsyncSession, *, now=None) -> dict:
    """Compute (without deleting) what retention would expire - SET-15 dry run."""
    settings = await settings_store.all_settings(session)
    moment = now or utcnow()
    pinned = await pinned_generation_ids(session)

    attempt_cutoff = moment - timedelta(days=int(settings["attempt_artifacts_retention_days"]))
    superseded_cutoff = moment - timedelta(days=int(settings["superseded_generation_retention_days"]))
    docset_cutoff = moment - timedelta(days=int(settings["docset_retention_days"]))
    interview_cutoff = moment - timedelta(days=int(settings["interview_retention_days"]))

    attempt_dirs: list[str] = []
    for attempt in (
        await session.execute(
            select(GenerationAttempt).where(
                GenerationAttempt.artifacts_dir.is_not(None),
                GenerationAttempt.started_at < attempt_cutoff,
            )
        )
    ).scalars().all():
        attempt_dirs.append(attempt.artifacts_dir)

    superseded: list[Generation] = []
    for generation in (
        await session.execute(
            select(Generation).where(Generation.status == "ready", Generation.ready_at < superseded_cutoff)
        )
    ).scalars().all():
        if generation.expired_at is not None:
            continue
        if generation.id in pinned:
            continue
        doc_set = (await session.execute(select(DocSet).where(DocSet.job_id == generation.job_id))).scalar_one_or_none()
        if doc_set and (doc_set.current_generation_id == generation.id or doc_set.is_selected or doc_set.keep):
            continue
        superseded.append(generation)

    doc_sets: list[tuple[DocSet, Job, list[Generation]]] = []
    for doc_set in (await session.execute(select(DocSet))).scalars().all():
        job = await session.get(Job, doc_set.job_id)
        if job is None or job.submitted_at >= docset_cutoff:
            continue
        if doc_set.is_selected or doc_set.keep:
            continue
        generations = (
            await session.execute(
                select(Generation).where(Generation.job_id == job.id, Generation.expired_at.is_(None))
            )
        ).scalars().all()
        if not generations:
            continue
        if any(generation.id in pinned for generation in generations):
            keep_list = [g for g in generations if g.id in pinned]
            drop_list = [g for g in generations if g.id not in pinned]
            if drop_list:
                doc_sets.append((doc_set, job, drop_list))
            continue
        doc_sets.append((doc_set, job, list(generations)))

    interview_generations: list[Generation] = []
    for interview in (
        await session.execute(
            select(Interview).where(
                Interview.status.in_(("completed", "cancelled", "no_show")),
                Interview.updated_at < interview_cutoff,
                Interview.generation_id.is_not(None),
            )
        )
    ).scalars().all():
        generation = await session.get(Generation, interview.generation_id)
        if generation is not None and generation.expired_at is None:
            interview_generations.append(generation)

    size = 0
    generations_to_expire = {gen.id: gen for gen in [*superseded, *interview_generations]}
    for _doc_set, _job, gens in doc_sets:
        for generation in gens:
            generations_to_expire[generation.id] = generation
    for generation in generations_to_expire.values():
        if generation.storage_dir:
            size += storage.directory_size(storage.storage_root() / generation.storage_dir)
    for directory in set(attempt_dirs):
        size += storage.directory_size(storage.storage_root() / directory)

    return {
        "attempt_dirs": sorted(set(attempt_dirs)),
        "protected": await _protected_doc_sets(session),
        "superseded_generations": [g.id for g in superseded],
        "doc_set_generations": [(doc_set.id, [g.id for g in gens]) for doc_set, _job, gens in doc_sets],
        "interview_generations": [g.id for g in interview_generations],
        "generation_ids": sorted(generations_to_expire),
        "bytes": size,
        "cutoffs": {
            "attempts": attempt_cutoff.isoformat(),
            "superseded": superseded_cutoff.isoformat(),
            "doc_sets": docset_cutoff.isoformat(),
            "interviews": interview_cutoff.isoformat(),
        },
    }


async def _protected_doc_sets(session: AsyncSession) -> list[dict]:
    """SET-15: Selected / Kept / interview-pinned doc sets are never expired."""
    pinned_ids = select(Interview.doc_set_id).where(Interview.generation_id.is_not(None))
    rows = (
        await session.execute(
            select(DocSet).where(
                or_(DocSet.is_selected.is_(True), DocSet.keep.is_(True), DocSet.id.in_(pinned_ids))
            )
        )
    ).scalars().all()
    items: list[dict] = []
    for doc_set in rows:
        generations = (
            await session.execute(select(Generation).where(Generation.job_id == doc_set.job_id))
        ).scalars().all()
        size = 0
        for generation in generations:
            if generation.storage_dir:
                size += storage.directory_size(storage.storage_root() / generation.storage_dir)
        items.append(
            {
                "doc_set_id": doc_set.id,
                "label": doc_set.company_name or doc_set.job_title or doc_set.id,
                "is_selected": doc_set.is_selected,
                "keep": doc_set.keep,
                "generations": len(generations),
                "bytes": size,
            }
        )
    return items


async def run_retention(session: AsyncSession, *, dry_run: bool = False) -> dict:
    plan = await retention_plan(session)
    if dry_run:
        plan["dry_run"] = True
        return plan
    moment = utcnow()
    root = storage.storage_root()
    for directory in plan["attempt_dirs"]:
        shutil.rmtree(root / directory, ignore_errors=True)
    expired_generations = 0
    for generation_id in plan["generation_ids"]:
        generation = await session.get(Generation, generation_id)
        if generation is None or generation.expired_at is not None:
            continue
        if generation.storage_dir:
            shutil.rmtree(root / generation.storage_dir, ignore_errors=True)
        generation.expired_at = moment
        expired_generations += 1
        for artifact in (
            await session.execute(select(FileArtifact).where(FileArtifact.generation_id == generation.id))
        ).scalars().all():
            artifact.expired_at = moment
    from sqlalchemy import func

    for doc_set_id, generation_ids in plan["doc_set_generations"]:
        doc_set = await session.get(DocSet, doc_set_id)
        if doc_set is None:
            continue
        if doc_set.files_expired_at is None:
            doc_set.files_expired_at = moment
        # STO-4: when *every* generation of the doc set is expiring, drop the whole
        # folder (job description included) instead of only its generation folders -
        # otherwise jd.txt is left behind after `files_expired_at` was recorded.
        remaining = int(
            (
                await session.execute(
                    select(func.count(Generation.id)).where(
                        Generation.job_id == doc_set.job_id,
                        Generation.expired_at.is_(None),
                        Generation.id.not_in(generation_ids or [""]),
                    )
                )
            ).scalar_one()
            or 0
        )
        if remaining == 0 and doc_set.storage_dir:
            shutil.rmtree(root / doc_set.storage_dir, ignore_errors=True)
    plan["dry_run"] = False
    plan["expired_generations"] = expired_generations
    return plan


async def check_disk(session: AsyncSession) -> dict:
    """STO-5: warn, pause intake, then pause rendering as the disk fills."""
    usage = storage.disk_usage_pct()
    settings = await settings_store.all_settings(session)
    warn = float(settings["disk_warn_pct"])
    pause_intake = float(settings["disk_pause_intake_pct"])
    pause_render = float(settings["disk_pause_render_pct"])
    force_resume = bool(settings["intake_force_resume"])
    state = {"usage_pct": usage, "warn": usage >= warn, "rendering_paused": usage >= pause_render}

    paused_row = (await session.execute(select(Setting).where(Setting.key == "intake_paused"))).scalar_one_or_none()
    currently_paused = bool(paused_row.value) if paused_row else False
    if usage >= pause_intake and not currently_paused:
        await settings_store.set_settings(
            session,
            {"intake_paused": True, "intake_pause_reason": f"Storage is {usage:.0f}% full; intake paused automatically."},
            actor_id=None,
        )
        await emit(
            session,
            audience={"user_ids": [], "roles": ["manager", "maker"]},
            event_type="intake.paused",
            payload={"usage_pct": usage},
        )
        state["intake_paused"] = True
    elif usage < warn and currently_paused and not force_resume:
        await settings_store.set_settings(
            session, {"intake_paused": False, "intake_pause_reason": None}, actor_id=None
        )
        await emit(
            session, audience={"user_ids": [], "roles": ["manager", "maker"]},
            event_type="intake.resumed", payload={"usage_pct": usage},
        )
        state["intake_paused"] = False
    else:
        state["intake_paused"] = currently_paused
    return state
