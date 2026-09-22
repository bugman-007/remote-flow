"""Transactional event outbox + SSE fan-out (PIPE-1, RT-1…RT-8)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventOutbox, PipelineEvent, User
from app.services.broker import Broker, event_channel, event_stream, get_broker
from app.utils import utcnow

logger = logging.getLogger(__name__)

OUTBOX_MAX_AGE_HOURS = 24
SLOW_PATH_DELAY_S = 2


def job_audience(maker_id: str, *, include_managers: bool = True) -> dict[str, list[str]]:
    return {"user_ids": [maker_id], "roles": ["manager"] if include_managers else []}


def managers_only() -> dict[str, list[str]]:
    return {"user_ids": [], "roles": ["manager"]}


async def emit(
    session: AsyncSession,
    *,
    audience: dict[str, list[str]],
    event_type: str,
    payload: dict[str, Any],
    job_id: str | None = None,
    generation_id: str | None = None,
    pipeline_event: dict[str, Any] | None = None,
) -> EventOutbox:
    """Write the outbox row (and optional pipeline event) inside the caller's transaction."""
    row = EventOutbox(audience=audience, event_type=event_type, payload=payload)
    session.add(row)
    if pipeline_event is not None:
        session.add(PipelineEvent(job_id=job_id, generation_id=generation_id, **pipeline_event))
    await session.flush()
    return row


async def _resolve_recipients(session: AsyncSession, audience: dict[str, list[str]]) -> list[str]:
    user_ids = list(dict.fromkeys(audience.get("user_ids") or []))
    roles = audience.get("roles") or []
    if roles:
        rows = (
            await session.execute(select(User.id).where(User.role.in_(roles), User.is_active.is_(True)))
        ).scalars().all()
        user_ids.extend(uid for uid in rows if uid not in user_ids)
    return user_ids


async def deliver(session: AsyncSession, row: EventOutbox, *, broker: Broker | None = None) -> int:
    """Publish one outbox row to each recipient's SSE channel and stream."""
    broker = broker or get_broker()
    recipients = await _resolve_recipients(session, row.audience or {})
    for user_id in recipients:
        event_id = await broker.incr(f"rf:evseq:{user_id}", ttl_seconds=7 * 24 * 3600)
        envelope = {
            "id": str(event_id),
            "type": row.event_type,
            "data": row.payload,
            "outbox_id": row.id,
        }
        await broker.append_stream(event_stream(user_id), envelope)
        await broker.publish(event_channel(user_id), envelope)
    row.published_at = utcnow()
    row.publish_attempts = (row.publish_attempts or 0) + 1
    await session.flush()
    return len(recipients)


async def publish_fast_path(session: AsyncSession, rows: list[EventOutbox]) -> None:
    for row in rows:
        try:
            await deliver(session, row)
        except Exception:  # noqa: BLE001 - the relay will pick it up
            logger.exception("fast-path publish failed", extra={"outbox_id": row.id})


async def relay_unpublished(session: AsyncSession, *, limit: int = 200) -> int:
    """Slow path: publish anything committed but not published after 2 s (PIPE-1)."""
    cutoff = utcnow() - timedelta(seconds=SLOW_PATH_DELAY_S)
    rows = (
        await session.execute(
            select(EventOutbox)
            .where(EventOutbox.published_at.is_(None), EventOutbox.created_at <= cutoff)
            .order_by(EventOutbox.created_at)
            .limit(limit)
        )
    ).scalars().all()
    published = 0
    for row in rows:
        try:
            await deliver(session, row)
            published += 1
        except Exception:  # noqa: BLE001
            logger.exception("relay publish failed", extra={"outbox_id": row.id})
    return published


async def purge_old_outbox(session: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(hours=OUTBOX_MAX_AGE_HOURS)
    from sqlalchemy import delete

    result = await session.execute(delete(EventOutbox).where(EventOutbox.created_at < cutoff))
    return int(result.rowcount or 0)


async def replay(session: AsyncSession, user_id: str, *, last_event_id: str | None, limit: int = 500) -> list[dict]:
    broker = get_broker()
    entries = await broker.read_stream(event_stream(user_id), after_id=last_event_id, limit=limit)
    return [payload for _entry_id, payload in entries]


EVENT_TYPES = (
    "job.status",
    "job.released",
    "build.needs_attention",
    "docset.regenerated",
    "docset.selected",
    "limit.updated",
    "intake.paused",
    "intake.resumed",
    "interview.assigned",
    "interview.updated",
    "feedback.added",
    "system.notice",
)
