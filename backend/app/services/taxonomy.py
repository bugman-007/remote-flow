"""INT-14: Manager-owned interview steps, status labels and per-step history."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InterviewStatus, InterviewStep, InterviewStepRecord, User

#: Label -> the interview lifecycle enum the rest of the app already understands.
LABEL_TO_LIFECYCLE: dict[str, str] = {
    "Done": "completed",
    "Rejected": "completed",
    "Cancelled": "cancelled",
}
FALLBACK_LIFECYCLE = "scheduled"

SCHEDULED_LABEL = "Scheduled"
DONE_LABEL = "Done"
CANCELLED_LABEL = "Cancelled"
REJECTED_LABEL = "Rejected"


def step_out(row: InterviewStep) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "color": row.color,
        "position": row.position,
        "is_active": row.is_active,
    }


def status_out(row: InterviewStatus) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "color": row.color,
        "position": row.position,
        "is_active": row.is_active,
    }


def lifecycle_for(label: str | None) -> str:
    return LABEL_TO_LIFECYCLE.get(label or "", FALLBACK_LIFECYCLE)


async def status_by_name(session: AsyncSession, name: str) -> InterviewStatus | None:
    return (
        await session.execute(select(InterviewStatus).where(InterviewStatus.name == name))
    ).scalar_one_or_none()


async def apply_label(session: AsyncSession, interview, name: str) -> InterviewStatus | None:
    """Point the interview at ``name`` and mirror it onto the lifecycle enum.

    The lifecycle mirror happens even when the label row is missing (e.g. an
    install that has not seeded yet), so Done/Cancel always behave.
    """
    interview.status = lifecycle_for(name)
    row = await status_by_name(session, name)
    if row is not None:
        interview.status_id = row.id
    return row


async def step_records_out(session: AsyncSession, interview_ids: list[str]) -> dict[str, list[dict]]:
    """Batch-load the per-step history (step + reviewer names) for each interview."""
    if not interview_ids:
        return {}
    records = (
        await session.execute(
            select(InterviewStepRecord)
            .where(InterviewStepRecord.interview_id.in_(interview_ids))
            .order_by(InterviewStepRecord.position, InterviewStepRecord.created_at)
        )
    ).scalars().all()
    step_ids = {record.step_id for record in records if record.step_id}
    reviewer_ids = {record.reviewer_id for record in records if record.reviewer_id}
    steps = {
        row.id: row
        for row in (
            await session.execute(select(InterviewStep).where(InterviewStep.id.in_(step_ids)))
        ).scalars().all()
    } if step_ids else {}
    reviewers = {
        row.id: row
        for row in (
            await session.execute(select(User).where(User.id.in_(reviewer_ids)))
        ).scalars().all()
    } if reviewer_ids else {}
    grouped: dict[str, list[dict]] = {}
    for record in records:
        step = steps.get(record.step_id) if record.step_id else None
        reviewer = reviewers.get(record.reviewer_id) if record.reviewer_id else None
        grouped.setdefault(record.interview_id, []).append(
            {
                "id": record.id,
                "step_id": record.step_id,
                "step_name": step.name if step else None,
                "step_color": step.color if step else None,
                "position": record.position,
                "reviewer_id": record.reviewer_id,
                "reviewer_name": reviewer.name if reviewer else None,
                "done": record.done,
                "rejected": record.rejected,
                "note": record.note,
                "done_at": record.done_at,
                "created_at": record.created_at,
            }
        )
    return grouped


def current_record(records: list[dict]) -> dict | None:
    """The step being worked on: the last one in the ordered history."""
    return records[-1] if records else None
