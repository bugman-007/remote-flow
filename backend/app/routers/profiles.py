"""Profiles, prompt versions and Maker assignments (PRO-1…PRO-10)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, manager_required
from app.errors import APIError
from app.models import (
    AuditLog,
    DocSet,
    Job,
    Profile,
    ProfileAssignment,
    PromptVersion,
    LLMProvider,
    Theme,
    User,
)
from app.schemas import AssignmentRequest, ProfileRequest, ProfileUpdate, PromptVersionRequest, TestPromptRequest
from app.serializers import profile_out, prompt_version_out
from app.services import events, intake, render, snapshots, storage
from app.services.derived import doc_set_summary
from app.services.llm import (
    LLMError,
    build_system_prompt,
    generate_resume,
    inject_job_description,
    make_client,
    provider_config_from_row,
)
from app.utils import utcnow

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(csrf_protect)])


async def _load(session: AsyncSession, profile_id: str) -> Profile:
    profile = await session.get(Profile, profile_id)
    if profile is None:
        raise APIError("not_found", "Profile not found.", status_code=404)
    return profile


async def _out(session: AsyncSession, profile: Profile) -> dict:
    maker_count = (
        await session.execute(
            select(func.count(ProfileAssignment.id)).where(
                ProfileAssignment.profile_id == profile.id, ProfileAssignment.ended_at.is_(None)
            )
        )
    ).scalar_one()
    doc_set_count = (
        await session.execute(
            select(func.count(DocSet.id)).join(Job, Job.id == DocSet.job_id).where(Job.profile_id == profile.id)
        )
    ).scalar_one()
    theme = await session.get(Theme, profile.theme_id) if profile.theme_id else None
    provider = await session.get(LLMProvider, profile.provider_id) if profile.provider_id else None
    active_prompt = (
        await session.get(PromptVersion, profile.active_prompt_version_id)
        if profile.active_prompt_version_id
        else None
    )
    return profile_out(
        profile,
        maker_count=int(maker_count or 0),
        doc_set_count=int(doc_set_count or 0),
        theme_name=theme.name if theme else None,
        provider_name=provider.display_name if provider else None,
        active_prompt=active_prompt,
    )


@router.get("")
async def list_profiles(
    q: str | None = None,
    status: str | None = None,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    query = select(Profile)
    if q:
        query = query.where(Profile.name.ilike(f"%{q}%"))
    if status:
        query = query.where(Profile.status == status)
    profiles = (await session.execute(query.order_by(Profile.name))).scalars().all()
    return {"items": [await _out(session, profile) for profile in profiles]}


@router.post("", status_code=201)
async def create_profile(
    payload: ProfileRequest,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    existing = (await session.execute(select(Profile).where(Profile.name == payload.name))).scalar_one_or_none()
    if existing is not None:
        raise APIError("name_taken", "A profile with that name already exists.", status_code=409)
    profile = Profile(
        name=payload.name,
        url=payload.url,
        description=payload.description,
        start_date=date.fromisoformat(payload.start_date) if payload.start_date else None,
        end_date=date.fromisoformat(payload.end_date) if payload.end_date else None,
        theme_id=payload.theme_id,
        provider_id=payload.provider_id,
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        tags=payload.tags or [],
        custom_fields=payload.custom_fields or {},
        shared_fields=payload.shared_fields or ["Name", "URL"],
    )
    session.add(profile)
    await session.flush()
    if payload.prompt_body:
        version = PromptVersion(
            profile_id=profile.id, version_no=1, body=payload.prompt_body,
            change_note=payload.prompt_change_note or "Initial version", created_by=user.id,
        )
        session.add(version)
        await session.flush()
        profile.active_prompt_version_id = version.id
    session.add(
        AuditLog(actor_id=user.id, action="profile.create", entity_type="profile", entity_id=profile.id,
                 after={"name": profile.name}, ip=client_ip(request))
    )
    await session.commit()
    return await _out(session, profile)


@router.get("/{profile_id}")
async def get_profile(
    profile_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    payload = await _out(session, profile)
    versions = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.profile_id == profile.id).order_by(PromptVersion.version_no.desc())
        )
    ).scalars().all()
    payload["prompt_versions"] = [prompt_version_out(version) for version in versions]
    return payload


@router.patch("/{profile_id}")
async def update_profile(
    profile_id: str,
    payload: ProfileUpdate,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    before = {"name": profile.name, "theme_id": profile.theme_id, "provider_id": profile.provider_id}
    data = payload.model_dump(exclude_unset=True)
    for key in ("start_date", "end_date"):
        if key in data and data[key]:
            data[key] = date.fromisoformat(data[key])
    for key, value in data.items():
        setattr(profile, key, value)
    session.add(
        AuditLog(actor_id=user.id, action="profile.update", entity_type="profile", entity_id=profile.id,
                 before=before, after=data, ip=client_ip(request))
    )
    await session.commit()
    return await _out(session, profile)


@router.post("/{profile_id}/archive")
async def archive_profile(
    profile_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    profile.status = "active" if profile.status == "archived" else "archived"
    if profile.status == "archived":
        assignments = (
            await session.execute(
                select(ProfileAssignment).where(
                    ProfileAssignment.profile_id == profile.id, ProfileAssignment.ended_at.is_(None)
                )
            )
        ).scalars().all()
        for assignment in assignments:
            assignment.ended_at = utcnow()
    session.add(
        AuditLog(actor_id=user.id, action="profile.archive", entity_type="profile", entity_id=profile.id,
                 after={"status": profile.status})
    )
    await session.commit()
    return await _out(session, profile)


@router.get("/{profile_id}/prompt-versions")
async def list_prompt_versions(
    profile_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    versions = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.profile_id == profile.id).order_by(PromptVersion.version_no.desc())
        )
    ).scalars().all()
    # PRO-3: the version list shows the author and a diff against the previous version.
    authors = {
        row.id: row.name
        for row in (
            await session.execute(select(User).where(User.id.in_({v.created_by for v in versions if v.created_by})))
        ).scalars()
    }
    bodies = {version.version_no: version.body for version in versions}
    items = []
    for version in versions:
        payload = prompt_version_out(version) | {
            "active": version.id == profile.active_prompt_version_id,
            "author_name": authors.get(version.created_by),
            "previous_body": bodies.get(version.version_no - 1),
        }
        payload["diff"] = _prompt_diff(bodies.get(version.version_no - 1), version.body)
        items.append(payload)
    return {"items": items, "output_contract": build_system_prompt("")}


def _prompt_diff(previous: str | None, current: str) -> list[dict[str, str]]:
    """PRO-3: a compact line diff (``difflib``) the UI renders next to a version."""
    import difflib

    previous_lines = (previous or "").splitlines()
    current_lines = (current or "").splitlines()
    if previous is None:
        return [{"op": "add", "line": line} for line in current_lines][:200]
    diff: list[dict[str, str]] = []
    for line in difflib.unified_diff(previous_lines, current_lines, lineterm="", n=0):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            diff.append({"op": "hunk", "line": line})
        elif line.startswith("+"):
            diff.append({"op": "add", "line": line[1:]})
        elif line.startswith("-"):
            diff.append({"op": "remove", "line": line[1:]})
        else:
            diff.append({"op": "context", "line": line[1:]})
    return diff[:200]


@router.post("/{profile_id}/prompt-versions", status_code=201)
async def create_prompt_version(
    profile_id: str,
    payload: PromptVersionRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    max_no = (
        await session.execute(
            select(func.max(PromptVersion.version_no)).where(PromptVersion.profile_id == profile.id)
        )
    ).scalar_one()
    version = PromptVersion(
        profile_id=profile.id,
        version_no=int(max_no or 0) + 1,
        body=payload.body,
        change_note=payload.change_note,
        created_by=user.id,
    )
    session.add(version)
    await session.flush()
    profile.active_prompt_version_id = version.id
    session.add(
        AuditLog(actor_id=user.id, action="profile.prompt_version", entity_type="profile", entity_id=profile.id,
                 after={"version_no": version.version_no})
    )
    await session.commit()
    return prompt_version_out(version)


@router.post("/{profile_id}/prompt-versions/{version_id}/activate")
async def activate_prompt_version(
    profile_id: str,
    version_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    version = await session.get(PromptVersion, version_id)
    if version is None or version.profile_id != profile.id:
        raise APIError("not_found", "Prompt version not found.", status_code=404)
    profile.active_prompt_version_id = version.id
    session.add(
        AuditLog(actor_id=user.id, action="profile.prompt_activate", entity_type="profile", entity_id=profile.id,
                 after={"version_no": version.version_no})
    )
    await session.commit()
    return {"ok": True, "active_prompt_version_id": version.id}


@router.post("/{profile_id}/test")
async def test_prompt(
    profile_id: str,
    payload: TestPromptRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """LLM-5: run once without creating a Maker-visible doc set."""
    profile = await _load(session, profile_id)
    snapshot = await snapshots.resolve_profile_snapshot(session, profile)
    provider = await session.get(LLMProvider, snapshot["provider_id"])
    if provider is None:
        raise APIError("no_provider_configured", "No provider is configured for this profile.", status_code=409)
    config = provider_config_from_row(
        provider,
        model=snapshot["model"],
        temperature=snapshot["llm_params"].get("temperature"),
        max_tokens=snapshot["llm_params"].get("max_tokens"),
    )
    # LLM-5: test the text in the editor when supplied, otherwise the saved version.
    prompt = payload.prompt_body or ""
    if not prompt and profile.active_prompt_version_id:
        version = await session.get(PromptVersion, profile.active_prompt_version_id)
        prompt = version.body if version else ""
    try:
        data, raw, log = await generate_resume(
            make_client(config), provider=config, profile_prompt=prompt, jd_text=payload.jd_text
        )
    except LLMError as exc:
        raise APIError("llm_test_failed", exc.message, status_code=502, details={"code": exc.code}) from exc
    data = inject_job_description(data, payload.jd_text)
    from vendor.resume_builder import core

    name, basename = core.make_names(data)
    return {"json": data, "raw": raw, "call_log": log, "candidate_name": name, "docx_basename": basename}


@router.post("/{profile_id}/test/pdf")
async def test_prompt_pdf(
    profile_id: str,
    payload: TestPromptRequest,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """LLM-5: same test run, but rendered to PDF with the profile's theme."""
    profile = await _load(session, profile_id)
    snapshot = await snapshots.resolve_profile_snapshot(session, profile)
    provider = await session.get(LLMProvider, snapshot["provider_id"])
    if provider is None:
        raise APIError("no_provider_configured", "No provider is configured for this profile.", status_code=409)
    config = provider_config_from_row(
        provider,
        model=snapshot["model"],
        temperature=snapshot["llm_params"].get("temperature"),
        max_tokens=snapshot["llm_params"].get("max_tokens"),
    )
    prompt = payload.prompt_body or ""
    if not prompt and profile.active_prompt_version_id:
        version = await session.get(PromptVersion, profile.active_prompt_version_id)
        prompt = version.body if version else ""
    try:
        data, _raw, _log = await generate_resume(
            make_client(config), provider=config, profile_prompt=prompt, jd_text=payload.jd_text
        )
        data = inject_job_description(data, payload.jd_text)
        output = render.render_generation(
            llm_json=data,
            theme_snapshot=snapshot["theme_snapshot"],
            doc_set_path=storage.tmp_dir(),
            generation_no=0,
        )
    except LLMError as exc:
        raise APIError("llm_test_failed", exc.message, status_code=502, details={"code": exc.code}) from exc
    except render.RenderError as exc:
        raise APIError("render_failed", exc.message, status_code=502, details={"code": exc.code}) from exc
    try:
        pdf = next((item for item in output.files if item.kind == "pdf"), None)
        if pdf is None:
            raise APIError("render_failed", "The PDF was not produced.", status_code=502)
        content = pdf.path.read_bytes()
    finally:
        render.discard(output)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="prompt-test-{profile.id[:8]}.pdf"'},
    )


@router.get("/{profile_id}/assignments")
async def list_assignments(
    profile_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    day = await intake.server_today(session)
    rows = (
        await session.execute(
            select(ProfileAssignment, User)
            .join(User, User.id == ProfileAssignment.maker_id)
            .where(ProfileAssignment.profile_id == profile.id, ProfileAssignment.ended_at.is_(None))
            .order_by(User.name)
        )
    ).all()
    items = []
    for assignment, maker in rows:
        items.append(
            {
                "assignment_id": assignment.id,
                "maker_id": maker.id,
                "maker_name": maker.name,
                "maker_email": maker.email,
                "daily_limit": maker.daily_limit,
                "assigned_at": assignment.started_at,
                "usage_today": await intake.usage_today(session, maker.id, day),
            }
        )
    return {"items": items}


@router.post("/{profile_id}/assignments")
async def assign_makers(
    profile_id: str,
    payload: AssignmentRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    if profile.status != "active":
        raise APIError("profile_archived", "This profile is archived; unarchive it first.", status_code=409)
    started = utcnow()
    moved = 0
    for maker_id in payload.maker_ids:
        maker = await session.get(User, maker_id)
        if maker is None or maker.role != "maker":
            raise APIError("invalid_maker", f"User {maker_id} is not a maker.", status_code=400)
        current = (
            await session.execute(
                select(ProfileAssignment).where(
                    ProfileAssignment.maker_id == maker_id, ProfileAssignment.ended_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if current is not None:
            if current.profile_id == profile.id:
                continue
            current.ended_at = started
            moved += 1
        session.add(
            ProfileAssignment(profile_id=profile.id, maker_id=maker_id, assigned_by=user.id, started_at=started)
        )
    session.add(
        AuditLog(actor_id=user.id, action="profile.assign", entity_type="profile", entity_id=profile.id,
                 after={"maker_ids": payload.maker_ids, "moved": moved})
    )
    await session.commit()
    return {"ok": True, "assigned": len(payload.maker_ids), "moved": moved}


@router.post("/assignments/{assignment_id}/end")
async def end_assignment(
    assignment_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    assignment = await session.get(ProfileAssignment, assignment_id)
    if assignment is None:
        raise APIError("not_found", "Assignment not found.", status_code=404)
    assignment.ended_at = utcnow()
    session.add(
        AuditLog(actor_id=user.id, action="profile.unassign", entity_type="profile", entity_id=assignment.profile_id,
                 after={"maker_id": assignment.maker_id})
    )
    await session.commit()
    return {"ok": True}


@router.get("/{profile_id}/doc-sets")
async def profile_doc_sets(
    profile_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    profile = await _load(session, profile_id)
    rows = (
        await session.execute(
            select(DocSet, Job)
            .join(Job, Job.id == DocSet.job_id)
            .where(Job.profile_id == profile.id)
            .order_by(Job.submitted_at.desc())
            .limit(200)
        )
    ).all()
    return {"items": [doc_set_summary(doc_set) | {"seq_no": job.seq_no, "submitted_at": job.submitted_at} for doc_set, job in rows]}
