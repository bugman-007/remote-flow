"""JD intake: one transaction under the Maker's row lock (PIPE-11)."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import acquire_serialization_lock, is_postgres
from app.models import DocSet, FileArtifact, Job, Profile, ProfileAssignment, User
from app.services import events, settings_store, snapshots, storage
from app.services.snapshots import SnapshotError
from app.utils import sha256_text, utcnow
from app.services.search import attach_jd_tsv

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_DAYS = 7


class IntakeError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def server_today(session: AsyncSession) -> date:
    tz_name = await settings_store.get_setting(session, "timezone") or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return utcnow().astimezone(tz).date()


async def active_assignment(session: AsyncSession, maker_id: str) -> ProfileAssignment | None:
    return (
        await session.execute(
            select(ProfileAssignment)
            .where(ProfileAssignment.maker_id == maker_id, ProfileAssignment.ended_at.is_(None))
            .order_by(ProfileAssignment.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def usage_today(session: AsyncSession, maker_id: str, day: date) -> int:
    count = (
        await session.execute(
            select(func.count(Job.id)).where(Job.maker_id == maker_id, Job.submitted_date == day)
        )
    ).scalar_one()
    return int(count or 0)


def normalise_jd(text: str) -> str:
    return "\r\n".join(line.rstrip() for line in (text or "").replace("\r\n", "\n").split("\n")).strip()


async def submit_jd(
    session: AsyncSession,
    *,
    maker: User,
    jd_text: str,
    idempotency_key: str | None,
) -> tuple[Job, bool]:
    """Returns ``(job, created)``. Raises :class:`IntakeError` on rejection."""
    text = normalise_jd(jd_text)
    settings = await settings_store.all_settings(session)
    if len(text) < int(settings["min_jd_chars"]):
        raise IntakeError("invalid_jd", f"Job description must be at least {settings['min_jd_chars']} characters.", status_code=422)
    if len(text) > int(settings["max_jd_chars"]):
        raise IntakeError("invalid_jd", f"Job description must be at most {settings['max_jd_chars']} characters.", status_code=422)

    if idempotency_key:
        existing = (
            await session.execute(
                select(Job).where(Job.maker_id == maker.id, Job.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    paused, reason = await settings_store.intake_paused(session)
    if paused:
        raise IntakeError("intake_paused", reason or "Submissions are paused for maintenance.", status_code=503)

    # PIPE-11: serialise this Maker for the whole transaction (row lock on
    # Postgres, transaction-scoped in-process lock elsewhere).
    use_row_lock = is_postgres(session.get_bind())
    if use_row_lock:
        await session.execute(select(User.id).where(User.id == maker.id).with_for_update())
    else:
        await acquire_serialization_lock(session, f"intake:{maker.id}")
    assignment = await active_assignment(session, maker.id)
    if assignment is None:
        raise IntakeError("no_profile_assigned", "Your account is not yet configured. Contact your manager.", status_code=409)
    profile = await session.get(Profile, assignment.profile_id)
    if profile is None or profile.status != "active":
        raise IntakeError("no_profile_assigned", "Your account is not yet configured. Contact your manager.", status_code=409)

    day = await server_today(session)
    if maker.daily_limit is not None:
        used = await usage_today(session, maker.id, day)
        if used >= maker.daily_limit:
            raise IntakeError("limit_reached", "You have reached your daily submission limit.", status_code=429)

    seq_no = int(
        (
            await session.execute(
                select(func.max(Job.seq_no)).where(Job.maker_id == maker.id, Job.submitted_date == day)
            )
        ).scalar_one()
        or 0
    ) + 1
    jd_hash = sha256_text(text)
    cutoff = utcnow() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    duplicate = (
        await session.execute(
            select(Job.id)
            .where(Job.maker_id == maker.id, Job.jd_hash == jd_hash, Job.submitted_at >= cutoff)
            .order_by(Job.submitted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    job = Job(
        maker_id=maker.id,
        profile_id=profile.id,
        seq_no=seq_no,
        submitted_date=day,
        submitted_at=utcnow(),
        idempotency_key=idempotency_key,
        jd_text=text,
        jd_hash=jd_hash,
        duplicate_of=duplicate,
        delivery_status="pending",
    )
    session.add(job)
    await session.flush()

    doc_set = DocSet(job_id=job.id)
    session.add(doc_set)
    await session.flush()

    try:
        generation = await snapshots.create_generation(
            session, job=job, profile=profile, kind="initial", created_by=None
        )
    except SnapshotError as exc:
        raise IntakeError("no_provider_configured", str(exc), status_code=409) from exc

    directory = storage.doc_set_dir(maker.id, day, seq_no, "pending")
    storage.ensure_dir(directory)
    jd_file = storage.write_text(directory / "jd.txt", text)
    doc_set.storage_dir = storage.relative_to_root(directory)
    session.add(
        FileArtifact(
            generation_id=None,
            doc_set_id=doc_set.id,
            kind="jd",
            path=storage.relative_to_root(jd_file.path),
            filename="jd.txt",
            size_bytes=jd_file.size_bytes,
            sha256=jd_file.sha256,
        )
    )
    await attach_jd_tsv(session, job.id, text)

    from app.services.dispatch import enqueue_after_commit

    enqueue_after_commit(session, generation.id, "llm", high_priority=False)
    await events.emit(
        session,
        audience=events.job_audience(maker.id),
        event_type="job.status",
        payload={
            "job_id": job.id,
            "seq_no": seq_no,
            "status": "queued",
            "generation_id": generation.id,
            "duplicate_of": duplicate,
        },
        job_id=job.id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": None,
            "to_state": "pending",
            "stage": "intake",
            "actor": maker.id,
            "details": {"seq_no": seq_no, "duplicate_of": duplicate},
        },
    )
    await session.flush()
    return job, True
