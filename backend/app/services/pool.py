"""Dynamic worker-pool control (CONC-2): static sizing vs memory-driven autoscaling.

The LLM queue is I/O bound - a child sits at ~0 % CPU for the two or three
minutes its provider call takes - so the pool is sized to keep as many calls in
flight as the box can afford, and the box's binding resource is **memory**, not
CPU. Two modes:

* ``static`` - the Manager picks the number of processors; the pool is pinned.
* ``dynamic`` - the pool grows while memory is comfortable and is pulled back
  when it is not, always by letting running jobs finish first.

"Pulling back" is safe by construction: billiard's ``Pool.shrink`` only
terminates children with no active job and raises ``ValueError("Can't shrink
pool. All processes busy!")`` rather than killing a busy one, and Celery's
``autoscale`` control command goes through exactly that path. On top of that we
never dispatch more work than the current ceiling, so shrinking always finds an
idle child.

With ``LLM_EXECUTOR=runner`` the LLM stage runs in the async runner
(:mod:`app.workers.llm_runner`) instead of a prefork pool: the same two modes
then size the number of parallel provider calls (:func:`decide_runner_pool`),
and the runner reads the budget published here on every pass.

Everything here is a pure function of its inputs plus a handful of
self-expiring Redis keys; nothing needs a schema migration.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.services.broker import Broker

#: Admission budget: how many LLM builds may be in flight at once.
LLM_BUDGET_KEY = "rf:llm:budget"
#: zset of published-but-unfinished LLM builds (member: generation id).
LLM_INFLIGHT_KEY = "rf:llm:inflight"
#: zset of provider calls currently running (member: attempt token).
LLM_ACTIVE_KEY = "rf:llm:active"
#: Last applied autoscale range + the decision that produced it (UI/metrics).
LLM_PLAN_KEY = "rf:llm:plan"
#: Rate-limits the control broadcast: the last time we grew / shrank.
LLM_LAST_GROW_KEY = "rf:llm:last_grow"
LLM_LAST_SHRINK_KEY = "rf:llm:last_shrink"
#: Previous /proc/stat sample for the CPU delta.
LLM_CPU_SAMPLE_KEY = "rf:llm:cpu_sample"
#: The ceiling the controller last applied.
LLM_CEILING_KEY = "rf:llm:ceiling"
#: When the range was last broadcast, so a restarted worker converges again.
LLM_PUBLISHED_KEY = "rf:llm:published"

#: Re-broadcast the range this often even when the plan has not changed. A worker
#: that was restarted comes back with the `--autoscale` range from its command
#: line, and the "only talk when the plan changes" rule would otherwise leave it
#: running a range nobody asked for.
PUBLISH_KEEPALIVE_S = 60.0

MODE_STATIC = "static"
MODE_DYNAMIC = "dynamic"
POOL_MODES = (MODE_STATIC, MODE_DYNAMIC)

#: Providers refuse to be hammered; never size the pool past the queue's own
#: provider tier even if memory allows.
ACTIVE_TTL_S = 900.0


# ----------------------------------------------------------------- resources


@dataclass(frozen=True)
class HostUsage:
    """A snapshot of the machine, read from /proc (shared with the container)."""

    mem_total_mb: int
    mem_available_mb: int
    mem_used_pct: float
    cpu_pct: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_meminfo(path: str = "/proc/meminfo") -> tuple[int, int]:
    total_kb = available_kb = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available_kb = int(line.split()[1])
            if total_kb and available_kb:
                break
    return total_kb, available_kb


def _read_cpu_jiffies(path: str = "/proc/stat") -> tuple[float, float]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("cpu "):
                parts = [float(value) for value in line.split()[1:]]
                total = sum(parts)
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
                return total, idle
    return 0.0, 0.0


def read_host_usage(previous: dict[str, Any] | None = None) -> tuple[HostUsage, dict[str, Any]]:
    """Memory + CPU for the whole box.

    ``MemAvailable`` (not ``MemFree``) is the honest number: page cache is
    reclaimable, so it counts as free memory. CPU needs two samples, hence the
    returned state blob that the caller stores for the next tick.
    """
    total_kb, available_kb = _read_meminfo()
    total_mb = total_kb // 1024
    available_mb = available_kb // 1024
    used_pct = 0.0 if total_kb == 0 else round((total_kb - available_kb) / total_kb * 100, 1)

    total, idle = _read_cpu_jiffies()
    sample = {"total": total, "idle": idle, "at": time.time()}
    cpu_pct: float | None = None
    if previous and previous.get("at") and total > float(previous.get("total", 0)):
        delta_total = total - float(previous["total"])
        delta_idle = idle - float(previous.get("idle", 0.0))
        cpu_pct = round(max(0.0, min(100.0, (1 - delta_idle / delta_total) * 100)), 1)
    return HostUsage(total_mb, available_mb, used_pct, cpu_pct), sample


# ------------------------------------------------------------------ counters


async def mark_inflight(broker: Broker, generation_id: str, *, ttl_s: float = ACTIVE_TTL_S) -> None:
    """Record that this build's LLM task is on the queue (self-expiring)."""
    await broker.zadd(LLM_INFLIGHT_KEY, generation_id, time.time() + ttl_s)


async def clear_inflight(broker: Broker, generation_id: str) -> None:
    await broker.zrem(LLM_INFLIGHT_KEY, generation_id)


async def inflight_count(broker: Broker) -> int:
    return await broker.zcount(LLM_INFLIGHT_KEY)


async def begin_active(broker: Broker, *, budget: int, ttl_s: float = ACTIVE_TTL_S) -> str | None:
    """Claim one concurrency slot, or None when the budget is exhausted.

    A budget of ``0`` means "no controller has published one" and is treated as
    unlimited: the autoscale ceiling still bounds the pool, and the admission
    gate is an optimisation on top of it, never the safety net.
    """
    if budget > 0 and await broker.zcount(LLM_ACTIVE_KEY) >= budget:
        return None
    token = f"{time.time()}:{id(broker)}:{int(time.time() * 1000) % 100000}"
    await broker.zadd(LLM_ACTIVE_KEY, token, time.time() + ttl_s)
    # Re-check after writing to close the race between two workers reading first.
    if budget > 0 and await broker.zcount(LLM_ACTIVE_KEY) > budget:
        await broker.zrem(LLM_ACTIVE_KEY, token)
        return None
    return token


async def end_active(broker: Broker, token: str | None) -> None:
    if token:
        await broker.zrem(LLM_ACTIVE_KEY, token)


async def active_count(broker: Broker) -> int:
    return await broker.zcount(LLM_ACTIVE_KEY)


async def get_budget(broker: Broker, *, fallback: int) -> int:
    value = await broker.get_int(LLM_BUDGET_KEY)
    return value if value > 0 else fallback


async def set_budget(broker: Broker, budget: int, *, ttl_s: float = 300.0) -> None:
    await broker.set_int(LLM_BUDGET_KEY, budget, ttl_seconds=ttl_s)


async def load_plan(broker: Broker) -> dict[str, Any] | None:
    raw = await broker.get_text(LLM_PLAN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


async def store_plan(broker: Broker, plan: dict[str, Any], *, ttl_s: float = 3600.0) -> None:
    await broker.set_text(LLM_PLAN_KEY, json.dumps(plan, default=str), ttl_seconds=ttl_s)


async def _stamp(broker: Broker, key: str, *, ttl_s: float = 3600.0) -> None:
    await broker.set_text(key, str(time.time()), ttl_seconds=ttl_s)


async def _age(broker: Broker, key: str) -> float:
    raw = await broker.get_text(key)
    if not raw:
        return float("inf")
    try:
        return max(0.0, time.time() - float(raw))
    except ValueError:
        return float("inf")


# ------------------------------------------------------------------- decision


@dataclass(frozen=True)
class PoolPlan:
    """What the pool should look like right now.

    ``floor`` is the warm minimum the autoscaler may fall back to; ``size`` is
    the maximum it may reach *right now* and doubles as the admission budget.
    The controller moves both every tick, because Celery's own autoscaler only
    shrinks after it has scaled up at least once - it cannot be trusted to bring
    an idle pool back down.
    """

    floor: int
    ceiling: int  # ramp high-water mark for this tick
    size: int
    budget: int
    state: str
    reason: str
    #: True when this tick raised the ceiling, i.e. the growth timer must reset.
    grew: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bounded(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def decide_pool(
    *,
    mode: str,
    static_size: int,
    min_size: int,
    max_size: int,
    ceiling: int | None,
    mem_used_pct: float,
    cpu_pct: float | None = None,
    active: int = 0,
    backlog: int = 0,
    grow_below: float = 70.0,
    admit_above: float = 85.0,
    shrink_above: float = 90.0,
    shrink_below: float = 80.0,
    step: int = 1,
    grow_interval_s: float = 15.0,
    shrink_cooldown_s: float = 45.0,
    since_grow_s: float = float("inf"),
    since_shrink_s: float = float("inf"),
) -> PoolPlan:
    """The whole policy, as one testable function.

    ``mem_used_pct`` is the only hard limit; CPU can veto growth but never
    forces a release (CPU contention slows work down, it does not break it -
    memory exhaustion does).
    """
    max_size = max(1, max_size)
    min_size = _bounded(min_size, 1, max_size)
    active = max(0, active)
    floor = min_size

    if mode == MODE_STATIC:
        size = _bounded(static_size, 1, max_size)
        return PoolPlan(floor=size, ceiling=size, size=size, budget=size, state="static", reason="static size")

    # High-water mark the controller is willing to ramp to. It only ever moves
    # one step per interval, so a burst cannot add several children at once.
    ramp = _bounded(ceiling if ceiling else min_size, min_size, max_size)
    grew = False
    if mem_used_pct < grow_below and backlog > 0 and ramp < max_size and since_grow_s >= grow_interval_s:
        if cpu_pct is None or cpu_pct < 90.0:
            ramp = _bounded(ramp + max(1, step), min_size, max_size)
            grew = True

    # 1. Memory critical: keep running work, release everything idle. A release
    #    only ever terminates children with no job (billiard refuses otherwise),
    #    and we allow one release per cooldown so a spike cannot thrash the pool.
    if mem_used_pct >= shrink_above:
        target = max(floor, active)
        if target < ramp and since_shrink_s < shrink_cooldown_s:
            return PoolPlan(
                floor, ramp, ramp, target, "holding",
                f"memory {mem_used_pct:.0f}% - cooling down after the last release",
            )
        return PoolPlan(
            floor, target, target, target, "shrinking",
            f"memory {mem_used_pct:.0f}% >= {shrink_above:.0f}% - releasing idle processors",
        )

    # 2. Warning band: hold the line and stop admitting new work.
    if mem_used_pct >= admit_above:
        target = max(floor, min(ramp, active))
        return PoolPlan(floor, min(ramp, target), target, target, "holding",
                        f"memory {mem_used_pct:.0f}% >= {admit_above:.0f}% - no new work")

    # 3. A quiet box should be a cheap box: with nothing running and nothing
    #    waiting, drop to the warm floor and give the RAM back.
    if backlog == 0 and active == 0:
        return PoolPlan(floor, floor, floor, floor, "steady", "idle - warm floor only")

    # 4. Comfortable with work to do: allow one slot of headroom past what is
    #    already running, capped by the ramp.
    size = _bounded(active + 1, floor, ramp)
    state = "growing" if grew else "steady"
    return PoolPlan(floor, ramp, size, size, state,
                    f"memory {mem_used_pct:.0f}%, {backlog} waiting, {active} running",
                    grew=grew)


def decide_runner_pool(
    *,
    mode: str,
    static_size: int,
    min_size: int,
    max_size: int,
    hard_cap: int,
    ceiling: int | None,
    mem_used_pct: float,
    active: int = 0,
    backlog: int = 0,
    grow_below: float = 70.0,
    admit_above: float = 85.0,
    shrink_above: float = 90.0,
) -> PoolPlan:
    """The policy for the async runner, where every number is parallel provider calls.

    A call costs a few MB inside the runner instead of a ~270 MB process, so there
    is nothing to ramp up or tear down: dynamic mode admits as many calls as are
    waiting (between the minimum and the maximum) while memory is comfortable, and
    stops admitting new ones when it is not. A running call is never interrupted -
    a smaller budget only means fewer new calls start until enough have finished.
    ``hard_cap`` is what this server's runner can hold; every number is clamped to it.
    """
    cap = max(1, hard_cap)
    if mode == MODE_STATIC:
        size = _bounded(static_size, 1, cap)
        reason = "static size" if static_size <= cap else f"static size, capped at {cap} by this server"
        return PoolPlan(floor=size, ceiling=size, size=size, budget=size, state="static", reason=reason)

    max_size = _bounded(max_size, 1, cap)
    min_size = _bounded(min_size, 1, max_size)
    active = max(0, active)
    backlog = max(0, backlog)
    floor = min_size
    previous = _bounded(ceiling if ceiling else floor, floor, max_size)

    if mem_used_pct >= shrink_above:
        return PoolPlan(
            floor, floor, floor, floor, "shrinking",
            f"memory {mem_used_pct:.0f}% >= {shrink_above:.0f}% - no new calls until running ones finish",
        )
    if mem_used_pct >= admit_above:
        target = _bounded(active, floor, max_size)
        return PoolPlan(floor, target, target, target, "holding",
                        f"memory {mem_used_pct:.0f}% >= {admit_above:.0f}% - no new calls")
    if backlog == 0 and active == 0:
        return PoolPlan(floor, floor, floor, floor, "steady", "idle")
    demand = _bounded(active + backlog, floor, max_size)
    if mem_used_pct >= grow_below:
        # Between the growth line and the admission line: keep what is already
        # allowed, never more.
        target = min(demand, max(previous, _bounded(active, floor, max_size)))
        return PoolPlan(
            floor, target, target, target, "holding",
            f"memory {mem_used_pct:.0f}% >= {grow_below:.0f}% - not growing; {backlog} waiting, {active} running",
        )
    grew = demand > previous
    return PoolPlan(floor, demand, demand, demand, "growing" if grew else "steady",
                    f"memory {mem_used_pct:.0f}%, {backlog} waiting, {active} running", grew=grew)


def settings_budget(values: dict[str, Any], *, hard_cap: int) -> int:
    """The budget straight from the saved settings, for when no controller has ticked."""
    mode = str(values.get("llm_pool_mode") or MODE_STATIC)
    if mode == MODE_DYNAMIC:
        return _bounded(_setting_int(values, "llm_pool_min", 2), 1, max(1, hard_cap))
    return _bounded(_setting_int(values, "llm_pool_static_size", 6), 1, max(1, hard_cap))


# ----------------------------------------------------------------- controller


def _setting_int(values: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(values.get(key, default))
    except (TypeError, ValueError):
        return default


def _setting_float(values: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return default


async def backlog_count(session) -> int:
    """LLM builds waiting for a processor right now."""
    from sqlalchemy import func, select

    from app.models import Generation

    return int(
        (
            await session.execute(
                select(func.count(Generation.id)).where(
                    Generation.stage == "llm", Generation.status.in_(("queued", "retry_wait"))
                )
            )
        ).scalar_one()
    )


def send_autoscale(ceiling: int, floor: int) -> dict[str, Any]:
    """Push the range to the llm worker (control broadcast, no reply needed).

    The target is a glob: kombu matches ``pattern`` with ``matcher="glob"``,
    while ``destination`` is compared with ``hostname in destination`` - an
    exact match only, so ``destination=["llm@*"]`` silently reaches nobody.
    """
    from app.workers.celery_app import celery_app

    try:
        celery_app.control.autoscale(max=ceiling, min=floor, pattern="llm@*", matcher="glob")
    except Exception as exc:  # noqa: BLE001 - a broker hiccup must not kill the tick
        return {"sent": False, "error": str(exc)[:200]}
    return {"sent": True}


async def tick(session, *, broker: Broker | None = None) -> dict[str, Any]:
    """One control cycle: measure, decide, apply. Safe to run every 10 s."""
    from app.config import get_settings
    from app.services import settings_store
    from app.services.broker import get_broker

    config = get_settings()
    runner = config.llm_runner_enabled
    hard_cap = max(1, int(config.llm_runner_max_concurrency))
    broker = broker or get_broker()
    values = await settings_store.all_settings(session)
    mode = str(values.get("llm_pool_mode") or MODE_STATIC)
    if mode not in POOL_MODES:
        mode = MODE_STATIC

    usage_previous = None
    raw_sample = await broker.get_text(LLM_CPU_SAMPLE_KEY)
    if raw_sample:
        try:
            usage_previous = json.loads(raw_sample)
        except (TypeError, ValueError):
            usage_previous = None
    usage, sample = read_host_usage(usage_previous)
    await broker.set_text(LLM_CPU_SAMPLE_KEY, json.dumps(sample), ttl_seconds=3600)

    active = await active_count(broker)
    inflight = await inflight_count(broker)
    backlog = await backlog_count(session)
    ceiling_raw = await broker.get_int(LLM_CEILING_KEY)

    if runner:
        plan = decide_runner_pool(
            mode=mode,
            static_size=_setting_int(values, "llm_pool_static_size", 6),
            min_size=_setting_int(values, "llm_pool_min", 2),
            max_size=_setting_int(values, "llm_pool_max", 8),
            hard_cap=hard_cap,
            ceiling=ceiling_raw or None,
            mem_used_pct=usage.mem_used_pct,
            active=active,
            backlog=backlog,
            grow_below=_setting_float(values, "llm_grow_below_pct", 70),
            admit_above=_setting_float(values, "llm_admit_above_pct", 85),
            shrink_above=_setting_float(values, "llm_shrink_above_pct", 90),
        )
    else:
        plan = decide_pool(
            mode=mode,
            static_size=_setting_int(values, "llm_pool_static_size", 6),
            min_size=_setting_int(values, "llm_pool_min", 2),
            max_size=_setting_int(values, "llm_pool_max", 8),
            ceiling=ceiling_raw or None,
            mem_used_pct=usage.mem_used_pct,
            cpu_pct=usage.cpu_pct,
            active=active,
            backlog=backlog,
            grow_below=_setting_float(values, "llm_grow_below_pct", 70),
            admit_above=_setting_float(values, "llm_admit_above_pct", 85),
            shrink_above=_setting_float(values, "llm_shrink_above_pct", 90),
            shrink_below=_setting_float(values, "llm_shrink_below_pct", 80),
            step=_setting_int(values, "llm_scale_step", 1),
            grow_interval_s=_setting_float(values, "llm_scale_interval_s", 15),
            shrink_cooldown_s=_setting_float(values, "llm_shrink_cooldown_s", 45),
            since_grow_s=await _age(broker, LLM_LAST_GROW_KEY),
            since_shrink_s=await _age(broker, LLM_LAST_SHRINK_KEY),
        )

    previous = await load_plan(broker)
    applied = False
    control: dict[str, Any] = {"sent": False}
    if runner:
        # The runner reads the budget from Redis on every pass; there is no
        # process pool to resize.
        applied = True
    else:
        changed = plan.size != (previous or {}).get("size") or plan.floor != (previous or {}).get("floor")
        stale = await _age(broker, LLM_PUBLISHED_KEY) >= PUBLISH_KEEPALIVE_S
        if changed or stale:
            control = send_autoscale(plan.size, plan.floor)
            applied = bool(control.get("sent"))
            if applied:
                await _stamp(broker, LLM_PUBLISHED_KEY)
    if plan.ceiling != ceiling_raw:
        await broker.set_int(LLM_CEILING_KEY, plan.ceiling, ttl_seconds=3600)
    if plan.grew:
        await _stamp(broker, LLM_LAST_GROW_KEY)
    elif plan.state == "shrinking" and plan.size < (previous or {}).get("size", plan.size):
        await _stamp(broker, LLM_LAST_SHRINK_KEY)
    await set_budget(broker, plan.budget, ttl_s=600.0)

    state = {
        "mode": mode,
        "executor": "runner" if runner else "celery",
        "hard_cap": hard_cap if runner else None,
        "state": plan.state,
        "reason": plan.reason,
        "applied": applied,
        "control": control,
        "updated_at": time.time(),
        "floor": plan.floor,
        "ceiling": plan.ceiling,
        "size": plan.size,
        "budget": plan.budget,
        "active": active,
        "inflight": inflight,
        "backlog": backlog,
        "memory_used_pct": usage.mem_used_pct,
        "memory_available_mb": usage.mem_available_mb,
        "memory_total_mb": usage.mem_total_mb,
        "cpu_pct": usage.cpu_pct,
    }
    await store_plan(broker, state)
    return state
