"""Event outbox, relay and SSE replay (PIPE-1, RT-1…RT-8)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import EventOutbox, PipelineEvent, User
from app.services import events
from app.services.broker import event_channel, event_stream, get_broker
from app.utils import utcnow
from tests.helpers import submit


async def emit_job_event(session, maker, *, payload: dict | None = None, job=None) -> EventOutbox:
    return await events.emit(
        session,
        audience=events.job_audience(maker.id),
        event_type="job.status",
        payload=payload or {"job_id": job.id if job else None, "seq_no": 1, "status": "queued"},
        job_id=job.id if job else None,
        pipeline_event={
            "from_state": None,
            "to_state": "pending",
            "stage": "intake",
            "actor": maker.id,
            "details": {"seq_no": 1},
        },
    )


async def age_row(session, row: EventOutbox, *, seconds: int = 5) -> None:
    await session.execute(
        update(EventOutbox).where(EventOutbox.id == row.id).values(created_at=utcnow() - timedelta(seconds=seconds))
    )
    await session.commit()


@pytest.mark.asyncio
async def test_outbox_row_is_invisible_until_the_transaction_commits(db_session, workspace):
    """PIPE-1: the outbox row is written with the work, published only after COMMIT."""
    maker = workspace["maker"]
    row = await emit_job_event(db_session, maker)
    await db_session.flush()

    broker = get_broker()
    assert await broker.read_stream(event_stream(maker.id)) == []
    assert await broker.read_stream(event_stream(workspace["manager"].id)) == []

    # Only rows older than the 2 s fast-path window are relayed.
    assert await events.relay_unpublished(db_session) == 0
    await age_row(db_session, row)
    assert await events.relay_unpublished(db_session) == 1
    await db_session.commit()

    for user_id in (maker.id, workspace["manager"].id):
        entries = await broker.read_stream(event_stream(user_id))
        assert len(entries) == 1
        assert entries[0][1]["type"] == "job.status"
        assert entries[0][1]["data"]["status"] == "queued"


@pytest.mark.asyncio
async def test_relay_publishes_every_recipient_once(db_session, workspace):
    maker = workspace["maker"]
    row = await emit_job_event(db_session, maker)
    await age_row(db_session, row)

    broker = get_broker()
    assert await events.relay_unpublished(db_session) == 1
    await db_session.commit()
    await db_session.refresh(row)
    assert row.published_at is not None
    assert row.publish_attempts == 1

    # A second pass must not re-publish the same row.
    assert await events.relay_unpublished(db_session) == 0
    for user_id in (maker.id, workspace["manager"].id):
        entries = await broker.read_stream(event_stream(user_id))
        assert len(entries) == 1
        assert entries[0][1]["outbox_id"] == row.id


@pytest.mark.asyncio
async def test_pipeline_event_is_written_inside_the_same_transaction(db_session, workspace):
    job = await submit(db_session, workspace["maker"], "role at Company Outbox for a backend engineer, remote")
    row = await emit_job_event(db_session, workspace["maker"], job=job)
    await db_session.commit()

    stored = (
        await db_session.execute(
            select(PipelineEvent).where(PipelineEvent.job_id == job.id, PipelineEvent.actor == workspace["maker"].id)
        )
    ).scalars().all()
    assert len(stored) == 2  # the intake event plus ours
    assert (stored[0].to_state, stored[0].stage) == ("pending", "intake")
    assert row.published_at is None


@pytest.mark.asyncio
async def test_replay_resumes_from_the_last_event_id(db_session, workspace):
    maker = workspace["maker"]
    broker = get_broker()
    first = await events.emit(
        db_session, audience=events.job_audience(maker.id), event_type="job.status", payload={"seq": 1}
    )
    second = await events.emit(
        db_session, audience=events.job_audience(maker.id), event_type="job.status", payload={"seq": 2}
    )
    await db_session.commit()
    await events.deliver(db_session, first)
    await events.deliver(db_session, second)
    await db_session.commit()

    entries = await broker.read_stream(event_stream(maker.id))
    assert [payload["data"]["seq"] for _id, payload in entries] == [1, 2]
    assert entries[0][1]["id"] != entries[1][1]["id"]

    replayed = await events.replay(db_session, maker.id, last_event_id=entries[0][0])
    assert [item["data"]["seq"] for item in replayed] == [2]
    assert [item["data"]["seq"] for item in await events.replay(db_session, maker.id, last_event_id=None)] == [1, 2]


@pytest.mark.asyncio
async def test_purge_drops_only_old_outbox_rows(db_session, workspace):
    maker = workspace["maker"]
    stale = await emit_job_event(db_session, maker)
    fresh = await emit_job_event(db_session, maker)
    await db_session.commit()
    await age_row(db_session, stale, seconds=25 * 3600)

    assert await events.purge_old_outbox(db_session) == 1
    await db_session.commit()
    remaining = (await db_session.execute(select(EventOutbox.id))).scalars().all()
    assert remaining == [fresh.id]


@pytest.mark.asyncio
async def test_events_endpoint_requires_authentication(client):
    """RT-1: the SSE stream is authenticated; anonymous callers get the error envelope."""
    response = await client.get("/api/v1/events")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_sse_frames_carry_id_type_and_data():
    from app.routers.events import _format

    frame = _format({"id": "42", "type": "job.released", "data": {"job_id": "abc"}})
    assert frame == 'id: 42\nevent: job.released\ndata: {"job_id": "abc"}\n\n'
