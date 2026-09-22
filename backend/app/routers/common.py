"""Query helpers shared by the routers."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.errors import APIError
from app.models import DocSet, FileArtifact, Generation, GenerationAttempt, Job, User

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class Pagination:
    def __init__(self, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self.page = max(page, 1)
        self.page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
        self.offset = (self.page - 1) * self.page_size

    def as_dict(self, total: int) -> dict:
        return {
            "page": self.page,
            "page_size": self.page_size,
            "total": total,
            "pages": (total + self.page_size - 1) // self.page_size if self.page_size else 0,
        }


def pagination(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> Pagination:
    return Pagination(page, page_size)


def ensure_maker_scope(user: User, job: Job) -> None:
    if user.role == "maker" and job.maker_id != user.id:
        raise APIError("not_found", "Doc set not found.", status_code=404)


async def get_job(session: AsyncSession, job_id: str) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise APIError("not_found", "Job not found.", status_code=404)
    return job


async def get_doc_set(session: AsyncSession, doc_set_id: str) -> DocSet:
    doc_set = await session.get(DocSet, doc_set_id)
    if doc_set is None:
        raise APIError("not_found", "Doc set not found.", status_code=404)
    return doc_set


async def doc_set_job(session: AsyncSession, doc_set: DocSet) -> Job:
    return await get_job(session, doc_set.job_id)


async def files_for_generation(session: AsyncSession, generation_id: str) -> list[FileArtifact]:
    return (
        await session.execute(
            select(FileArtifact)
            .where(FileArtifact.generation_id == generation_id)
            .order_by(FileArtifact.kind)
        )
    ).scalars().all()


async def generations_for_job(session: AsyncSession, job_id: str) -> list[Generation]:
    return (
        await session.execute(
            select(Generation).where(Generation.job_id == job_id).order_by(Generation.generation_no)
        )
    ).scalars().all()


async def attempts_for_generation(session: AsyncSession, generation_id: str) -> list[GenerationAttempt]:
    return (
        await session.execute(
            select(GenerationAttempt)
            .where(GenerationAttempt.generation_id == generation_id)
            .order_by(GenerationAttempt.started_at, GenerationAttempt.attempt_no)
        )
    ).scalars().all()


async def visible_generation(session: AsyncSession, doc_set: DocSet, user: User, generation_id: str | None) -> Generation | None:
    """Makers only ever see the current generation (RES-13)."""
    if generation_id is None:
        if doc_set.current_generation_id is None:
            return None
        generation = await session.get(Generation, doc_set.current_generation_id)
    else:
        generation = await session.get(Generation, generation_id)
    if generation is None or generation.job_id != doc_set.job_id:
        return None
    if user.role == "maker" and generation.id != doc_set.current_generation_id:
        raise APIError("not_found", "Generation not found.", status_code=404)
    return generation


CurrentUser = Depends(current_user)
SessionDep = Depends(get_session)
