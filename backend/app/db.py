"""Async SQLAlchemy engine/session management.

PostgreSQL is the production target (§3.1). SQLite is supported so the test
suite and `make dev` run without a database server; the few Postgres-only
features (row locks, advisory locks, full-text search) have portable fallbacks
in this module and in ``app/services/search.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# SQLite has no advisory locks; serialise in-process instead (single-node dev/tests).
_keyed_locks: dict[str, asyncio.Lock] = {}


def url_is_postgres(url: str) -> bool:
    """True when a database URL points at PostgreSQL (used by the migration lock)."""
    return url.startswith("postgresql")


def is_postgres(engine: AsyncEngine) -> bool:
    return engine.dialect.name == "postgresql"


def create_engine_for(database_url: str) -> AsyncEngine:
    kwargs: dict = {"future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in database_url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        kwargs["pool_pre_ping"] = True
    engine = create_async_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine_for(get_settings().database_url)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def configure_engine(engine: AsyncEngine) -> None:
    """Point the process at a specific engine (used by tests)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def keyed_lock(key: str) -> asyncio.Lock:
    lock = _keyed_locks.get(key)
    if lock is None:
        lock = _keyed_locks.setdefault(key, asyncio.Lock())
    return lock


def _advisory_bindings(session: AsyncSession) -> dict[str, asyncio.Lock]:
    return session.info.setdefault("advisory_locks", {})  # type: ignore[return-value]


async def acquire_serialization_lock(session: AsyncSession, key: str) -> None:
    """Portable stand-in for ``pg_advisory_xact_lock(hashtext(key))`` (ORD-2).

    The lock is *transaction scoped* on both backends: it is released by
    commit/rollback and cannot dangle after a crash. On PostgreSQL the real
    advisory lock is taken; on SQLite (dev/tests only) an in-process lock is
    held until the surrounding transaction ends. Releasing it any earlier
    would let a concurrent writer read a stale ``MAX(seq_no)`` before our
    commit lands and then collide on the gapless-sequence unique index.
    """
    if is_postgres(session.get_bind()):
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
        return

    bound = _advisory_bindings(session)
    if key in bound:
        return
    if not session.in_transaction():
        await session.begin()
    lock = keyed_lock(f"advisory::{key}")
    await lock.acquire()
    bound[key] = lock
    sync_session = session.sync_session

    @event.listens_for(sync_session, "after_transaction_end")
    def _release_when_transaction_ends(_session, transaction) -> None:  # pragma: no cover - hook
        if transaction.parent is not None:
            return
        held = bound.pop(key, None)
        if held is not None and held.locked():
            held.release()
