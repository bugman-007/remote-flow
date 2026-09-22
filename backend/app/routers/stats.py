"""Stats endpoints (RES-16, SET-6, PRO-10)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import manager_required
from app.models import GenerationAttempt, Job, LLMProvider, Profile, User
from app.services import stats as stats_service
from app.utils import utcnow

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/makers")
async def maker_stats(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    maker_id: str | None = None,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    from app.models import DailyMakerStat

    today = date.today()
    window_from = date_from or today - timedelta(days=7)
    window_to = date_to or today
    if window_from <= today <= window_to:
        # The ops worker refreshes the daily aggregates every STATS_REFRESH_SECONDS.
        # If it has not run yet today (fresh install, ops worker down), build the
        # row now so the page is not empty; at most one extra refresh per day.
        has_today = (
            await session.execute(
                select(func.count(DailyMakerStat.id)).where(DailyMakerStat.date == today)
            )
        ).scalar_one()
        if not has_today:
            await stats_service.refresh_daily_stats(session, days=1, today=today)
            await session.commit()
    return {
        "items": await stats_service.maker_stats(
            session,
            date_from=window_from,
            date_to=window_to,
            maker_id=maker_id,
        )
    }


@router.get("/profiles")
async def profile_stats(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(
            select(
                Profile.id,
                Profile.name,
                func.count(Job.id).label("submitted"),
                func.count(func.nullif(Job.delivery_status, "pending")).label("ready_or_skipped"),
            )
            .outerjoin(Job, Job.profile_id == Profile.id)
            .group_by(Profile.id, Profile.name)
            .order_by(Profile.name)
        )
    ).all()
    return {
        "items": [
            {"profile_id": row.id, "name": row.name, "submitted": row.submitted, "ready": row.ready_or_skipped}
            for row in rows
        ]
    }


@router.get("/providers")
async def provider_stats(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    since = utcnow() - timedelta(days=1)
    rows = (
        await session.execute(
            select(
                LLMProvider.id,
                LLMProvider.display_name,
                func.count(GenerationAttempt.id).label("requests"),
                func.avg(GenerationAttempt.latency_ms).label("avg_latency_ms"),
                func.sum(func.coalesce(GenerationAttempt.tokens_in, 0) + func.coalesce(GenerationAttempt.tokens_out, 0)).label("tokens"),
                func.sum(case((GenerationAttempt.outcome != "succeeded", 1), else_=0)).label("errors"),
            )
            # SET-13 labels this card "Usage (24 h)", so the window is part of the
            # outer join condition: providers without attempts must still be listed.
            .outerjoin(
                GenerationAttempt,
                (GenerationAttempt.provider_id == LLMProvider.id)
                & (GenerationAttempt.started_at >= since),
            )
            .group_by(LLMProvider.id, LLMProvider.display_name)
        )
    ).all()
    return {
        "items": [
            {
                "provider_id": row.id,
                "name": row.display_name,
                "requests": row.requests or 0,
                "avg_latency_ms": int(row.avg_latency_ms) if row.avg_latency_ms else None,
                "tokens": int(row.tokens or 0),
                "errors": int(row.errors or 0),
            }
            for row in rows
        ]
    }
