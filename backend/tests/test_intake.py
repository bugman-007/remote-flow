"""JD intake: quota, idempotency, gapless sequence numbers (PIPE-11, JD-1…JD-7)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Job, User
from app.services import intake, settings_store
from tests.helpers import maker_usage, submit


@pytest.mark.asyncio
async def test_sequence_numbers_are_gapless_and_ordered(db_session, workspace):
    maker = workspace["makers"][0]
    jobs = [
        await submit(db_session, maker, f"job {i} at Company {i} hiring engineers for the platform team", f"k{i}")
        for i in range(5)
    ]
    assert [job.seq_no for job in jobs] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_repeated_idempotency_key_returns_the_same_job(db_session, workspace):
    maker = workspace["makers"][0]
    first, created = await intake.submit_jd(db_session, maker=maker, jd_text="a" * 60, idempotency_key="same")
    await db_session.commit()
    second, created_again = await intake.submit_jd(db_session, maker=maker, jd_text="a" * 60, idempotency_key="same")
    await db_session.commit()
    assert created is True
    assert created_again is False
    assert first.id == second.id
    count = (await db_session.execute(select(func.count(Job.id)))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_submissions_from_one_maker_stay_gapless(db_session, workspace):
    from app.db import get_session_factory

    maker = workspace["makers"][0]
    factory = get_session_factory()

    async def worker(index: int) -> None:
        async with factory() as session:
            user = await session.get(User, maker.id)
            await intake.submit_jd(
                session, maker=user,
                jd_text=f"concurrent {index} at Company {index} hiring engineers for the platform team",
                idempotency_key=f"c{index}",
            )
            await session.commit()

    await asyncio.gather(*(worker(index) for index in range(6)))
    seqs = (
        await db_session.execute(select(Job.seq_no).order_by(Job.seq_no))
    ).scalars().all()
    assert list(seqs) == list(range(1, 7))


@pytest.mark.asyncio
async def test_daily_limit_is_enforced(db_session, workspace):
    maker = workspace["makers"][0]
    maker.daily_limit = 2
    await db_session.commit()
    await submit(db_session, maker, "one at Company A hiring engineers for the platform team today")
    await submit(db_session, maker, "two at Company B hiring engineers for the platform team today")
    with pytest.raises(intake.IntakeError) as excinfo:
        await submit(db_session, maker, "three at Company C hiring engineers for the platform team today")
    assert excinfo.value.code == "limit_reached"
    assert await maker_usage(db_session, maker.id) == 2


@pytest.mark.asyncio
async def test_maker_without_profile_cannot_submit(db_session, workspace):
    maker = workspace["maker_unassigned"]
    with pytest.raises(intake.IntakeError) as excinfo:
        await submit(db_session, maker, "no profile assigned but trying to submit a job anyway right now")
    assert excinfo.value.code == "no_profile_assigned"


@pytest.mark.asyncio
async def test_intake_paused_rejects_submission(db_session, workspace):
    maker = workspace["makers"][0]
    await settings_store.set_settings(
        db_session, {"intake_paused": True, "intake_pause_reason": "Storage is 91% full"}
    )
    await db_session.commit()
    with pytest.raises(intake.IntakeError) as excinfo:
        await submit(db_session, maker, "paused intake should not accept this job text at all today")
    assert excinfo.value.code == "intake_paused"


@pytest.mark.asyncio
async def test_short_jd_is_rejected(db_session, workspace):
    maker = workspace["makers"][0]
    with pytest.raises(intake.IntakeError) as excinfo:
        await submit(db_session, maker, "too short")
    assert excinfo.value.code == "invalid_jd"


@pytest.mark.asyncio
async def test_duplicate_text_is_flagged(db_session, workspace):
    maker = workspace["makers"][0]
    first = await submit(db_session, maker, "identical text about a role at Company X hiring engineers")
    second = await submit(db_session, maker, "identical text about a role at Company X hiring engineers")
    assert second.duplicate_of == first.id


@pytest.mark.asyncio
async def test_intake_creates_snapshot_and_dispatch(db_session, workspace):
    from app.services import dispatch

    recorder = dispatch.NoopDispatcher()
    from app.services.dispatch import set_dispatcher

    set_dispatcher(recorder)
    try:
        maker = workspace["makers"][0]
        job = await submit(db_session, maker, "A role at Company Z for a senior backend engineer, remote")
        from tests.helpers import initial_build

        generation = await initial_build(db_session, job)
        assert generation.prompt_version_id == workspace["profile"].active_prompt_version_id
        assert generation.provider_id == workspace["provider"].id
        assert generation.theme_snapshot == workspace["theme"].params
        await dispatch.flush_dispatches(db_session)
        assert recorder.calls and recorder.calls[0][0] == generation.id
    finally:
        set_dispatcher(None)
