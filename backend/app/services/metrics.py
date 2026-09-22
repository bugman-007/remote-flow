"""Cumulative operational counters and worker heartbeats (SET-14, NFR-5)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SystemMetric, WorkerHeartbeat
from app.utils import utcnow


def daily_key(name: str, moment=None) -> str:
    stamp = (moment or utcnow()).date().isoformat()
    return f"{name}:{stamp}"


async def increment(session: AsyncSession, key: str, amount: int = 1) -> int:
    row = (await session.execute(select(SystemMetric).where(SystemMetric.key == key))).scalar_one_or_none()
    if row is None:
        row = SystemMetric(key=key, value_int=amount)
        session.add(row)
    else:
        row.value_int = (row.value_int or 0) + amount
    await session.flush()
    return row.value_int


async def get(session: AsyncSession, key: str) -> int:
    row = (await session.execute(select(SystemMetric).where(SystemMetric.key == key))).scalar_one_or_none()
    return int(row.value_int) if row else 0


async def sum_prefix(session: AsyncSession, names: list[str], days: int = 2) -> int:
    """Sum the per-day counters recorded for the last ``days`` days."""
    from datetime import timedelta

    today = utcnow().date()
    keys = [f"{name}:{(today - timedelta(days=offset)).isoformat()}" for name in names for offset in range(days)]
    if not keys:
        return 0
    rows = (await session.execute(select(SystemMetric.value_int).where(SystemMetric.key.in_(keys)))).scalars().all()
    return int(sum(rows))


async def record_heartbeat(
    session: AsyncSession,
    *,
    name: str,
    queues: list[str] | None = None,
    concurrency: int | None = None,
    pid: int | None = None,
    started: bool = False,
) -> WorkerHeartbeat:
    row = (await session.execute(select(WorkerHeartbeat).where(WorkerHeartbeat.name == name))).scalar_one_or_none()
    if row is None:
        row = WorkerHeartbeat(name=name, queues=queues, concurrency=concurrency, pid=pid, started_at=utcnow())
        session.add(row)
    if row.started_at is None or started:
        row.started_at = utcnow()
    row.queues = queues or row.queues
    row.concurrency = concurrency or row.concurrency
    row.pid = pid or row.pid
    row.last_heartbeat_at = utcnow()
    await session.flush()
    return row
