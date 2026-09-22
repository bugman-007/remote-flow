"""Celery configuration: three independent queues with priority (CONC-1, PIPE-3)."""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import schedule
from celery.signals import heartbeat_sent, worker_ready

from app.config import get_settings

settings = get_settings()

broker_url = settings.redis_url or os.environ.get("CELERY_BROKER_URL") or "memory://"

celery_app = Celery("remote_flow", broker=broker_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="llm",
    task_queues={
        "llm": {"exchange": "llm", "routing_key": "llm"},
        "render": {"exchange": "render", "routing_key": "render"},
        "ops": {"exchange": "ops", "routing_key": "ops"},
    },
    task_routes={
        "app.workers.tasks.run_llm": {"queue": "llm"},
        "app.workers.tasks.run_render": {"queue": "render"},
        "app.workers.tasks.*_sweep": {"queue": "ops"},
        "app.workers.tasks.*_watchdog": {"queue": "ops"},
        "app.workers.tasks.*_reconcile": {"queue": "ops"},
        "app.workers.tasks.relay_outbox": {"queue": "ops"},
        "app.workers.tasks.run_retention": {"queue": "ops"},
        "app.workers.tasks.run_retention_sweep": {"queue": "ops"},
        "app.workers.tasks.check_disk": {"queue": "ops"},
        "app.workers.tasks.refresh_stats": {"queue": "ops"},
    },
    broker_transport_options={"queue_order_strategy": "priority", "visibility_timeout": 3600},
    task_default_priority=5,
    task_time_limit=settings.llm_timeout_s + 120,
    task_soft_time_limit=settings.llm_timeout_s + 60,
    worker_max_tasks_per_child=200,
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "dispatch-sweep": {"task": "app.workers.tasks.dispatch_sweep", "schedule": schedule(settings.dispatch_sweep_seconds)},
        "lease-watchdog": {"task": "app.workers.tasks.lease_watchdog", "schedule": schedule(settings.lease_watchdog_seconds)},
        "release-reconcile": {"task": "app.workers.tasks.release_reconcile", "schedule": schedule(settings.release_scan_seconds)},
        "relay-outbox": {"task": "app.workers.tasks.relay_outbox", "schedule": schedule(settings.outbox_relay_seconds)},
        "retention-sweep": {"task": "app.workers.tasks.run_retention_sweep", "schedule": schedule(settings.retention_sweep_seconds)},
        "disk-check": {"task": "app.workers.tasks.check_disk", "schedule": schedule(settings.disk_check_seconds)},
        "refresh-stats": {"task": "app.workers.tasks.refresh_stats", "schedule": schedule(settings.stats_refresh_seconds)},
    },
)


DEFAULT_QUEUES = ("llm", "render", "ops")

#: Worker identity captured at boot, reused by the heartbeat signal (which has no Worker).
_worker_identity: str | None = None


def _run_async(factory):
    """Local import: ``app.workers.runtime`` imports ``app.db``/broker, not this module."""
    from app.workers.runtime import run_async

    return run_async(factory)


def _resolve_worker_name(sender) -> str | None:
    """The Celery node name for this process (``-n llm@%h``), or None if not known yet.

    ``worker_ready`` sends the *consumer* as the sender (no hostname) while
    ``heartbeat_sent`` sends the Heart (which knows its worker), so the first
    heartbeat can arrive before the node name is resolvable. Those are skipped:
    the next heartbeat two seconds later records the worker with a stable name,
    so the System status card shows exactly one row per process.
    """
    global _worker_identity
    if _worker_identity:
        return _worker_identity
    env_name = os.environ.get("CELERY_NODE_NAME")
    if env_name:
        _worker_identity = f"worker:{env_name}"
        return _worker_identity
    for candidate in (sender, getattr(sender, "worker", None)):
        hostname = getattr(candidate, "hostname", None)
        if hostname:
            _worker_identity = f"worker:{hostname}"
            return _worker_identity
    return None


def _sender_attr(sender, name: str):
    """Read an attribute from the signal sender, the Heart's worker, or its consumer."""
    candidates = (
        sender,
        getattr(sender, "worker", None),
        getattr(getattr(sender, "worker", None), "consumer", None),
        getattr(sender, "consumer", None),
    )
    for candidate in candidates:
        value = getattr(candidate, name, None)
        if value is not None:
            return value
    return None


def _queues(sender) -> list[str]:
    """The queues this process actually consumes.

    Celery does not expose the consumer on every signal sender, so as a second
    source of truth we read the node name (``-n render@%h`` in deploy/) and, as a
    last resort, report the configured queue names.
    """
    for container in (
        sender,
        getattr(sender, "worker", None),
        getattr(getattr(sender, "worker", None), "consumer", None),
        getattr(sender, "consumer", None),
    ):
        queues = getattr(container, "queues", None)
        if not queues:
            continue
        try:
            names = {getattr(queue, "name", None) or str(queue) for queue in queues.values()}
        except Exception:  # noqa: BLE001
            continue
        names = {name for name in names if name}
        if names:
            return sorted(names)
    node = (os.environ.get("CELERY_NODE_NAME") or (_worker_identity or "").removeprefix("worker:"))
    tokens = {token for token in node.lower().replace("@", "-").split("-") if token}
    found = sorted(queue for queue in DEFAULT_QUEUES if queue in tokens)
    return found or list(DEFAULT_QUEUES)


@worker_ready.connect
def _on_worker_ready(sender=None, **_kwargs) -> None:
    """SET-14: record a heartbeat when the worker boots."""

    name = _resolve_worker_name(sender)
    if name is None:
        return

    def work():
        async def inner():
            from app.db import session_scope
            from app.services import metrics

            async with session_scope() as session:
                await metrics.record_heartbeat(
                    session,
                    name=name,
                    queues=_queues(sender),
                    concurrency=_sender_attr(sender, "concurrency"),
                    pid=os.getpid(),
                    started=True,
                )
                await session.commit()

        return _run_async(inner)

    try:
        work()
    except Exception:  # noqa: BLE001 - heartbeats must never break the worker
        pass


@heartbeat_sent.connect
def _on_heartbeat(sender=None, **_kwargs) -> None:
    name = _resolve_worker_name(sender)
    if name is None:
        return

    def work():
        async def inner():
            from app.db import session_scope
            from app.services import metrics

            async with session_scope() as session:
                await metrics.record_heartbeat(
                    session, name=name, queues=_queues(sender), pid=os.getpid()
                )
                await session.commit()

        return _run_async(inner)

    try:
        work()
    except Exception:  # noqa: BLE001
        pass
