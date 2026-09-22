"""In-process pipeline driver for `make dev` (no Redis/Celery required).

Started from ``app.main.lifespan`` when ``INLINE_PIPELINE=true``. It consumes the
:class:`~app.services.dispatch.InlineDispatcher` queue, and while idle it runs the
same maintenance the beat schedule runs in production, so a restarted dev server
picks up work it may have lost (PIPE-3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.db import session_scope
from app.services import events as events_service
from app.services import metrics, pipeline, retention
from app.services.dispatch import InlineDispatcher, flush_dispatches

logger = logging.getLogger(__name__)

IDLE_SWEEP_SECONDS = 30.0


async def consume(dispatcher: InlineDispatcher, *, idle_sweep_s: float = IDLE_SWEEP_SECONDS) -> None:
    while True:
        try:
            generation_id, stage, _high_priority = await asyncio.wait_for(
                dispatcher.queue.get(), timeout=idle_sweep_s
            )
        except asyncio.TimeoutError:
            await _idle_maintenance()
            continue
        try:
            async with session_scope() as session:
                await pipeline.process_one(session, generation_id, stage, worker="inline")
                await session.commit()
                await flush_dispatches(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad build must not kill the driver
            logger.exception("inline build failed", extra={"generation_id": generation_id, "stage": stage})
        finally:
            dispatcher.queue.task_done()


async def _idle_maintenance() -> None:
    """Mirror the production beat schedule while the dev server is quiet."""
    try:
        async with session_scope() as session:
            redispatched = await pipeline.dispatch_sweep(session)
            expired = await pipeline.lease_watchdog(session)
            await pipeline.reconcile_releases(session)
            await events_service.relay_unpublished(session)
            await events_service.purge_old_outbox(session)
            await retention.check_disk(session)
            if redispatched:
                await metrics.increment(session, metrics.daily_key("sweep_redispatched"), redispatched)
            if expired:
                await metrics.increment(session, metrics.daily_key("leases_expired"), expired)
            await session.commit()
            await flush_dispatches(session)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("inline maintenance failed")


class InlineDriver:
    """Owns the dispatcher + task so lifespan start/shutdown is symmetrical."""

    def __init__(self) -> None:
        self.dispatcher = InlineDispatcher()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        from app.services.dispatch import set_dispatcher

        set_dispatcher(self.dispatcher)
        self.task = asyncio.create_task(consume(self.dispatcher), name="inline-pipeline")

    async def stop(self) -> None:
        from app.services.dispatch import set_dispatcher

        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        set_dispatcher(None)
