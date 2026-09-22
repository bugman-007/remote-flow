"""Helpers for driving the pipeline directly in tests."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocSet, Generation, GenerationAttempt, Job, User
from app.services import intake, pipeline, release


async def submit(session: AsyncSession, maker: User, text: str, key: str | None = None) -> Job:
    if key is None:
        key = f"key-{uuid.uuid4()}"
    job, _created = await intake.submit_jd(session, maker=maker, jd_text=text, idempotency_key=key)
    await session.commit()
    return job


async def submit_many(session: AsyncSession, maker: User, count: int, *, prefix: str = "jd") -> list[Job]:
    jobs = []
    for index in range(count):
        jobs.append(
            await submit(
                session,
                maker,
                f"{prefix} number {index}: we are hiring a senior engineer at Company {index} "
                "to join the platform team and ship backend services.",
                f"{prefix}-{index}",
            )
        )
    return jobs


async def initial_build(session: AsyncSession, job: Job) -> Generation:
    generation = await release.initial_generation(session, job)
    assert generation is not None
    return generation


async def llm_attempt_count(session: AsyncSession, generation_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.generation_id == generation_id, GenerationAttempt.stage == "llm"
                )
            )
        ).scalar_one()
    )


async def render_attempt_count(session: AsyncSession, generation_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.generation_id == generation_id, GenerationAttempt.stage == "render"
                )
            )
        ).scalar_one()
    )


async def run_pipeline(*, max_steps: int = 2000) -> int:
    return await pipeline.run_until_idle(max_steps=max_steps)


async def doc_set_for(session: AsyncSession, job_id: str) -> DocSet:
    doc_set = (await session.execute(select(DocSet).where(DocSet.job_id == job_id))).scalar_one()
    return doc_set


async def released_sequence(session: AsyncSession, maker_id: str) -> list[int]:
    rows = (
        await session.execute(
            select(Job.seq_no)
            .where(Job.maker_id == maker_id, Job.delivery_status == "released")
            .order_by(Job.submitted_date, Job.seq_no)
        )
    ).scalars().all()
    return list(rows)


async def maker_usage(session: AsyncSession, maker_id: str) -> int:
    day = await intake.server_today(session)
    return await intake.usage_today(session, maker_id, day)
