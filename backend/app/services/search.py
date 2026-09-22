"""Search helpers: Postgres full-text with a portable fallback (RES-6)."""

from __future__ import annotations

from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import is_postgres


def search_text(value: str | None) -> str:
    """Stored in ``*_tsv``: Postgres gets the real tsvector, others plain text."""
    return (value or "").lower()


def _seq_candidates(query: str) -> list[int]:
    cleaned = query.strip().lstrip("#")
    return [int(cleaned)] if cleaned.isdigit() else []


def job_text_search_clause(model, query: str) -> ColumnElement[bool]:
    pattern = f"%{query.strip()}%"
    clauses = [model.jd_text.ilike(pattern), model.jd_tsv.ilike(pattern)]
    return or_(*clauses)


async def attach_jd_tsv(session: AsyncSession, job_id: str, jd_text: str) -> None:
    if is_postgres(session.get_bind()):
        await session.execute(
            text("UPDATE jobs SET jd_tsv = to_tsvector('english', :text) WHERE id = :id"),
            {"text": jd_text, "id": job_id},
        )
    else:
        await session.execute(
            text("UPDATE jobs SET jd_tsv = :tsv WHERE id = :id"),
            {"tsv": search_text(jd_text), "id": job_id},
        )


async def attach_docset_search(session: AsyncSession, doc_set_id: str, text_value: str) -> None:
    await session.execute(
        text("UPDATE doc_sets SET search_tsv = :tsv WHERE id = :id"),
        {"tsv": search_text(text_value), "id": doc_set_id},
    )


def reindex_statements() -> list[tuple[str, str]]:
    """Statements for ``manage.py reindex-search``."""
    return [
        ("jobs", "UPDATE jobs SET jd_tsv = to_tsvector('english', coalesce(jd_text,''))"),
        (
            "doc_sets",
            "UPDATE doc_sets SET search_tsv = lower(concat_ws(' ', coalesce(company_name,''), "
            "coalesce(job_title,''), coalesce(candidate_name,'')))",
        ),
    ]
