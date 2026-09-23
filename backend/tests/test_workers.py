"""Worker task registry (CONC-1, PIPE-3).

A consumer that boots without its task modules registers nothing, acknowledges
every message it receives and discards it - the queue then looks "delivered" while
nothing ever runs. Every name below is published by ``CeleryDispatcher.send_task``
or by the beat schedule, so a missing registration silently stalls the pipeline.
"""

from __future__ import annotations

from app.workers.celery_app import TASK_MODULES, celery_app

#: Published by app.services.dispatch.CeleryDispatcher.
DISPATCHED = {"app.workers.tasks.run_llm", "app.workers.tasks.run_render"}


def _beat_tasks() -> set[str]:
    return {entry["task"] for entry in celery_app.conf.beat_schedule.values()}


def test_task_modules_declared():
    assert "app.workers.tasks" in TASK_MODULES


def test_worker_registers_every_published_task():
    import app.workers.tasks  # noqa: F401 - the import the worker must perform

    published = DISPATCHED | _beat_tasks()
    missing = published - set(celery_app.tasks)
    assert not missing, f"worker would acknowledge and discard: {sorted(missing)}"


def test_dispatch_stages_map_to_registered_tasks():
    from app.services.dispatch import CeleryDispatcher

    assert CeleryDispatcher is not None
    assert DISPATCHED <= set(celery_app.conf.task_routes)
