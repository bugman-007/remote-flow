"""Permanent deletion of doc sets (owner request, 2026-09-23).

Retention (STO-4) only expires *files* and never removes database rows. This module
is the explicit Manager action that removes a submission completely: the generated
documents on disk, the pipeline history and every row that describes them.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DocSet,
    FileArtifact,
    Generation,
    GenerationAttempt,
    Interview,
    Job,
    PipelineEvent,
)
from app.services import storage

logger = logging.getLogger(__name__)

#: A build in one of these states belongs to a running worker. Deleting it under the
#: worker's feet would let a late lease write rows and files straight back.
BUSY_STATUSES = ("queued", "llm_running", "rendering", "retry_wait")

#: Reasons a doc set is refused, with the code the API returns for it.
REFUSAL_CODES = {
    "in_flight": "docset_in_flight",
    "has_interviews": "docset_has_interviews",
    "not_found": "not_found",
}


def remove_tree(relative: str | None) -> int:
    """Delete one storage folder, refusing anything outside the storage root."""
    if not relative:
        return 0
    root = storage.storage_root()
    try:
        target = storage.safe_relative(relative, root=root)
    except ValueError:
        logger.warning("refusing to delete a path outside storage", extra={"path": relative})
        return 0
    if target == root.resolve() or not target.exists():
        return 0
    size = storage.directory_size(target)
    shutil.rmtree(target, ignore_errors=True)
    # Leave no empty ``<date>/`` (or ``makers/<id>/``) folders behind: rmdir only
    # succeeds while the folder is empty, so a shared date stops the walk.
    root = root.resolve()
    parent = target.parent
    while parent != root and str(parent).startswith(str(root)):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return size


async def purge_doc_sets(session: AsyncSession, doc_set_ids: list[str]) -> dict:
    """Delete doc sets for good. Reports what was removed and what was refused."""
    deleted: list[str] = []
    blocked: list[dict] = []
    freed_bytes = 0
    maker_ids: list[str] = []

    for doc_set_id in dict.fromkeys(doc_set_ids):
        doc_set = await session.get(DocSet, doc_set_id)
        if doc_set is None:
            blocked.append({"doc_set_id": doc_set_id, "reason": "not_found"})
            continue
        job = await session.get(Job, doc_set.job_id)
        generations = (
            await session.execute(select(Generation).where(Generation.job_id == doc_set.job_id))
        ).scalars().all()
        generation_ids = [generation.id for generation in generations]

        busy = next((generation.status for generation in generations if generation.status in BUSY_STATUSES), None)
        if busy is not None:
            blocked.append({"doc_set_id": doc_set.id, "reason": "in_flight", "status": busy})
            continue

        interviews = (
            await session.execute(
                select(Interview.id).where(
                    or_(
                        Interview.doc_set_id == doc_set.id,
                        Interview.generation_id.in_(generation_ids or [""]),
                    )
                )
            )
        ).scalars().all()
        if interviews:
            blocked.append(
                {"doc_set_id": doc_set.id, "reason": "has_interviews", "interview_count": len(interviews)}
            )
            continue

        # 1) the files: the doc set folder holds jd.txt, attempts/ and gen-XX/.
        freed_bytes += remove_tree(doc_set.storage_dir)
        for generation in generations:
            if generation.storage_dir and generation.storage_dir != doc_set.storage_dir:
                freed_bytes += remove_tree(generation.storage_dir)

        # 2) the rows. ``jobs.duplicate_of`` points at sibling jobs, so detach those
        # first; everything else hangs off the job or the generation.
        await session.execute(
            update(Job).where(Job.duplicate_of == doc_set.job_id).values(duplicate_of=None)
        )
        await session.execute(
            delete(FileArtifact).where(
                or_(
                    FileArtifact.doc_set_id == doc_set.id,
                    FileArtifact.generation_id.in_(generation_ids or [""]),
                )
            )
        )
        await session.execute(
            delete(GenerationAttempt).where(GenerationAttempt.generation_id.in_(generation_ids or [""]))
        )
        await session.execute(
            delete(PipelineEvent).where(
                or_(
                    PipelineEvent.job_id == doc_set.job_id,
                    PipelineEvent.generation_id.in_(generation_ids or [""]),
                )
            )
        )
        await session.execute(delete(Generation).where(Generation.job_id == doc_set.job_id))
        await session.execute(delete(DocSet).where(DocSet.id == doc_set.id))
        await session.execute(delete(Job).where(Job.id == doc_set.job_id))
        deleted.append(doc_set.id)
        if job is not None:
            maker_ids.append(job.maker_id)

    await session.flush()
    return {"deleted": deleted, "blocked": blocked, "bytes": freed_bytes, "maker_ids": maker_ids}
