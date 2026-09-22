"""Health, readiness and metrics (OPS, NFR-9)."""

from __future__ import annotations

import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_session
from app.models import EventOutbox, Generation, GenerationAttempt, Job, User
from app.services import storage
from app.services.broker import get_broker
from app.utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok", "version": get_settings().app_version}


@router.get("/readyz")
async def readyz(session: AsyncSession = Depends(get_session)):
    from sqlalchemy import text

    await session.execute(text("SELECT 1"))
    broker_ok = await get_broker().health()
    return {"status": "ok" if broker_ok else "degraded", "database": True, "broker": broker_ok}


@router.get("/metrics")
async def metrics(session: AsyncSession = Depends(get_session)):
    hour_ago = utcnow() - timedelta(hours=1)
    queues = {
        "llm": (
            await session.execute(
                select(func.count(Generation.id)).where(
                    Generation.status.in_(("queued", "llm_running", "retry_wait")), Generation.stage == "llm"
                )
            )
        ).scalar_one(),
        "render": (
            await session.execute(
                select(func.count(Generation.id)).where(
                    Generation.status.in_(("rendering", "retry_wait")), Generation.stage == "render"
                )
            )
        ).scalar_one(),
    }
    needs_attention = (
        await session.execute(select(func.count(Generation.id)).where(Generation.status == "needs_attention"))
    ).scalar_one()
    expired_leases = (
        await session.execute(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.outcome == "timed_out", GenerationAttempt.finished_at >= hour_ago
            )
        )
    ).scalar_one()
    outbox_backlog = (
        await session.execute(select(func.count(EventOutbox.id)).where(EventOutbox.published_at.is_(None)))
    ).scalar_one()
    pending_jobs = (
        await session.execute(select(func.count(Job.id)).where(Job.delivery_status == "pending"))
    ).scalar_one()
    lines = [
        "# HELP remote_flow_queue_depth Pending builds per stage",
        "# TYPE remote_flow_queue_depth gauge",
        f'remote_flow_queue_depth{{stage="llm"}} {queues["llm"]}',
        f'remote_flow_queue_depth{{stage="render"}} {queues["render"]}',
        "remote_flow_builds_needing_attention " + str(needs_attention),
        "remote_flow_expired_leases_last_hour " + str(expired_leases),
        "remote_flow_outbox_backlog " + str(outbox_backlog),
        "remote_flow_pending_jobs " + str(pending_jobs),
        "remote_flow_disk_usage_percent " + str(storage.disk_usage_pct()),
    ]
    total, used, free = shutil.disk_usage(str(storage.storage_root()))
    lines.append("remote_flow_disk_free_bytes " + str(free))
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
