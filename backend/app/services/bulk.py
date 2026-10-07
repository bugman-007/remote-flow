"""Bulk Resumes (BULK-1): a CSV of job links + JDs generated for many Profiles.

The Manager uploads a CSV with ``Job Links`` and ``JD`` columns, picks a group or
some Profiles and a number N. Every chosen Profile gets resumes for the first N
usable CSV rows, in CSV order:

* rows without a link or a JD (or with an invalid link / JD length) are left out
  at upload time;
* a row whose JD or link already appeared earlier in the CSV is a duplicate;
* a row the Profile (or its Maker) already has from the last 30 days is a
  duplicate for that Profile;
* duplicates are skipped and the next rows fill up to N.

Each job belongs to the Profile and is submitted for the Maker assigned to it,
through the normal intake (sequence numbers, ordered release, the pipeline), but
never against the Maker's daily limit. A Profile with no Maker gets nothing.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BulkBatch, Job, Profile, ProfileAssignment, User
from app.services import intake, settings_store
from app.utils import sha256_text, utcnow

#: Accepted header spellings (case and surrounding spaces ignored).
LINK_HEADERS = ("job links", "job link", "joblinks", "joblink")
JD_HEADERS = ("jd", "job description", "job descriptions")
MAX_CSV_BYTES = 25 * 1024 * 1024
DUPLICATE_WINDOW_DAYS = 30


class BulkError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _columns(header: list[str]) -> tuple[int, int] | None:
    names = [cell.strip().lower() for cell in header]
    link = next((index for index, name in enumerate(names) if name in LINK_HEADERS), None)
    jd = next((index for index, name in enumerate(names) if name in JD_HEADERS), None)
    return (link, jd) if link is not None and jd is not None else None


def valid_link(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def link_key(value: str) -> str:
    return value.strip().rstrip("/").lower()


def parse_csv(data: bytes, *, min_chars: int, max_chars: int) -> tuple[list[dict], list[dict]]:
    """Return ``(rows, rejected)``; ``row`` is the spreadsheet row number (header = 1)."""
    if len(data) > MAX_CSV_BYTES:
        raise BulkError("csv_too_large", "The CSV file is larger than 25 MB.", status_code=413)
    text = _decode(data)
    csv.field_size_limit(max(max_chars * 4, 1_000_000))
    reader = None
    columns = None
    for delimiter in (",", ";", "\t"):
        candidate = csv.reader(io.StringIO(text), delimiter=delimiter)
        header = next(candidate, None)
        if header and (found := _columns(header)) is not None:
            reader, columns = candidate, found
            break
    if reader is None or columns is None:
        raise BulkError("missing_columns", 'The CSV must have a "Job Links" column and a "JD" column.')
    link_index, jd_index = columns

    rows: list[dict] = []
    rejected: list[dict] = []
    for number, cells in enumerate(reader, start=2):
        link = cells[link_index].strip() if len(cells) > link_index else ""
        jd = intake.normalise_jd(cells[jd_index]) if len(cells) > jd_index else ""
        if not link and not jd:
            continue  # blank line
        reason = None
        if not link:
            reason = "missing job link"
        elif not jd:
            reason = "missing JD"
        elif not valid_link(link):
            reason = "invalid job link"
        elif len(jd) < min_chars:
            reason = f"JD shorter than {min_chars} characters"
        elif len(jd) > max_chars:
            reason = f"JD longer than {max_chars} characters"
        if reason:
            rejected.append({"row": number, "reason": reason})
        else:
            rows.append({"row": number, "job_link": link, "jd": jd})
    if not rows and not rejected:
        raise BulkError("empty_csv", "The CSV has no rows.")
    return rows, rejected


async def create_batch(session: AsyncSession, *, data: bytes, filename: str | None, actor_id: str | None) -> BulkBatch:
    settings = await settings_store.all_settings(session)
    rows, rejected = parse_csv(data, min_chars=int(settings["min_jd_chars"]), max_chars=int(settings["max_jd_chars"]))
    batch = BulkBatch(created_by=actor_id, filename=(filename or "")[:300] or None, rows=rows, rejected=rejected)
    session.add(batch)
    await session.flush()
    return batch


def batch_out(batch: BulkBatch) -> dict:
    return {
        "id": batch.id,
        "filename": batch.filename,
        "created_at": batch.created_at,
        "generated_at": batch.generated_at,
        "usable_rows": len(batch.rows or []),
        "rejected_rows": len(batch.rejected or []),
        "rejected": (batch.rejected or [])[:100],
        "summary": batch.summary,
    }


@dataclass
class _Row:
    number: int
    link: str
    jd: str
    jd_hash: str
    link_key: str


def unique_rows(rows: list[dict]) -> tuple[list[_Row], int]:
    """CSV rows in order with in-file duplicates (same JD or same link) removed."""
    seen_hashes: set[str] = set()
    seen_links: set[str] = set()
    out: list[_Row] = []
    duplicates = 0
    for row in rows:
        jd_hash = sha256_text(row["jd"])
        key = link_key(row["job_link"])
        if jd_hash in seen_hashes or key in seen_links:
            duplicates += 1
            continue
        seen_hashes.add(jd_hash)
        seen_links.add(key)
        out.append(_Row(int(row["row"]), row["job_link"], row["jd"], jd_hash, key))
    return out, duplicates


async def profile_maker(session: AsyncSession, profile: Profile) -> User | None:
    """The Maker the Profile is assigned to now (the latest assignment)."""
    assignment = (
        await session.execute(
            select(ProfileAssignment)
            .where(ProfileAssignment.profile_id == profile.id, ProfileAssignment.ended_at.is_(None))
            .order_by(ProfileAssignment.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if assignment is None:
        return None
    maker = await session.get(User, assignment.maker_id)
    return maker if maker is not None and maker.is_active and maker.role == "maker" else None


async def _history(session: AsyncSession, profile: Profile, maker: User) -> tuple[set[str], set[str]]:
    cutoff = utcnow() - timedelta(days=DUPLICATE_WINDOW_DAYS)
    rows = (
        await session.execute(
            select(Job.jd_hash, Job.job_link).where(
                or_(Job.profile_id == profile.id, Job.maker_id == maker.id),
                Job.submitted_at >= cutoff,
                Job.delivery_status != "skipped",
            )
        )
    ).all()
    return {row[0] for row in rows}, {link_key(row[1]) for row in rows if row[1]}


async def generate(
    session: AsyncSession, batch: BulkBatch, *, profiles: list[Profile], count: int
) -> dict:
    """Submit the resumes; returns the summary shown to the Manager (and kept on the batch)."""
    if batch.generated_at is not None:
        raise BulkError("already_generated", "Resumes were already generated from this upload.", status_code=409)
    rows, csv_duplicates = unique_rows(batch.rows or [])
    per_profile: list[dict] = []
    skipped_profiles: list[dict] = []
    for profile in profiles:
        if profile.status != "active":
            skipped_profiles.append({"profile_id": profile.id, "profile_name": profile.name, "reason": "archived"})
            continue
        maker = await profile_maker(session, profile)
        if maker is None:
            skipped_profiles.append(
                {"profile_id": profile.id, "profile_name": profile.name, "reason": "no maker assigned"}
            )
            continue
        known_hashes, known_links = await _history(session, profile, maker)
        created = duplicates = 0
        for row in rows:
            if created >= count:
                break
            if row.jd_hash in known_hashes or row.link_key in known_links:
                duplicates += 1
                continue
            await intake.submit_jd(
                session,
                maker=maker,
                jd_text=row.jd,
                idempotency_key=f"bulk:{batch.id}:{profile.id}:{row.number}",
                source="bulk",
                job_link=row.link,
                bulk_batch_id=batch.id,
            )
            known_hashes.add(row.jd_hash)
            known_links.add(row.link_key)
            created += 1
        per_profile.append(
            {
                "profile_id": profile.id,
                "profile_name": profile.name,
                "maker_id": maker.id,
                "maker_name": maker.name,
                "created": created,
                "skipped_duplicates": duplicates,
                "short_by": max(0, count - created),
            }
        )
    summary = {
        "count": count,
        "created": sum(item["created"] for item in per_profile),
        "profiles": per_profile,
        "skipped_profiles": skipped_profiles,
        "csv_duplicates": csv_duplicates,
        "rejected_rows": len(batch.rejected or []),
    }
    batch.generated_at = utcnow()
    batch.summary = summary
    await session.flush()
    return summary
