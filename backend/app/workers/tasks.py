"""Celery tasks (thin wrappers around the service layer)."""

from __future__ import annotations

import logging

from app.db import session_scope
from app.services import dispatch, metrics, pipeline, release, retention, stats
from app.services import events as events_service
from app.workers.celery_app import celery_app
from app.workers.runtime import run_async

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.run_llm", bind=True, max_retries=0)
def run_llm(self, generation_id: str) -> str | None:
    async def work():
        async with session_scope() as session:
            result = await pipeline.execute_llm_attempt(session, generation_id, worker=f"worker-llm:{self.request.hostname}")
            await session.commit()
            await dispatch.flush_dispatches(session)
            return result

    return run_async(work)


@celery_app.task(name="app.workers.tasks.run_render", bind=True, max_retries=0)
def run_render(self, generation_id: str) -> str | None:
    async def work():
        async with session_scope() as session:
            result = await pipeline.execute_render_attempt(
                session, generation_id, worker=f"worker-render:{self.request.hostname}"
            )
            await session.commit()
            await dispatch.flush_dispatches(session)
            return result

    return run_async(work)


@celery_app.task(name="app.workers.tasks.dispatch_sweep")
def dispatch_sweep() -> int:
    async def work():
        async with session_scope() as session:
            count = await pipeline.dispatch_sweep(session)
            if count:
                await metrics.increment(session, metrics.daily_key("sweep_redispatched"), count)
            await session.commit()
            await dispatch.flush_dispatches(session)
            return count

    return run_async(work)


@celery_app.task(name="app.workers.tasks.lease_watchdog")
def lease_watchdog() -> int:
    async def work():
        async with session_scope() as session:
            count = await pipeline.lease_watchdog(session)
            if count:
                await metrics.increment(session, metrics.daily_key("leases_expired"), count)
            await session.commit()
            await dispatch.flush_dispatches(session)
            return count

    return run_async(work)


@celery_app.task(name="app.workers.tasks.release_reconcile")
def release_reconcile() -> int:
    async def work():
        async with session_scope() as session:
            count = await pipeline.reconcile_releases(session)
            await session.commit()
            await dispatch.flush_dispatches(session)
            return count

    return run_async(work)


@celery_app.task(name="app.workers.tasks.relay_outbox")
def relay_outbox() -> int:
    async def work():
        async with session_scope() as session:
            published = await events_service.relay_unpublished(session)
            purged = await events_service.purge_old_outbox(session)
            await session.commit()
            return published + purged

    return run_async(work)


@celery_app.task(name="app.workers.tasks.run_retention_sweep")
def run_retention_sweep() -> dict:
    async def work():
        async with session_scope() as session:
            plan = await retention.run_retention(session)
            expired = int(plan.get("expired_generations", 0) or 0)
            freed = int(plan.get("bytes", 0) or 0)
            if expired:
                await metrics.increment(session, metrics.daily_key("retention_generations"), expired)
            if freed:
                await metrics.increment(session, metrics.daily_key("retention_bytes"), freed)
            await session.commit()
            return {"expired_generations": expired, "bytes": freed}

    return run_async(work)


@celery_app.task(name="app.workers.tasks.check_disk")
def check_disk() -> dict:
    async def work():
        async with session_scope() as session:
            state = await retention.check_disk(session)
            await session.commit()
            return state

    return run_async(work)


@celery_app.task(name="app.workers.tasks.refresh_stats")
def refresh_stats() -> int:
    async def work():
        async with session_scope() as session:
            count = await stats.refresh_daily_stats(session)
            await session.commit()
            return count

    return run_async(work)
