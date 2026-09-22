"""Per-Maker statistics (RES-16, daily_maker_stats refresh)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyMakerStat, DocSet, Generation, GenerationAttempt, Job, User


async def refresh_daily_stats(session: AsyncSession, *, days: int = 3, today: date | None = None) -> int:
    today = today or datetime.utcnow().date()
    refreshed = 0
    for offset in range(days):
        day = today - timedelta(days=offset)
        jobs = (await session.execute(select(Job).where(Job.submitted_date == day))).scalars().all()
        by_maker: dict[str, list[Job]] = {}
        for job in jobs:
            by_maker.setdefault(job.maker_id, []).append(job)
        for maker_id, maker_jobs in by_maker.items():
            ready_ms: list[int] = []
            attempts_total = 0
            tokens_total = 0
            selected = 0
            for job in maker_jobs:
                doc_set = (await session.execute(select(DocSet).where(DocSet.job_id == job.id))).scalar_one_or_none()
                if doc_set and doc_set.is_selected:
                    selected += 1
                generations = (
                    await session.execute(select(Generation).where(Generation.job_id == job.id))
                ).scalars().all()
                initial = next((g for g in generations if g.kind == "initial"), None)
                if initial is not None:
                    attempts_total += initial.llm_attempts
                    if initial.ready_at and job.submitted_at:
                        ready_ms.append(int((initial.ready_at - job.submitted_at).total_seconds() * 1000))
                    for attempt in (
                        await session.execute(
                            select(GenerationAttempt).where(GenerationAttempt.generation_id == initial.id)
                        )
                    ).scalars().all():
                        tokens_total += (attempt.tokens_in or 0) + (attempt.tokens_out or 0)
            existing = (
                await session.execute(
                    select(DailyMakerStat).where(DailyMakerStat.maker_id == maker_id, DailyMakerStat.date == day)
                )
            ).scalar_one_or_none()
            values = {
                "submitted": len(maker_jobs),
                "ready": sum(1 for job in maker_jobs if job.delivery_status == "released"),
                "skipped": sum(1 for job in maker_jobs if job.delivery_status == "skipped"),
                "selected": selected,
                "avg_ready_ms": int(sum(ready_ms) / len(ready_ms)) if ready_ms else None,
                "avg_llm_attempts": (attempts_total / len(maker_jobs)) if maker_jobs else None,
                "tokens": tokens_total,
            }
            if existing is None:
                session.add(DailyMakerStat(maker_id=maker_id, date=day, **values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            refreshed += 1
    await session.flush()
    return refreshed


async def maker_stats(session: AsyncSession, *, date_from: date, date_to: date, maker_id: str | None = None) -> list[dict]:
    query = select(DailyMakerStat).where(DailyMakerStat.date >= date_from, DailyMakerStat.date <= date_to)
    if maker_id:
        query = query.where(DailyMakerStat.maker_id == maker_id)
    rows = (await session.execute(query)).scalars().all()
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(
            row.maker_id,
            {"maker_id": row.maker_id, "submitted": 0, "ready": 0, "skipped": 0, "selected": 0,
             "tokens": 0, "avg_ready_ms": None, "avg_llm_attempts": None, "_ready_samples": []},
        )
        entry["submitted"] += row.submitted
        entry["ready"] += row.ready
        entry["skipped"] += row.skipped
        entry["selected"] += row.selected
        entry["tokens"] += row.tokens
        if row.avg_ready_ms is not None:
            entry["_ready_samples"].append(row.avg_ready_ms)
        if row.avg_llm_attempts is not None:
            entry["avg_llm_attempts"] = row.avg_llm_attempts
    out = []
    for entry in totals.values():
        samples = entry.pop("_ready_samples")
        entry["avg_ready_ms"] = int(sum(samples) / len(samples)) if samples else None
        entry["success_rate"] = round(entry["selected"] / entry["ready"] * 100, 1) if entry["ready"] else None
        maker = await session.get(User, entry["maker_id"])
        entry["maker_name"] = maker.name if maker else ""
        entry["maker_email"] = maker.email if maker else ""
        out.append(entry)
    return out
