"""The async LLM runner (CONC-2 with ``LLM_EXECUTOR=runner``).

The runner only changes *how* LLM attempts are run - many in one process instead
of one per prefork child. Claims, leases, retries, cancellation and ordered
release are the pipeline's, so these tests drive whole builds through it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import Generation, LLMProvider
from app.services import dispatch, pipeline, pool, settings_store
from app.services.broker import MemoryBroker, get_broker
from app.services.llm import LLMError, LLMResult, mock_resume, set_client_override
from app.utils import utcnow
from app.workers.llm_runner import LLMRunner, claimable_builds
from tests.helpers import initial_build, llm_attempt_count, released_sequence, run_pipeline, submit_many


class SlowClient:
    """A provider that takes ``delay`` seconds and records how many calls overlap."""

    def __init__(self, delay: float = 0.2, *, probe=None, fail_first: int = 0) -> None:
        self.delay = delay
        self.probe = probe
        self.fail_first = fail_first
        self.current = 0
        self.peak = 0
        self.calls = 0
        self.probes: list = []

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls += 1
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(self.delay / 2)
            if self.probe is not None:
                self.probes.append(self.probe())
            await asyncio.sleep(self.delay / 2)
            if self.calls <= self.fail_first:
                raise LLMError("provider_server_error", "injected 503", provider_error=True)
            raw = json.dumps(mock_resume(user))
            return LLMResult(json={}, raw=raw, usage={"tokens_in": 1, "tokens_out": 1, "tokens_cached": 0},
                             latency_ms=int(self.delay * 1000), finish_reason="stop")
        finally:
            self.current -= 1


@pytest.fixture
def runner_mode(fast_settings):
    fast_settings.llm_executor = "runner"
    fast_settings.llm_timeout_s = 30
    yield fast_settings
    set_client_override(None)


async def _configure(session, *, static: int | None = None, provider_concurrency: int = 100) -> None:
    if static is not None:
        await settings_store.set_settings(session, {"llm_pool_mode": "static", "llm_pool_static_size": static})
    await session.execute(update(LLMProvider).values(max_concurrency=provider_concurrency, rpm=10_000))
    await session.commit()


async def _statuses(session) -> list[str]:
    session.expire_all()
    rows = (await session.execute(select(Generation.status))).scalars().all()
    return sorted(rows)


# ------------------------------------------------------------------- policy


def _plan(**overrides):
    values = dict(mode="dynamic", static_size=6, min_size=2, max_size=150, hard_cap=150, ceiling=None,
                  mem_used_pct=50.0, active=0, backlog=0)
    values.update(overrides)
    return pool.decide_runner_pool(**values)


def test_static_mode_is_the_managers_number_clamped_to_the_server():
    assert _plan(mode="static", static_size=100).budget == 100
    capped = _plan(mode="static", static_size=2500, hard_cap=150)
    assert capped.budget == 150 and "capped" in capped.reason


def test_dynamic_mode_admits_everything_waiting_at_once():
    plan = _plan(backlog=200)
    assert plan.budget == 150 and plan.state == "growing", "no one-per-tick ramp: a call costs a few MB"
    assert _plan(backlog=30, active=10).budget == 40


def test_dynamic_mode_idles_at_the_minimum():
    assert _plan().budget == 2
    assert _plan(ceiling=80).budget == 2


def test_memory_lines_stop_new_calls_but_never_interrupt_running_ones():
    holding = _plan(mem_used_pct=86.0, active=40, backlog=100)
    assert holding.state == "holding" and holding.budget == 40
    critical = _plan(mem_used_pct=92.0, active=40, backlog=100)
    assert critical.state == "shrinking" and critical.budget == 2
    between = _plan(mem_used_pct=75.0, active=40, backlog=100, ceiling=60)
    assert between.state == "holding" and between.budget == 60, "keeps what was allowed, grows no further"


def test_the_server_cap_bounds_every_dynamic_number():
    plan = _plan(min_size=500, max_size=2500, hard_cap=150, backlog=1000)
    assert plan.floor == 150 and plan.budget == 150


def test_budget_from_settings_when_the_controller_has_not_ticked():
    assert pool.settings_budget({"llm_pool_mode": "static", "llm_pool_static_size": 40}, hard_cap=150) == 40
    assert pool.settings_budget({"llm_pool_mode": "dynamic", "llm_pool_min": 3}, hard_cap=150) == 3
    assert pool.settings_budget({"llm_pool_mode": "static", "llm_pool_static_size": 900}, hard_cap=150) == 150


@pytest.mark.asyncio
async def test_the_controller_publishes_the_runner_budget_without_autoscaling(runner_mode, monkeypatch):
    broker = MemoryBroker()

    async def all_settings(_session):
        return dict(settings_store.SETTING_DEFAULTS, llm_pool_mode="dynamic", llm_pool_min=2, llm_pool_max=120)

    async def backlog_count(_session):
        return 75

    monkeypatch.setattr(settings_store, "all_settings", all_settings)
    monkeypatch.setattr(pool, "backlog_count", backlog_count)
    monkeypatch.setattr(
        pool,
        "read_host_usage",
        lambda previous=None: (pool.HostUsage(1000, 600, 40.0, 5.0), {"at": 1.0, "total": 1.0, "idle": 1.0}),
    )

    def no_autoscale(*_args, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("the runner has no prefork pool to resize")

    monkeypatch.setattr(pool, "send_autoscale", no_autoscale)
    state = await pool.tick(None, broker=broker)
    assert state["executor"] == "runner" and state["hard_cap"] == 150
    assert state["budget"] == 75 and await pool.get_budget(broker, fallback=0) == 75


# ------------------------------------------------------------ settings/queue


def test_runner_mode_allows_provider_scale_pool_sizes(runner_mode):
    assert settings_store.validate_setting("llm_pool_static_size", 2500) == 2500
    with pytest.raises(settings_store.SettingsError):
        settings_store.validate_setting("llm_pool_max", 2501)


def test_prefork_mode_keeps_its_process_bounds(fast_settings):
    with pytest.raises(settings_store.SettingsError):
        settings_store.validate_setting("llm_pool_static_size", 100)


@pytest.mark.asyncio
async def test_llm_work_is_never_published_to_celery_in_runner_mode(runner_mode, monkeypatch):
    from app.workers.celery_app import celery_app

    sent: list[str] = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, **_kwargs: sent.append(name))
    dispatcher = dispatch.CeleryDispatcher()
    await dispatcher.enqueue("g1", "llm", high_priority=True)
    await dispatcher.enqueue("g2", "render")
    assert sent == ["app.workers.tasks.run_render"]

    background = dispatch.BackgroundCeleryDispatcher()
    try:
        await background.enqueue("g3", "render")
        await background.enqueue("g4", "llm")
    finally:
        background.close()
    assert sent == ["app.workers.tasks.run_render", "app.workers.tasks.run_render"]


@pytest.mark.asyncio
async def test_the_sweep_leaves_llm_builds_to_the_runner(db_session, workspace, runner_mode):
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        jobs = await submit_many(db_session, workspace["maker"], 2)
        rendering = await initial_build(db_session, jobs[1])
        rendering.stage, rendering.status, rendering.dispatch_state = "render", "rendering", "pending"
        await db_session.commit()
        count = await pipeline.dispatch_sweep(db_session)
        assert count == 1, "only the render build is re-dispatched"
    finally:
        dispatch.set_dispatcher(None)


# --------------------------------------------------------------- the runner


@pytest.mark.asyncio
async def test_a_burst_runs_in_parallel_and_still_releases_in_order(db_session, workspace, runner_mode):
    await _configure(db_session, static=5)
    jobs = await submit_many(db_session, workspace["maker"], 12)
    expected = [job.seq_no for job in jobs]
    maker_id = workspace["maker"].id
    client = SlowClient(delay=0.3)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        runner = LLMRunner(worker="test-runner", poll_s=0.05)
        await runner.run_until_idle(timeout_s=30)
        assert client.peak == 5, "exactly the Manager's number of calls at once"
        assert runner.started == 12
        assert await _statuses(db_session) == ["rendering"] * 12
        await run_pipeline()
    finally:
        dispatch.set_dispatcher(None)
    assert await _statuses(db_session) == ["ready"] * 12
    assert await released_sequence(db_session, maker_id) == expected


@pytest.mark.asyncio
async def test_a_provider_never_gets_more_calls_than_its_own_concurrency(db_session, workspace, runner_mode):
    await _configure(db_session, static=10, provider_concurrency=2)
    await submit_many(db_session, workspace["maker"], 6)
    client = SlowClient(delay=0.2)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        await LLMRunner(worker="test-runner", poll_s=0.05).run_until_idle(timeout_s=30)
    finally:
        dispatch.set_dispatcher(None)
    assert client.peak == 2
    assert await _statuses(db_session) == ["rendering"] * 6


@pytest.mark.asyncio
async def test_no_database_connection_is_held_during_the_provider_call(db_session, workspace, runner_mode):
    from app.db import get_engine

    await _configure(db_session, static=1)
    await db_session.close()
    await submit_many(db_session, workspace["maker"], 2)
    await db_session.close()
    engine = get_engine()
    client = SlowClient(delay=0.2, probe=lambda: engine.pool.checkedout())
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        await LLMRunner(worker="test-runner", poll_s=5.0).run_until_idle(timeout_s=30)
    finally:
        dispatch.set_dispatcher(None)
    assert client.probes == [0, 0], "a call in flight must not pin a connection"


@pytest.mark.asyncio
async def test_builds_start_in_release_order_with_earlier_retries_first(db_session, workspace, runner_mode):
    jobs = await submit_many(db_session, workspace["maker"], 3)
    first = await initial_build(db_session, jobs[0])
    first.status, first.next_retry_at = "retry_wait", utcnow() - timedelta(seconds=1)
    later = await initial_build(db_session, jobs[2])
    later.status, later.next_retry_at = "retry_wait", utcnow() + timedelta(minutes=5)
    await db_session.commit()
    rows = await claimable_builds(db_session, limit=10)
    ordered = [generation_id for generation_id, _provider in rows]
    second = await initial_build(db_session, jobs[1])
    assert ordered == [first.id, second.id], "a backoff still pending is not claimable"


@pytest.mark.asyncio
async def test_failed_attempts_are_retried_by_the_runner(db_session, workspace, runner_mode):
    await _configure(db_session, static=4)
    jobs = await submit_many(db_session, workspace["maker"], 2)
    build_ids = [(await initial_build(db_session, job)).id for job in jobs]
    client = SlowClient(delay=0.05, fail_first=2)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        await LLMRunner(worker="test-runner", poll_s=0.05).run_until_idle(timeout_s=30)
    finally:
        dispatch.set_dispatcher(None)
    assert await _statuses(db_session) == ["rendering", "rendering"]
    attempts = [await llm_attempt_count(db_session, build_id) for build_id in build_ids]
    assert sum(attempts) == 4, "each failed attempt is recorded and retried under the normal policy"


@pytest.mark.asyncio
async def test_a_build_cancelled_mid_call_is_not_resurrected(db_session, workspace, runner_mode):
    await _configure(db_session, static=2)
    jobs = await submit_many(db_session, workspace["maker"], 2)
    client = SlowClient(delay=0.6)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    runner = LLMRunner(worker="test-runner", poll_s=0.05)
    try:
        idle = asyncio.create_task(runner.run_until_idle(timeout_s=30))
        while client.current < 2:
            await asyncio.sleep(0.02)
        await pipeline.cancel_job(db_session, job=jobs[0], actor_id=workspace["maker"].id)
        await db_session.commit()
        await idle
    finally:
        dispatch.set_dispatcher(None)
    cancelled = await initial_build(db_session, jobs[0])
    kept = await initial_build(db_session, jobs[1])
    await db_session.refresh(cancelled)
    await db_session.refresh(kept)
    assert cancelled.status == "cancelled"
    assert kept.status == "rendering"


@pytest.mark.asyncio
async def test_stopping_lets_the_calls_in_flight_finish(db_session, workspace, runner_mode):
    await _configure(db_session, static=3)
    await submit_many(db_session, workspace["maker"], 6)
    client = SlowClient(delay=0.4)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    runner = LLMRunner(worker="test-runner", poll_s=0.05, drain_s=10)
    try:
        task = asyncio.create_task(runner.run())
        while client.current < 3:
            await asyncio.sleep(0.02)
        runner.stop()
        await asyncio.wait_for(task, timeout=10)
    finally:
        dispatch.set_dispatcher(None)
    statuses = await _statuses(db_session)
    assert statuses.count("rendering") == 3, "the three calls in flight finished"
    assert statuses.count("queued") == 3, "nothing new started after stop"
    assert client.calls == 3


@pytest.mark.asyncio
async def test_the_runner_follows_the_controllers_budget(db_session, workspace, runner_mode):
    await _configure(db_session, static=10)
    await submit_many(db_session, workspace["maker"], 8)
    await pool.set_budget(get_broker(), 3)
    client = SlowClient(delay=0.2)
    set_client_override(client)
    dispatch.set_dispatcher(dispatch.NoopDispatcher())
    try:
        await LLMRunner(worker="test-runner", poll_s=0.05).run_until_idle(timeout_s=30)
    finally:
        dispatch.set_dispatcher(None)
    assert client.peak == 3
