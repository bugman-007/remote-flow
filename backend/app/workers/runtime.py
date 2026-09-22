"""Per-task asyncio runtime.

Celery tasks are synchronous; each one owns a fresh engine/broker so no
connection is shared across event loops (important for asyncpg and redis).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.config import get_settings
from app.db import configure_engine, create_engine_for, dispose_engine
from app.logging_setup import configure_logging
from app.services import broker as broker_module

T = TypeVar("T")


async def _bootstrap() -> None:
    configure_logging()
    configure_engine(create_engine_for(get_settings().database_url))


async def _teardown() -> None:
    current = broker_module._broker  # noqa: SLF001 - deliberate: close per-task broker
    if current is not None:
        try:
            await current.close()
        except Exception:  # noqa: BLE001
            pass
    broker_module.set_broker(None)
    await dispose_engine()


def run_async(factory: Callable[[], Awaitable[T]]) -> T:
    async def main() -> T:
        await _bootstrap()
        try:
            return await factory()
        finally:
            await _teardown()

    return asyncio.run(main())
