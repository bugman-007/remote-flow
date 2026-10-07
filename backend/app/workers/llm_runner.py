"""Async LLM runner (CONC-2 with ``LLM_EXECUTOR=runner``).

A provider call spends ~99 % of its time waiting on the network, so giving each
call its own prefork child (~270 MB, two thirds of it the LiteLLM import) made
memory the limit on parallel calls. The runner is one asyncio process: LiteLLM,
the database engine and the Redis client are loaded once, and each call is a
task costing a few MB.

Nothing about a build's life changes. The runner only decides *which* builds to
start and hands each one to :func:`pipeline.execute_llm_attempt` - the same
claim, lease, retry, fallback and cancel code the Celery task ran - while the
render stage, ordered release and every maintenance job stay on Celery.

The database is the queue. Builds start in release order (oldest day, then
sequence number), so every Maker's next delivery goes first and a retry of an
earlier job comes before later submissions. How many run at once is the budget
the pool controller publishes (Settings -> Processors), clamped to
``LLM_RUNNER_MAX_CONCURRENCY``, and never more than a provider's own
concurrency. A runner that dies leaves leases the watchdog recovers exactly as
before; a stopping runner lets its calls finish first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.models import Generation, Job, LLMProvider
from app.services import dispatch, metrics, pipeline, pool, settings_store
from app.services.broker import get_broker

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15.0
#: A build that could not start (provider slot or rpm budget taken) waits this
#: long before it is tried again, so a saturated provider cannot spin the loop.
RETRY_IDLE_S = 5.0
#: After a crash inside an attempt; its lease is recovered by the watchdog.
CRASH_IDLE_S = 30.0
#: Back-off when the database or Redis is unreachable.
ERROR_IDLE_S = 5.0


async def claimable_builds(
    session: AsyncSession, *, limit: int, exclude: set[str] | None = None, saturated: set[str] | None = None
) -> list[tuple[str, str | None]]:
    """LLM builds that can be claimed right now, in release order."""
    moment = pipeline.now()
    query = (
        select(Generation.id, Generation.provider_id)
        .join(Job, Job.id == Generation.job_id)
        .where(
            Generation.stage == "llm",
            Generation.status.in_(pipeline.actionable_statuses("llm")),
            or_(Generation.next_retry_at.is_(None), Generation.next_retry_at <= moment),
            Generation.lease_token.is_(None),
        )
        .order_by(Job.submitted_date, Job.seq_no, Generation.generation_no, Generation.created_at)
        .limit(max(1, limit))
    )
    if exclude:
        query = query.where(Generation.id.not_in(list(exclude)))
    if saturated:
        query = query.where(or_(Generation.provider_id.is_(None), Generation.provider_id.not_in(list(saturated))))
    return [(row[0], row[1]) for row in (await session.execute(query)).all()]


class LLMRunner:
    def __init__(
        self,
        *,
        worker: str | None = None,
        hard_cap: int | None = None,
        poll_s: float | None = None,
        slot_wait_s: float | None = None,
        drain_s: float | None = None,
        retry_idle_s: float = RETRY_IDLE_S,
    ) -> None:
        settings = get_settings()
        self.worker = worker or f"llm-runner@{socket.gethostname()}"
        self.hard_cap = max(1, int(hard_cap or settings.llm_runner_max_concurrency))
        self.poll_s = settings.llm_runner_poll_s if poll_s is None else poll_s
        self.slot_wait_s = settings.llm_runner_slot_wait_s if slot_wait_s is None else slot_wait_s
        self.drain_s = settings.llm_runner_drain_s if drain_s is None else drain_s
        self.retry_idle_s = retry_idle_s
        #: generation id -> the task running its attempt / the provider it uses.
        self.running: dict[str, asyncio.Task] = {}
        self.provider_of: dict[str, str | None] = {}
        #: generation id -> monotonic time before which it is not started again.
        self.idle_until: dict[str, float] = {}
        self.limit = 0
        self.peak = 0
        self.started = 0
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ control

    def stop(self) -> None:
        """Stop starting new calls; :meth:`run` then drains the running ones."""
        self._stopping.set()
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    async def current_limit(self, session: AsyncSession) -> int:
        """The controller's budget, or the saved settings while it has not ticked yet."""
        budget = await pool.get_budget(get_broker(), fallback=0)
        if budget <= 0:
            budget = pool.settings_budget(await settings_store.all_settings(session), hard_cap=self.hard_cap)
        return max(1, min(budget, self.hard_cap))

    def _in_use(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for provider_id in self.provider_of.values():
            if provider_id:
                counts[provider_id] = counts.get(provider_id, 0) + 1
        return counts

    def _resting(self) -> set[str]:
        moment = time.monotonic()
        for generation_id, until in list(self.idle_until.items()):
            if until <= moment:
                self.idle_until.pop(generation_id, None)
        return set(self.idle_until)

    async def fill(self) -> int:
        """Start as many claimable builds as the budget allows; returns how many."""
        if self.stopping:
            return 0
        async with session_scope() as session:
            self.limit = await self.current_limit(session)
            free = self.limit - len(self.running)
            if free <= 0:
                return 0
            providers = (await session.execute(select(LLMProvider))).scalars().all()
            limits = {provider.id: pipeline.provider_slot_limit(provider) for provider in providers}
            in_use = self._in_use()
            saturated = {provider_id for provider_id, cap in limits.items() if in_use.get(provider_id, 0) >= cap}
            candidates = await claimable_builds(
                session,
                limit=min(free * 2, 2000),
                exclude=set(self.running) | self._resting(),
                saturated=saturated,
            )
        started = 0
        for generation_id, provider_id in candidates:
            if started >= free:
                break
            if provider_id and provider_id in limits and in_use.get(provider_id, 0) >= limits[provider_id]:
                continue
            self._start(generation_id, provider_id)
            if provider_id:
                in_use[provider_id] = in_use.get(provider_id, 0) + 1
            started += 1
        return started

    def _start(self, generation_id: str, provider_id: str | None) -> None:
        task = asyncio.create_task(self._execute(generation_id), name=f"llm:{generation_id}")
        self.running[generation_id] = task
        self.provider_of[generation_id] = provider_id
        self.started += 1
        self.peak = max(self.peak, len(self.running))
        task.add_done_callback(lambda _task, gid=generation_id: self._finished(gid))

    def _finished(self, generation_id: str) -> None:
        self.running.pop(generation_id, None)
        self.provider_of.pop(generation_id, None)
        self._wake.set()

    async def _execute(self, generation_id: str) -> str | None:
        try:
            async with session_scope() as session:
                result = await pipeline.execute_llm_attempt(
                    session,
                    generation_id,
                    worker=self.worker,
                    admission=False,
                    slot_wait_s=self.slot_wait_s,
                )
                await session.commit()
                await dispatch.flush_dispatches(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad build must never stop the others
            logger.exception("llm attempt crashed", extra={"generation_id": generation_id, "stage": "llm"})
            self.idle_until[generation_id] = time.monotonic() + CRASH_IDLE_S
            return None
        if result is None:
            # Nothing was claimed (provider slot or rpm budget taken, or someone
            # else owns it): rest before trying this build again.
            self.idle_until[generation_id] = time.monotonic() + self.retry_idle_s
        return result

    # --------------------------------------------------------------------- loop

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    async def run(self) -> None:
        """Start work until :meth:`stop`, then let the calls in flight finish."""
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="llm-runner-heartbeat")
        try:
            while not self.stopping:
                self._wake.clear()
                try:
                    await self.fill()
                except Exception:  # noqa: BLE001 - database/Redis blip: back off, keep going
                    logger.exception("llm runner pass failed")
                    await self._pause(ERROR_IDLE_S)
                    continue
                await self._pause(self.poll_s)
        finally:
            await self.drain()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def run_until_idle(self, *, timeout_s: float = 60.0) -> None:
        """Tests/CLI: start work until nothing is claimable and nothing is running."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._wake.clear()
            started = await self.fill()
            if not started and not self.running and not self._resting():
                return
            await self._pause(self.poll_s)
        raise TimeoutError("llm runner did not go idle in time")

    async def drain(self) -> None:
        if not self.running:
            return
        tasks = list(self.running.values())
        logger.info("llm runner draining", extra={"stage": "llm"})
        _done, pending = await asyncio.wait(tasks, timeout=self.drain_s)
        for task in pending:
            # The lease stays behind; the watchdog recovers the build as for a crash.
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        started = True
        while True:
            try:
                async with session_scope() as session:
                    await metrics.record_heartbeat(
                        session,
                        name=f"worker:{self.worker}",
                        queues=["llm"],
                        concurrency=self.limit or None,
                        pid=os.getpid(),
                        started=started,
                    )
                started = False
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - heartbeats must never stop the runner
                logger.warning("llm runner heartbeat failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_S)


async def main() -> int:
    """``manage.py llm-runner``: the process behind the ``worker-llm`` service."""
    from app.db import configure_engine, create_engine_for, dispose_engine
    from app.logging_setup import configure_logging

    configure_logging()
    settings = get_settings()
    configure_engine(
        create_engine_for(
            settings.database_url,
            pool_size=settings.llm_runner_db_pool_size,
            max_overflow=settings.llm_runner_db_max_overflow,
        )
    )
    # Import LiteLLM once, up front: it costs ~2 s of CPU and would otherwise stall
    # every call in flight the first time a provider is used.
    try:
        import litellm  # noqa: F401
    except ImportError:  # pragma: no cover - mock-only installs
        logger.warning("litellm is not installed; only the mock provider will work")

    dispatcher = dispatch.BackgroundCeleryDispatcher()
    dispatch.set_dispatcher(dispatcher)
    broker = get_broker()
    # This process owns every running-call marker: markers left by a killed
    # runner (or the old prefork pool) only inflate the Processors panel.
    await broker.delete(pool.LLM_ACTIVE_KEY)

    runner = LLMRunner()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, runner.stop)
    logger.info("llm runner %s started: up to %d calls in flight", runner.worker, runner.hard_cap)
    try:
        await runner.run()
    finally:
        dispatch.set_dispatcher(None)
        dispatcher.close()
        with contextlib.suppress(Exception):
            await broker.close()
        await dispose_engine()
    logger.info("llm runner stopped after %d attempts (peak %d at once)", runner.started, runner.peak)
    return 0
