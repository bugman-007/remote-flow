"""Durable dispatch (PIPE-3): enqueue after commit, swallow duplicates harmlessly."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

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
        from app.workers.celery_app import celery_app

        task = "app.workers.tasks.run_llm" if stage == "llm" else "app.workers.tasks.run_render"
        celery_app.send_task(task, args=[generation_id], priority=9 if high_priority else 5)


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
    dispatcher = get_dispatcher()
    for generation_id, stage, high_priority in pending:
        try:
            await dispatcher.enqueue(generation_id, stage, high_priority=high_priority)
        except Exception:  # noqa: BLE001 - the dispatch sweep will retry
            logger.exception("dispatch failed", extra={"generation_id": generation_id, "stage": stage})
    return len(pending)


def dispatch_state_payload(generation: Any) -> dict:
    return {
        "generation_id": generation.id,
        "job_id": generation.job_id,
        "status": generation.status,
        "stage": generation.stage,
    }
