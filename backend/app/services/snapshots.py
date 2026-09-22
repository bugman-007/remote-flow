"""Build creation and frozen snapshots (PIPE-8, PIPE-9)."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Generation, Job, LLMProvider, Profile, Theme
from app.services import events
from app.services.dispatch import enqueue_after_commit
from app.services.llm import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from app.services.theme import DEFAULT_THEME_NAME, validate_theme_params
from vendor.resume_builder import core

logger = logging.getLogger(__name__)


class SnapshotError(RuntimeError):
    pass


async def resolve_profile_snapshot(session: AsyncSession, profile: Profile) -> dict:
    provider = None
    if profile.provider_id:
        provider = (
            await session.execute(select(LLMProvider).where(LLMProvider.id == profile.provider_id))
        ).scalar_one_or_none()
    if provider is None:
        provider = (
            await session.execute(
                select(LLMProvider)
                .where(LLMProvider.is_default.is_(True), LLMProvider.is_enabled.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
    if provider is None:
        raise SnapshotError("no enabled LLM provider is configured")

    theme = None
    if profile.theme_id:
        theme = (await session.execute(select(Theme).where(Theme.id == profile.theme_id))).scalar_one_or_none()
    if theme is None:
        theme = (
            await session.execute(select(Theme).where(Theme.name == DEFAULT_THEME_NAME))
        ).scalar_one_or_none()
    theme_params = validate_theme_params(theme.params) if theme else dict(core.DEFAULTS)

    return {
        "prompt_version_id": profile.active_prompt_version_id,
        "provider_id": provider.id,
        "model": profile.model or provider.default_model or "",
        "llm_params": {
            "temperature": profile.temperature if profile.temperature is not None else DEFAULT_TEMPERATURE,
            "max_tokens": profile.max_tokens or DEFAULT_MAX_TOKENS,
        },
        "theme_id": theme.id if theme else None,
        "theme_snapshot": theme_params,
    }


async def next_generation_no(session: AsyncSession, job_id: str) -> int:
    current = (
        await session.execute(select(func.max(Generation.generation_no)).where(Generation.job_id == job_id))
    ).scalar_one()
    return int(current or 0) + 1


async def create_generation(
    session: AsyncSession,
    *,
    job: Job,
    profile: Profile,
    kind: str,
    created_by: str | None = None,
) -> Generation:
    snapshot = await resolve_profile_snapshot(session, profile)
    generation = Generation(
        job_id=job.id,
        generation_no=await next_generation_no(session, job.id),
        kind=kind,
        status="queued",
        stage="llm",
        dispatch_state="pending",
        created_by=created_by,
        next_retry_at=None,
        **snapshot,
    )
    session.add(generation)
    await session.flush()
    await events.emit(
        session,
        audience=events.job_audience(job.maker_id),
        event_type="job.status",
        payload={"job_id": job.id, "seq_no": job.seq_no, "generation_id": generation.id, "status": "queued"},
        job_id=job.id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": None,
            "to_state": "queued",
            "stage": "llm",
            "actor": created_by or "pipeline",
            "details": {"kind": kind},
        },
    )
    enqueue_after_commit(session, generation.id, "llm", high_priority=kind != "initial")
    return generation
