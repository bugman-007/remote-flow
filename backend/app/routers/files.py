"""Authenticated file downloads and ZIP streaming (SEC-3, RES-5, STO-3)."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, current_user
from app.errors import APIError
from app.models import AuditLog, DocSet, FileArtifact, Generation, Interview, Job, User
from app.services import storage

router = APIRouter(tags=["files"])

INLINE_KINDS = {"pdf", "txt"}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
    "llm_json": "application/json",
    "meta": "application/json",
    "jd": "text/plain; charset=utf-8",
}


async def authorize_file(session: AsyncSession, user: User, artifact: FileArtifact) -> None:
    if user.role == "manager":
        return
    if artifact.doc_set_id is None:
        raise APIError("forbidden", "You cannot access this file.", status_code=403)
    doc_set = await session.get(DocSet, artifact.doc_set_id)
    if doc_set is None:
        raise APIError("not_found", "File not found.", status_code=404)
    job = await session.get(Job, doc_set.job_id)
    if user.role == "maker":
        if job is None or job.maker_id != user.id:
            raise APIError("not_found", "File not found.", status_code=404)
        if artifact.generation_id is not None and artifact.generation_id != doc_set.current_generation_id:
            raise APIError("not_found", "File not found.", status_code=404)
        return
    if user.role == "reviewer":
        pinned = (
            await session.execute(
                select(Interview.id).where(
                    Interview.doc_set_id == doc_set.id,
                    Interview.reviewer_id == user.id,
                    Interview.generation_id == artifact.generation_id,
                )
            )
        ).first()
        if pinned is None:
            raise APIError("not_found", "File not found.", status_code=404)
        return
    raise APIError("forbidden", "You cannot access this file.", status_code=403)


def resolve_path(artifact: FileArtifact) -> Path:
    path = storage.safe_relative(artifact.path)
    if not path.exists():
        raise APIError("file_expired", "This file is no longer available.", status_code=410)
    return path


@router.get("/files/{file_id}")
async def download_file(
    file_id: str,
    request: Request,
    inline: int = Query(default=0),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    artifact = await session.get(FileArtifact, file_id)
    if artifact is None:
        raise APIError("not_found", "File not found.", status_code=404)
    await authorize_file(session, user, artifact)
    path = resolve_path(artifact)
    if artifact.expired_at is not None:
        raise APIError("file_expired", "This file is no longer available.", status_code=410)
    # SEC-7: every download of a generated document is audited.
    session.add(
        AuditLog(
            actor_id=user.id,
            action="file.download",
            entity_type="file",
            entity_id=artifact.id,
            after={
                "kind": artifact.kind,
                "filename": artifact.filename,
                "doc_set_id": artifact.doc_set_id,
                "inline": bool(inline),
            },
            ip=client_ip(request),
        )
    )
    await session.commit()
    disposition = "inline" if (inline and artifact.kind in INLINE_KINDS) else "attachment"
    filename = urllib.parse.quote(artifact.filename)
    return FileResponse(
        str(path),
        media_type=MIME.get(artifact.kind, "application/octet-stream"),
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


async def build_zip(
    session: AsyncSession,
    user: User,
    doc_sets: list[DocSet],
    *,
    generation_id: str | None = None,
    ip: str | None = None,
) -> tuple[StreamingResponse, int]:
    entries: list[tuple[Path, str]] = []
    skipped = 0
    for doc_set in doc_sets:
        job = await session.get(Job, doc_set.job_id)
        if job is None:
            skipped += 1
            continue
        chosen_id = generation_id or doc_set.current_generation_id
        generation = await session.get(Generation, chosen_id) if chosen_id else None
        if generation is None or generation.status != "ready" or generation.job_id != job.id:
            skipped += 1
            continue
        if user.role == "maker" and job.maker_id != user.id:
            skipped += 1
            continue
        files = (
            await session.execute(
                select(FileArtifact).where(
                    FileArtifact.generation_id == generation.id,
                    FileArtifact.kind.in_(("pdf", "docx", "txt")),
                )
            )
        ).scalars().all()
        if len(files) < 3:
            skipped += 1
            continue
        folder = storage.doc_set_zip_folder(job.submitted_at, doc_set.company_name, doc_set.job_title)
        for artifact in files:
            try:
                entries.append((storage.safe_relative(artifact.path), f"{folder}/{artifact.filename}"))
            except ValueError:
                continue
    if not entries:
        raise APIError("nothing_to_download", "No ready doc sets matched the selection.", status_code=409)
    # SEC-7: record which doc sets were downloaded, even when some were skipped.
    for doc_set in doc_sets:
        session.add(
            AuditLog(
                actor_id=user.id,
                action="docset.download",
                entity_type="doc_set",
                entity_id=doc_set.id,
                after={"files": len(entries), "skipped": skipped, "generation_id": generation_id},
                ip=ip,
            )
        )
    await session.commit()
    buffer = await storage.zip_entries_async(entries)
    headers = {
        "X-Zip-Included": str(len({entry[1].split("/")[0] for entry in entries})),
        "X-Zip-Skipped": str(skipped),
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(buffer, media_type="application/zip", headers=headers), skipped
