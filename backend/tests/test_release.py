"""Ordered release: cursor, skip, late release and crash reconciliation (ORD-1…ORD-9)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Generation, Job
from app.services import pipeline, release
from app.services.llm import LLMError, LLMResult, set_client_override
from app.utils import utcnow
from tests.helpers import (
    doc_set_for,
    initial_build,
    released_sequence,
    run_pipeline,
    submit,
    submit_many,
)

VALID = (
    '{"name":"Ada","title":"Engineer","target_company":"Acme","summary":"s",'
    '"experience":[{"title":"Engineer","company":"Acme","bullets":["b"]}]}'
)


class FailingClient:
    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        raise LLMError("provider_server_error", "injected", provider_error=True)


class FixedClient:
    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        return LLMResult(json={}, raw=VALID, usage={}, latency_ms=1)


@pytest.mark.asyncio
async def test_blocker_holds_later_ready_jobs_and_skip_releases_them(db_session, workspace, fast_settings):
    """ORD-1/ORD-5/ORD-6: a stuck cursor blocks the queue; skipping it drains everything behind."""
    fast_settings.max_llm_attempts = 1
    maker = workspace["maker"]
    set_client_override(FailingClient())
    try:
        first = await submit(db_session, maker, "role one at Company One hiring backend engineers today")
        await run_pipeline()
    finally:
        set_client_override(None)
    await db_session.refresh(first)
    assert first.delivery_status == "pending"
    assert (await initial_build(db_session, first)).status == "needs_attention"

    set_client_override(FixedClient())
    try:
        await submit_many(db_session, maker, 2, prefix="behind")
        await run_pipeline()
    finally:
        set_client_override(None)

    # Everything behind the blocker is built, but nothing may be released.
    assert await released_sequence(db_session, maker.id) == []
    blocker = await release.maker_blocker(db_session, maker.id)
    assert blocker == {
        "job_id": first.id,
        "seq_no": 1,
        "status": "needs_attention",
        "stage": "llm",
        "attempt": 1,
        "needs_attention": True,
    }
    jobs = (await db_session.execute(select(Job).order_by(Job.seq_no))).scalars().all()
    payload = await release.status_payload(db_session, jobs[1])
    assert payload["status"] == "waiting"
    assert payload["blocker"]["seq_no"] == 1
    assert payload["blocker"]["needs_attention"] is True

    await pipeline.skip_job(db_session, job=first, actor_id=workspace["manager"].id)
    await db_session.commit()
    assert await released_sequence(db_session, maker.id) == [2, 3]
    assert await release.maker_blocker(db_session, maker.id) is None


@pytest.mark.asyncio
async def test_skip_inside_the_queue_releases_strictly_in_order(db_session, workspace, fast_settings):
    """ORD-5: jobs released after a skip keep their original submission order."""
    fast_settings.max_llm_attempts = 1
    maker = workspace["maker"]
    set_client_override(FixedClient())
    try:
        jobs = await submit_many(db_session, maker, 3, prefix="ordered")
        await run_pipeline()
    finally:
        set_client_override(None)
    assert await released_sequence(db_session, maker.id) == [1, 2, 3]

    # Replay the crash: job 2 is the stuck cursor and jobs 1/3 are ready.
    from sqlalchemy import update

    await db_session.execute(
        update(Job).where(Job.maker_id == maker.id).values(delivery_status="pending", released_at=None)
    )
    await db_session.commit()
    await pipeline.skip_job(db_session, job=jobs[1], actor_id=workspace["manager"].id)
    await db_session.commit()
    assert await released_sequence(db_session, maker.id) == [1, 3]


@pytest.mark.asyncio
async def test_reconciliation_releases_a_crashed_pass(db_session, workspace):
    """ORD-8: ready cursor builds stranded by a crashed release pass are reconciled."""
    maker = workspace["maker"]
    jobs = await submit_many(db_session, maker, 3, prefix="crash")
    await run_pipeline()
    assert await released_sequence(db_session, maker.id) == [1, 2, 3]

    # Simulate a crash that lost the release transaction, including the cursor row.
    from sqlalchemy import update

    await db_session.execute(
        update(Job).where(Job.maker_id == maker.id).values(delivery_status="pending", released_at=None)
    )
    await db_session.execute(
        update(Generation).where(Generation.job_id.in_([job.id for job in jobs])).values(
            ready_at=utcnow() - timedelta(seconds=60)
        )
    )
    await db_session.commit()
    assert await release.makers_with_ready_cursor(db_session) == [maker.id]

    released = await pipeline.reconcile_releases(db_session)
    await db_session.commit()
    assert released == 3
    assert await released_sequence(db_session, maker.id) == [1, 2, 3]
    again = (await db_session.execute(select(Job).where(Job.maker_id == maker.id))).scalars().all()
    assert {job.delivery_status for job in again} == {"released"}


@pytest.mark.asyncio
async def test_regenerate_leaves_delivery_status_and_cursor_untouched(db_session, workspace):
    """PIPE-8: a new generation never rewrites delivery status, timestamps or the cursor."""
    maker = workspace["maker"]
    jobs = await submit_many(db_session, maker, 3, prefix="regen")
    await run_pipeline()
    job = jobs[1]
    await db_session.refresh(job)
    released_at = job.released_at
    assert released_at is not None

    doc_set = await doc_set_for(db_session, job.id)
    first_generation_id = doc_set.current_generation_id
    generation = await pipeline.regenerate(
        session=db_session, doc_set=doc_set, job=job, actor_id=workspace["manager"].id
    )
    await db_session.commit()
    from app.services import dispatch

    await dispatch.flush_dispatches(db_session)
    await run_pipeline()
    await db_session.refresh(job)
    await db_session.refresh(doc_set)
    assert job.delivery_status == "released"
    assert job.released_at == released_at
    assert job.released_late is False
    assert doc_set.current_generation_id == generation.id != first_generation_id
    assert generation.kind == "regenerate"
    assert generation.generation_no == 2

    # The cursor is unaffected: a new submission still releases straight away.
    await submit(db_session, maker, "a brand new role at Company Nine for a backend engineer now")
    await run_pipeline()
    assert await released_sequence(db_session, maker.id) == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_retry_after_skip_is_delivered_late(db_session, workspace, fast_settings):
    """ORD-9: a skipped blocker that is retried later is marked ``released_late``."""
    fast_settings.max_llm_attempts = 1
    maker = workspace["maker"]
    set_client_override(FailingClient())
    try:
        first = await submit(db_session, maker, "role one at Company Late for a backend engineer here")
        await run_pipeline()
    finally:
        set_client_override(None)
    await pipeline.skip_job(db_session, job=first, actor_id=workspace["manager"].id)
    await db_session.commit()

    set_client_override(FixedClient())
    try:
        await submit(db_session, maker, "role two at Company Late for a platform engineer here")
        await run_pipeline()
        assert await released_sequence(db_session, maker.id) == [2]

        generation = await pipeline.retry_now(
            db_session, job=first, mode="new_llm_call", actor_id=workspace["manager"].id
        )
        await db_session.commit()
        from app.services import dispatch

        await dispatch.flush_dispatches(db_session)
        await run_pipeline()
    finally:
        set_client_override(None)
    await db_session.refresh(first)
    assert generation.kind == "retry_after_skip"
    assert first.delivery_status == "released"
    assert first.released_late is True
    assert await released_sequence(db_session, maker.id) == [1, 2]
