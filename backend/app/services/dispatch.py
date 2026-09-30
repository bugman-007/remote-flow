"""Durable dispatch (PIPE-3): enqueue after commit, swallow duplicates harmlessly."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DISPATCH_KEY = "rf_dispatches"


class Dispatcher(Protocol):
    async def enqueue(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None: ...


class NoopDispatcher:
    """Records enqueues instead of running them (tests, `make dev --no-workers`)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def enqueue(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
        self.calls.append((generation_id, stage, high_priority))


class InlineDispatcher:
    """Dev/`make dev` dispatcher: runs builds in the API process (no broker).

    The queue is consumed by :func:`app.workers.inline.consume`, which is started
    by ``app.main.lifespan`` when ``INLINE_PIPELINE=true``. This is deliberately
    single-process only - never enable it with multiple uvicorn workers.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def enqueue(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
        await self.queue.put((generation_id, stage, high_priority))


class CeleryDispatcher:
    async def enqueue(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
        self.send(generation_id, stage, high_priority=high_priority)

    def send(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
        from app.config import get_settings
        from app.workers.celery_app import celery_app

        if stage == "llm" and get_settings().llm_runner_enabled:
            # CONC-2: the LLM runner claims straight from the database in release
            # order, so there is no queue message to publish (nobody would consume it).
            return
        task = "app.workers.tasks.run_llm" if stage == "llm" else "app.workers.tasks.run_render"
        celery_app.send_task(task, args=[generation_id], priority=9 if high_priority else 5)


class BackgroundCeleryDispatcher(CeleryDispatcher):
    """``CeleryDispatcher`` for the LLM runner, whose event loop must never block.

    ``send_task`` is blocking network I/O, and in the runner one event loop carries
    every provider call in flight, so publishes run on one background thread
    (a single thread keeps them serialised on Celery's shared producer pool).
    """

    def __init__(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="celery-dispatch")

    async def enqueue(self, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
        import functools

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor, functools.partial(self.send, generation_id, stage, high_priority=high_priority)
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True)


_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = CeleryDispatcher()
    return _dispatcher


def set_dispatcher(dispatcher: Dispatcher | None) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def enqueue_after_commit(session: AsyncSession, generation_id: str, stage: str, *, high_priority: bool = False) -> None:
    session.info.setdefault(DISPATCH_KEY, []).append((generation_id, stage, high_priority))


async def flush_dispatches(session: AsyncSession) -> int:
    """Call after ``commit()``; safe to call when nothing is pending."""
    pending: list[tuple[str, str, bool]] = session.info.pop(DISPATCH_KEY, [])
    if not pending:
        return 0
    dispatcher = get_dispatcher()
    accepted: list[str] = []
    for generation_id, stage, high_priority in pending:
        try:
            await dispatcher.enqueue(generation_id, stage, high_priority=high_priority)
        except Exception:  # noqa: BLE001 - the dispatch sweep will retry
            logger.exception("dispatch failed", extra={"generation_id": generation_id, "stage": stage})
        else:
            accepted.append(generation_id)
    if accepted:
        from app.models import Generation
        from app.utils import utcnow

        # PIPE-3: record that the broker took this work. A build left on ``pending``
        # while its task is already running is re-published by every ``dispatch_sweep``
        # pass, and each duplicate burns a worker slot waiting on the row lock.
        await session.execute(
            update(Generation)
            .where(Generation.id.in_(accepted))
            .values(dispatch_state="sent", dispatched_at=utcnow())
        )
        await session.commit()
    return len(pending)


def dispatch_state_payload(generation: Any) -> dict:
    return {
        "generation_id": generation.id,
        "job_id": generation.job_id,
        "status": generation.status,
        "stage": generation.stage,
    }
