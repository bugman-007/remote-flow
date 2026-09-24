"""CONC-2: the static/dynamic LLM pool policy and its admission counters."""

from __future__ import annotations

import pytest

from app.services import pool
from app.services.broker import MemoryBroker

BASE = dict(
    static_size=6,
    min_size=2,
    max_size=8,
    ceiling=8,
    mem_used_pct=50.0,
    grow_below=70.0,
    admit_above=85.0,
    shrink_above=90.0,
    shrink_below=80.0,
    step=1,
    grow_interval_s=15.0,
    shrink_cooldown_s=45.0,
)


def plan(**overrides):
    return pool.decide_pool(**{**BASE, **overrides})


# ------------------------------------------------------------------ static


def test_static_mode_pins_the_pool_to_the_managers_number():
    result = plan(mode="static", static_size=6, active=0, backlog=50, mem_used_pct=20)
    assert (result.floor, result.ceiling, result.budget) == (6, 6, 6)
    assert result.state == "static"


def test_static_mode_still_honours_the_configured_maximum():
    result = plan(mode="static", static_size=32)
    assert result.ceiling == 8


# ----------------------------------------------------------------- dynamic


def test_grows_one_step_when_memory_is_comfortable_and_work_is_waiting():
    result = plan(mode="dynamic", ceiling=4, mem_used_pct=55, active=4, backlog=12, since_grow_s=100)
    assert result.state == "growing"
    assert result.ceiling == 5
    assert result.budget == 5


def test_growth_waits_for_the_interval_and_for_backlog():
    throttled = plan(mode="dynamic", ceiling=4, mem_used_pct=55, backlog=12, since_grow_s=3)
    assert throttled.ceiling == 4 and throttled.state == "steady"
    assert throttled.grew is False


def test_a_quiet_box_drops_to_the_warm_floor_and_gives_the_ram_back():
    idle = plan(mode="dynamic", ceiling=8, mem_used_pct=20, backlog=0, active=0)
    assert (idle.floor, idle.ceiling, idle.size, idle.budget) == (2, 2, 2, 2)
    assert idle.state == "steady" and idle.grew is False


def test_admitting_one_more_never_raises_the_ceiling_without_the_interval():
    """The ramp is the throttle: admission may track running work, growth may not."""
    result = plan(mode="dynamic", ceiling=4, mem_used_pct=55, backlog=9, active=4, since_grow_s=1)
    assert result.ceiling == 4, "the high-water mark is still ramping"
    assert result.size == 4 and result.budget == 4, "running work is never evicted"
    assert result.state == "steady" and result.grew is False


def test_a_release_is_rate_limited_so_a_memory_spike_cannot_thrash_the_pool():
    just_released = plan(mode="dynamic", ceiling=8, mem_used_pct=94, active=2, since_shrink_s=5)
    assert just_released.state == "holding"
    assert just_released.size == 8, "no second release inside the cooldown"
    assert just_released.budget == 2, "but nothing new is admitted either"
    later = plan(mode="dynamic", ceiling=8, mem_used_pct=94, active=2, since_shrink_s=50)
    assert later.state == "shrinking" and later.size == 2


def test_a_first_release_is_immediate_even_without_a_shrink_history():
    """since_shrink_s is infinite when no release has ever happened."""
    first = plan(mode="dynamic", ceiling=8, mem_used_pct=95, active=0)
    assert first.state == "shrinking" and first.size == 2


def test_growth_stops_at_the_configured_maximum():
    result = plan(mode="dynamic", ceiling=8, mem_used_pct=30, backlog=99, since_grow_s=100)
    assert result.ceiling == 8
    assert result.state == "steady"


def test_holds_at_the_admission_line_and_stops_publishing_more_work():
    result = plan(mode="dynamic", ceiling=6, mem_used_pct=87, active=6, backlog=9)
    assert result.state == "holding"
    assert result.budget == 6, "already-running work continues, nothing new is admitted"
    assert result.ceiling == 6


def test_holding_never_starves_the_queue_below_the_floor():
    result = plan(mode="dynamic", ceiling=8, mem_used_pct=86, active=0, backlog=9)
    assert result.budget == 2, "the floor keeps progress alive when the box is busy rendering"


def test_releases_idle_processors_above_the_shrink_line():
    result = plan(mode="dynamic", ceiling=8, mem_used_pct=93, active=3, since_shrink_s=100)
    assert result.state == "shrinking"
    assert result.ceiling == 3, "never shrink below what is actually running"
    assert result.budget == 3


def test_shrink_waits_for_the_cooldown_and_for_an_idle_child():
    cooling = plan(mode="dynamic", ceiling=8, mem_used_pct=93, active=3, since_shrink_s=5)
    assert cooling.state == "holding" and cooling.ceiling == 8
    busy = plan(mode="dynamic", ceiling=8, mem_used_pct=95, active=8, since_shrink_s=100)
    assert busy.ceiling == 8, "every processor is busy; billiard would refuse anyway"


def test_shrink_never_goes_below_the_minimum():
    result = plan(mode="dynamic", ceiling=5, mem_used_pct=97, active=0, since_shrink_s=999)
    assert result.ceiling == 2


def test_hysteresis_keeps_the_pool_small_until_memory_recovers():
    shrunk = plan(mode="dynamic", ceiling=8, mem_used_pct=91, active=2, since_shrink_s=100)
    recovering = plan(mode="dynamic", ceiling=shrunk.ceiling, mem_used_pct=76, backlog=20, since_grow_s=100)
    assert recovering.ceiling == shrunk.ceiling, "76 % must not immediately grow back"
    assert recovering.state == "steady"


def test_cpu_can_veto_growth_but_never_forces_a_shrink():
    loaded = plan(mode="dynamic", ceiling=4, mem_used_pct=40, cpu_pct=97, backlog=9, since_grow_s=100)
    assert loaded.state == "steady" and loaded.ceiling == 4
    memory_only = plan(mode="dynamic", ceiling=4, mem_used_pct=93, cpu_pct=10, active=1, since_shrink_s=100)
    assert memory_only.state == "shrinking"


# ----------------------------------------------------------------- counters


@pytest.mark.asyncio
async def test_active_counter_enforces_the_budget_and_self_heals():
    broker = MemoryBroker()
    first = await pool.begin_active(broker, budget=2)
    second = await pool.begin_active(broker, budget=2)
    assert first and second
    assert await pool.begin_active(broker, budget=2) is None
    assert await pool.active_count(broker) == 2

    await pool.end_active(broker, first)
    assert await pool.active_count(broker) == 1
    assert await pool.begin_active(broker, budget=2) is not None


@pytest.mark.asyncio
async def test_a_zero_budget_means_no_controller_so_nothing_is_gated():
    broker = MemoryBroker()
    assert await pool.begin_active(broker, budget=0) is not None


@pytest.mark.asyncio
async def test_inflight_markers_are_idempotent_and_clearable():
    broker = MemoryBroker()
    await pool.mark_inflight(broker, "gen-1")
    await pool.mark_inflight(broker, "gen-1")
    await pool.mark_inflight(broker, "gen-2")
    assert await pool.inflight_count(broker) == 2
    await pool.clear_inflight(broker, "gen-1")
    assert await pool.inflight_count(broker) == 1


@pytest.mark.asyncio
async def test_slots_are_a_real_concurrency_limit_not_a_rate_window():
    broker = MemoryBroker()
    tokens = [await broker.acquire_slot("k", limit=2, timeout_s=0, ttl_s=600) for _ in range(3)]
    assert tokens[0] and tokens[1]
    assert tokens[2] is None
    assert await broker.slots_in_use("k") == 2
    await broker.release_slot("k", tokens[0])
    assert await broker.slots_in_use("k") == 1


@pytest.mark.asyncio
async def test_redis_broker_coerces_fractional_ttls():
    """redis-py raises DataError on a float EX; the controller passes floats."""
    from app.services.broker import RedisBroker

    class FakeRedis:
        def __init__(self):
            self.calls = []

        async def set(self, key, value, ex=None):
            self.calls.append((key, value, ex))

        async def expire(self, key, ttl):
            self.calls.append(("expire", key, ttl))

        async def incr(self, key):
            return 1

    client = FakeRedis()
    broker = RedisBroker(client)
    await broker.set_int("k", 6, ttl_seconds=600.0)
    await broker.set_text("t", "v", ttl_seconds=3600.0)
    await broker.incr("c", ttl_seconds=120.0)
    assert client.calls[0] == ("k", 6, 600)
    assert client.calls[1] == ("t", "v", 3600)
    assert client.calls[2] == ("expire", "c", 120)
    assert all(isinstance(call[-1], int) for call in client.calls)


def test_autoscale_targets_only_the_llm_workers(monkeypatch):
    """kombu matches `destination` exactly, so the llm worker is selected by glob."""
    captured: dict = {}

    class FakeControl:
        def autoscale(self, **kwargs):
            captured.update(kwargs)

    class FakeApp:
        control = FakeControl()

    import app.workers.celery_app as celery_module

    monkeypatch.setattr(celery_module, "celery_app", FakeApp())
    result = pool.send_autoscale(6, 6)
    assert result["sent"] is True
    assert captured == {"max": 6, "min": 6, "pattern": "llm@*", "matcher": "glob"}


def test_autoscale_reports_a_broker_failure_instead_of_raising(monkeypatch):
    class BrokenControl:
        def autoscale(self, **kwargs):
            raise RuntimeError("broker down")

    class FakeApp:
        control = BrokenControl()

    import app.workers.celery_app as celery_module

    monkeypatch.setattr(celery_module, "celery_app", FakeApp())
    result = pool.send_autoscale(4, 2)
    assert result["sent"] is False
    assert "broker down" in result["error"]


# ---------------------------------------------------------------- controller


SETTINGS = {
    "llm_pool_mode": "dynamic",
    "llm_pool_static_size": 6,
    "llm_pool_min": 2,
    "llm_pool_max": 8,
    "llm_grow_below_pct": 70,
    "llm_admit_above_pct": 85,
    "llm_shrink_above_pct": 90,
    "llm_shrink_below_pct": 80,
    "llm_scale_step": 1,
    "llm_scale_interval_s": 15,
    "llm_shrink_cooldown_s": 45,
}


async def _tick(monkeypatch, broker, *, mem_pct=30.0, backlog=9, active=0, settings=None, sent=None):
    """Drive one controller cycle against a fake box and a fake process count."""
    from app.services import settings_store

    values = {**SETTINGS, **(settings or {})}

    async def all_settings(_session):
        return dict(values)

    monkeypatch.setattr(settings_store, "all_settings", all_settings)
    monkeypatch.setattr(
        pool,
        "read_host_usage",
        lambda previous=None: (
            pool.HostUsage(mem_total_mb=1000, mem_available_mb=700, mem_used_pct=mem_pct, cpu_pct=10.0),
            {"at": 1.0, "total": 1.0, "idle": 1.0},
        ),
    )

    async def backlog_count(_session):
        return backlog

    monkeypatch.setattr(pool, "backlog_count", backlog_count)
    calls = sent if sent is not None else []

    def send_autoscale(ceiling, floor):
        calls.append((ceiling, floor))
        return {"sent": True}

    monkeypatch.setattr(pool, "send_autoscale", send_autoscale)
    return await pool.tick(None, broker=broker), calls


@pytest.mark.asyncio
async def test_the_growth_timer_only_resets_when_the_ceiling_actually_moves(monkeypatch):
    """A busy box used to re-stamp the growth key every tick, freezing the ramp."""
    broker = MemoryBroker()
    sent: list[tuple[int, int]] = []

    state, _ = await _tick(monkeypatch, broker, sent=sent)
    assert state["ceiling"] == 3 and state["size"] == 2, "one step, capped by what can run"
    assert sent[-1] == (2, 2), "the controller publishes the size, never the high-water mark"

    state, _ = await _tick(monkeypatch, broker, sent=sent)
    assert state["ceiling"] == 3, "a second tick must not step inside the interval"

    import time as _time

    await broker.set_text(pool.LLM_LAST_GROW_KEY, str(_time.time() - 20))
    state, _ = await _tick(monkeypatch, broker, sent=sent)
    assert state["ceiling"] == 4, "the ramp resumes once the interval has passed"


@pytest.mark.asyncio
async def test_a_memory_emergency_publishes_a_smaller_pool(monkeypatch):
    broker = MemoryBroker()
    sent: list[tuple[int, int]] = []
    await _tick(monkeypatch, broker, settings={"llm_pool_mode": "static", "llm_pool_static_size": 6})
    await broker.set_int(pool.LLM_CEILING_KEY, 8)
    state, _ = await _tick(monkeypatch, broker, mem_pct=95.0, backlog=9, sent=sent)
    assert state["state"] == "shrinking" and state["size"] == 2
    assert sent[-1] == (2, 2), "an idle pool is released instead of only being starved"
    assert state["budget"] == 2


@pytest.mark.asyncio
async def test_an_idle_box_releases_everything_above_the_floor(monkeypatch):
    broker = MemoryBroker()
    sent: list[tuple[int, int]] = []
    await broker.set_int(pool.LLM_CEILING_KEY, 8)
    state, _ = await _tick(monkeypatch, broker, backlog=0, sent=sent)
    assert state["size"] == 2 and sent[-1] == (2, 2)


@pytest.mark.asyncio
async def test_static_mode_pins_the_pool_and_still_admits(monkeypatch):
    broker = MemoryBroker()
    sent: list[tuple[int, int]] = []
    state, _ = await _tick(
        monkeypatch, broker, backlog=20, sent=sent,
        settings={"llm_pool_mode": "static", "llm_pool_static_size": 6},
    )
    assert state["mode"] == "static" and state["size"] == 6 and state["budget"] == 6
    assert sent[-1] == (6, 6)
