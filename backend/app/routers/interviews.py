"""Interview scheduling, reviewer view and feedback (INT-1…INT-13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import client_ip, csrf_protect, current_user, manager_required
from app.errors import APIError
from app.models import (
    AuditLog,
    DocSet,
    InterviewAttachment,
    Feedback,
    Generation,
    Interview,
    InterviewEvent,
    InterviewStatus,
    InterviewStep,
    InterviewStepRecord,
    InterviewTemplate,
    Job,
    Profile,
    PromptVersion,
    User,
)
from app.routers.common import files_for_generation
from app.schemas import (
    DuplicateInterviewRequest,
    FeedbackRequest,
    InterviewRequest,
    InterviewTemplateRequest,
    InterviewUpdate,
    StepRecordRequest,
    StepRecordUpdate,
    UseGenerationRequest,
)
from app.serializers import feedback_out, file_out, interview_out, profile_out, template_out
from app.services import events, storage, taxonomy
from app.utils import utcnow

router = APIRouter(tags=["interviews"], dependencies=[Depends(csrf_protect)])

#: INT-2 - built-in, non-deletable fields of every template.
BUILTIN_FIELDS = [
    {"key": "reviewer", "label": "Reviewer", "type": "user", "required": True, "builtin": True, "visible_to_reviewer": False},
    {"key": "meeting_time", "label": "Meeting time", "type": "datetime", "required": True, "builtin": True, "visible_to_reviewer": True},
    {"key": "meeting_link", "label": "Meeting link", "type": "url", "required": False, "builtin": True, "visible_to_reviewer": True},
    {"key": "location", "label": "Location", "type": "text", "required": False, "builtin": True, "visible_to_reviewer": True},
    {"key": "notes", "label": "Notes", "type": "textarea", "required": False, "builtin": True, "visible_to_reviewer": True},
]

FIELD_TYPES = {
    "text", "textarea", "datetime", "date", "time", "url", "email", "phone", "number", "select", "multiselect", "checkbox",
}


async def _load_interview(session: AsyncSession, interview_id: str) -> Interview:
    interview = await session.get(Interview, interview_id)
    if interview is None:
        raise APIError("not_found", "Interview not found.", status_code=404)
    return interview


def _ensure_interview_access(user: User, interview: Interview) -> None:
    if user.role == "manager":
        return
    if user.role == "reviewer" and interview.reviewer_id == user.id:
        return
    raise APIError("not_found", "Interview not found.", status_code=404)


async def _interview_out(session: AsyncSession, interview: Interview, *, user: User, steps: list[dict] | None = None) -> dict:
    doc_set = await session.get(DocSet, interview.doc_set_id) if interview.doc_set_id else None
    job = await session.get(Job, doc_set.job_id) if doc_set else None
    reviewer = await session.get(User, interview.reviewer_id) if interview.reviewer_id else None
    generation = await session.get(Generation, interview.generation_id) if interview.generation_id else None
    files = await files_for_generation(session, interview.generation_id) if generation else []
    status_row = await session.get(InterviewStatus, interview.status_id) if interview.status_id else None
    attachments = [
        {
            "id": row.id,
            "kind": row.kind,
            "filename": row.filename,
            "content_type": row.content_type,
            "size_bytes": row.size_bytes,
        }
        for row in (
            await session.execute(
                select(InterviewAttachment).where(InterviewAttachment.interview_id == interview.id)
            )
        ).scalars().all()
    ]
    profile_payload = None
    if user.role == "manager" and job and job.profile_id:
        profile = await session.get(Profile, job.profile_id)
        if profile is not None:
            active = await session.get(PromptVersion, profile.active_prompt_version_id) if profile.active_prompt_version_id else None
            profile_payload = profile_out(profile, active_prompt=active)
    elif user.role == "reviewer" and job and job.profile_id:
        profile = await session.get(Profile, job.profile_id)
        if profile is not None:
            profile_payload = profile_out(profile, shared_only=True)
    payload = interview_out(
        interview,
        doc_set=doc_set,
        job=job,
        reviewer=reviewer,
        files=[file_out(f) for f in files if getattr(f, "kind", "") in {"pdf", "docx", "txt"}],
        profile=profile_payload,
        pinned_generation_no=generation.generation_no if generation else None,
        status_label=taxonomy.status_out(status_row) if status_row else None,
        steps=steps if steps is not None else (await taxonomy.step_records_out(session, [interview.id])).get(interview.id, []),
        attachments=attachments,
    )
    if user.role == "reviewer":
        payload["job"] = {"seq_no": job.seq_no} if job else None
    return payload


@router.get("/interview-templates")
async def list_templates(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(InterviewTemplate).order_by(InterviewTemplate.name))).scalars().all()
    return {"items": [template_out(row) for row in rows], "builtin_fields": BUILTIN_FIELDS, "field_types": sorted(FIELD_TYPES)}


@router.post("/interview-templates", status_code=201)
async def create_template(
    payload: InterviewTemplateRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    _validate_fields(payload.fields)
    template = InterviewTemplate(
        name=payload.name, fields=payload.fields, is_default=payload.is_default, created_by=user.id
    )
    if payload.is_default:
        await _clear_defaults(session)
    session.add(template)
    await session.commit()
    return template_out(template)


@router.put("/interview-templates/{template_id}")
async def update_template(
    template_id: str,
    payload: InterviewTemplateRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    _validate_fields(payload.fields)
    template = await session.get(InterviewTemplate, template_id)
    if template is None:
        raise APIError("not_found", "Template not found.", status_code=404)
    if payload.is_default:
        await _clear_defaults(session)
    template.name = payload.name
    template.fields = payload.fields
    template.is_default = payload.is_default
    await session.commit()
    return template_out(template)


@router.delete("/interview-templates/{template_id}")
async def delete_template(
    template_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    template = await session.get(InterviewTemplate, template_id)
    if template is None:
        raise APIError("not_found", "Template not found.", status_code=404)
    in_use = (
        await session.execute(select(func.count(Interview.id)).where(Interview.template_id == template.id))
    ).scalar_one()
    if in_use:
        raise APIError("template_in_use", "This template is used by existing interviews.", status_code=409)
    await session.delete(template)
    await session.commit()
    return {"ok": True}


async def _clear_defaults(session: AsyncSession) -> None:
    rows = (await session.execute(select(InterviewTemplate).where(InterviewTemplate.is_default.is_(True)))).scalars().all()
    for row in rows:
        row.is_default = False


def _validate_fields(fields: list[dict]) -> None:
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, dict) or not field.get("label"):
            raise APIError("invalid_field", "Every field needs a label.", status_code=422)
        kind = field.get("type", "text")
        if kind not in FIELD_TYPES:
            raise APIError("invalid_field", f"Unsupported field type: {kind}", status_code=422)
        key = str(field.get("key") or "").strip()
        if not key:
            raise APIError("invalid_field", "Every field needs a key.", status_code=422)
        if key in seen:
            raise APIError("invalid_field", f"Duplicate field key: {key}", status_code=422)
        seen.add(key)
        if kind in {"select", "multiselect"} and not field.get("options"):
            raise APIError("invalid_field", f"{field['label']} needs options.", status_code=422)


@router.get("/interviews")
async def list_interviews(
    status: str | None = None,
    tab: str | None = Query(default=None, description="upcoming|past|selected|todo|done|all"),
    flow: str | None = Query(default=None, description="INT-14: all|new|done"),
    reviewer_id: str | None = None,
    maker_id: str | None = None,
    profile_id: list[str] | None = Query(default=None),
    step_id: list[str] | None = Query(default=None),
    status_id: list[str] | None = Query(default=None),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.role == "maker":
        raise APIError("forbidden", "Makers cannot view interviews.", status_code=403)
    if tab == "selected" and user.role == "manager":
        rows = (
            await session.execute(
                select(DocSet).where(
                    DocSet.is_selected.is_(True),
                    ~DocSet.id.in_(select(Interview.doc_set_id)),
                )
            )
        ).scalars().all()
        items = []
        for doc_set in rows:
            job = await session.get(Job, doc_set.job_id) if doc_set.job_id else None
            maker = await session.get(User, job.maker_id) if job else None
            profile = await session.get(Profile, job.profile_id) if job and job.profile_id else None
            generation = await session.get(Generation, doc_set.current_generation_id) if doc_set.current_generation_id else None
            files = (
                await files_for_generation(session, doc_set.current_generation_id)
                if doc_set.current_generation_id
                else []
            )
            items.append(
                {
                    "doc_set_id": doc_set.id,
                    "company_name": doc_set.company_name,
                    "job_title": doc_set.job_title,
                    "candidate_name": doc_set.candidate_name,
                    "maker_name": maker.name if maker else None,
                    "profile_id": job.profile_id if job else None,
                    "profile_name": profile.name if profile else None,
                    "submitted_date": job.submitted_date.isoformat() if job else None,
                    "generation_no": generation.generation_no if generation else None,
                    "file_count": len(files),
                    "selected_at": doc_set.selected_at,
                    "seq_no": job.seq_no if job else None,
                }
            )
        return {"items": items, "pagination": {"page": 1, "page_size": len(items), "total": len(items), "pages": 1}}

    query = select(Interview)
    if user.role == "reviewer":
        query = query.where(Interview.reviewer_id == user.id)
    elif reviewer_id:
        query = query.where(Interview.reviewer_id == reviewer_id)
    if status:
        query = query.where(Interview.status == status)
    if flow == "new":
        query = query.where(Interview.status == "scheduled")
    elif flow == "done":
        query = query.where(Interview.status == "completed")
    if maker_id:
        sub = select(DocSet.id).join(Job, Job.id == DocSet.job_id).where(Job.maker_id == maker_id)
        query = query.where(Interview.doc_set_id.in_(sub))
    if profile_id:
        sub = select(DocSet.id).join(Job, Job.id == DocSet.job_id).where(Job.profile_id.in_(profile_id))
        query = query.where(Interview.doc_set_id.in_(sub))
    if step_id:
        sub = select(InterviewStepRecord.interview_id).where(InterviewStepRecord.step_id.in_(step_id))
        query = query.where(Interview.id.in_(sub))
    if status_id:
        query = query.where(Interview.status_id.in_(status_id))
    if date_from:
        query = query.where(Interview.meeting_at >= date_from)
    if date_to:
        query = query.where(Interview.meeting_at <= date_to)
    if q:
        term = q.strip()
        ds_sub = select(DocSet.id).where(
            or_(DocSet.company_name.ilike(f"%{term}%"), DocSet.job_title.ilike(f"%{term}%"))
        )
        query = query.where(
            or_(
                Interview.doc_set_id.in_(ds_sub),
                Interview.company_name.ilike(f"%{term}%"),
                Interview.job_title.ilike(f"%{term}%"),
                Interview.candidate_name.ilike(f"%{term}%"),
                Interview.tech_stack.ilike(f"%{term}%"),
            )
        )
    interviews = (await session.execute(query)).scalars().all()
    now = utcnow()
    if tab == "todo":
        # INT-4: everything still waiting on the reviewer (not completed/cancelled).
        interviews = [i for i in interviews if i.status == "scheduled"]
        interviews.sort(key=lambda item: item.meeting_at or now)
    elif tab == "done":
        interviews = [i for i in interviews if i.status == "completed"]
        interviews.sort(key=lambda item: item.meeting_at or now, reverse=True)
    elif tab == "upcoming":
        interviews = [i for i in interviews if i.meeting_at and i.meeting_at >= now]
        interviews.sort(key=lambda item: item.meeting_at)
    elif tab == "past":
        interviews = [i for i in interviews if not i.meeting_at or i.meeting_at < now]
        interviews.sort(key=lambda item: item.meeting_at or now, reverse=True)
    elif tab == "done_all":
        interviews.sort(key=lambda item: item.meeting_at or now, reverse=True)
    else:
        interviews.sort(key=lambda item: item.meeting_at or now)
    total = len(interviews)
    page_items = interviews[(page - 1) * page_size : (page - 1) * page_size + page_size]
    steps_by_interview = await taxonomy.step_records_out(session, [item.id for item in page_items])
    payload = [
        await _interview_out(
            session, interview, user=user, steps=steps_by_interview.get(interview.id, [])
        )
        for interview in page_items
    ]
    counts: dict[str, int] | None = None
    if user.role == "reviewer":
        # Counts stay independent of the active tab so the badge never flickers off.
        own = (
            await session.execute(
                select(Interview.status, Interview.seen_by_reviewer_at, Interview.meeting_at).where(
                    Interview.reviewer_id == user.id
                )
            )
        ).all()
        unseen = sum(
            1 for status, seen, meeting_at in own if seen is None and meeting_at and meeting_at >= now and status == "scheduled"
        )
        counts = {
            "total": len(own),
            "todo": sum(1 for status, _seen, _at in own if status == "scheduled"),
            "done": sum(1 for status, _seen, _at in own if status == "completed"),
        }
    else:
        unseen = 0
    return {
        "items": payload,
        "unseen": unseen,
        "counts": counts,
        "pagination": {"page": page, "page_size": page_size, "total": total,
                       "pages": (total + page_size - 1) // page_size},
    }


@router.post("/interviews", status_code=201)
async def create_interview(
    payload: InterviewRequest,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    manual = payload.doc_set_id is None
    doc_set = None
    job = None
    generation = None
    if manual:
        # INT-16: a hand-made interview carries its own company/title and attachments.
        if not (payload.company_name or "").strip() or not (payload.job_title or "").strip():
            raise APIError("missing_company", "Company and job title are required.", status_code=422)
        if not payload.attachment_ids:
            raise APIError("missing_files", "Attach a resume and a job description.", status_code=422)
    else:
        doc_set = await session.get(DocSet, payload.doc_set_id)
        if doc_set is None:
            raise APIError("not_found", "Doc set not found.", status_code=404)
        job = await session.get(Job, doc_set.job_id)
        if doc_set.current_generation_id is None or (job and job.delivery_status != "released"):
            raise APIError("not_ready", "Only released doc sets can be scheduled.", status_code=409)
        generation = await session.get(Generation, doc_set.current_generation_id)
    template = await session.get(InterviewTemplate, payload.template_id) if payload.template_id else None
    if template is None:
        template = (
            await session.execute(
                select(InterviewTemplate).where(InterviewTemplate.is_default.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
    if payload.reviewer_id:
        reviewer = await session.get(User, payload.reviewer_id)
        if reviewer is None or reviewer.role != "reviewer":
            raise APIError("invalid_reviewer", "Reviewer not found.", status_code=400)
    meeting_at = payload.meeting_at
    values = dict(payload.values or {})
    if meeting_at is None and values.get("meeting_time"):
        try:
            meeting_at = datetime.fromisoformat(str(values["meeting_time"]).replace("Z", "+00:00"))
        except ValueError:
            raise APIError("invalid_meeting_time", "Meeting time is not a valid datetime.", status_code=422) from None
    status_row = None
    if payload.status_id:
        status_row = await session.get(InterviewStatus, payload.status_id)
    if status_row is None:
        status_row = await taxonomy.status_by_name(session, taxonomy.SCHEDULED_LABEL)
    interview = Interview(
        doc_set_id=doc_set.id if doc_set else None,
        generation_id=(generation.id if generation else doc_set.current_generation_id) if doc_set else None,
        reviewer_id=payload.reviewer_id,
        template_id=template.id if template else None,
        template_snapshot={
            "name": template.name if template else None,
            "fields": (template.fields if template else []) or [],
            "generation_no": generation.generation_no if generation else None,
        },
        values=values,
        meeting_at=meeting_at,
        meeting_tz=payload.meeting_tz,
        status=taxonomy.lifecycle_for(status_row.name if status_row else None),
        status_id=status_row.id if status_row else None,
        tech_stack=(payload.tech_stack or "").strip() or None,
        company_name=(payload.company_name or (doc_set.company_name if doc_set else None)),
        job_title=(payload.job_title or (doc_set.job_title if doc_set else None)),
        candidate_name=(payload.candidate_name or (doc_set.candidate_name if doc_set else None)),
        created_by=user.id,
    )
    session.add(interview)
    await session.flush()
    # INT-15: every interview starts with its first step.
    step_row = await session.get(InterviewStep, payload.step_id) if payload.step_id else None
    if step_row is None:
        step_row = (
            await session.execute(
                select(InterviewStep).where(InterviewStep.is_active.is_(True)).order_by(InterviewStep.position).limit(1)
            )
        ).scalar_one_or_none()
    session.add(
        InterviewStepRecord(
            interview_id=interview.id,
            step_id=step_row.id if step_row else None,
            position=0,
            reviewer_id=payload.reviewer_id,
        )
    )
    if manual:
        attachments = (
            await session.execute(
                select(InterviewAttachment).where(InterviewAttachment.id.in_(payload.attachment_ids))
            )
        ).scalars().all()
        found = {row.kind for row in attachments}
        if not {"resume", "jd"} <= found:
            raise APIError("missing_files", "Attach both a resume and a job description.", status_code=422)
        for row in attachments:
            row.interview_id = interview.id
    session.add(
        InterviewEvent(interview_id=interview.id, type="created", actor_id=user.id,
                       details={"generation_no": generation.generation_no if generation else None})
    )
    session.add(
        AuditLog(actor_id=user.id, action="interview.create", entity_type="interview", entity_id=interview.id,
                 ip=client_ip(request))
    )
    if interview.reviewer_id:
        await events.emit(
            session,
            audience={"user_ids": [interview.reviewer_id], "roles": []},
            event_type="interview.assigned",
            payload={"interview_id": interview.id, "doc_set_id": doc_set.id if doc_set else None},
        )
    await session.commit()
    return await _interview_out(session, interview, user=user)


@router.get("/interviews/{interview_id}")
async def get_interview(
    interview_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    _ensure_interview_access(user, interview)
    payload = await _interview_out(session, interview, user=user)
    events_rows = (
        await session.execute(
            select(InterviewEvent).where(InterviewEvent.interview_id == interview.id).order_by(InterviewEvent.at)
        )
    ).scalars().all()
    payload["events"] = [
        {"type": row.type, "actor_id": row.actor_id, "details": row.details, "at": row.at} for row in events_rows
    ]
    latest = (
        await session.execute(
            select(Feedback).where(Feedback.interview_id == interview.id).order_by(Feedback.version_no.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        edited = (
            await session.execute(
                select(func.count(Feedback.id)).where(Feedback.interview_id == interview.id)
            )
        ).scalar_one()
        payload["feedback"] = feedback_out(latest, edited=int(edited or 0) > 1)
    return payload


@router.patch("/interviews/{interview_id}")
async def update_interview(
    interview_id: str,
    payload: InterviewUpdate,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    changes = payload.model_dump(exclude_unset=True)
    if "template_id" in changes and changes["template_id"]:
        template = await session.get(InterviewTemplate, changes["template_id"])
        if template is None:
            raise APIError("not_found", "Template not found.", status_code=404)
        interview.template_snapshot = {
            "name": template.name,
            "fields": template.fields,
            "generation_no": (interview.template_snapshot or {}).get("generation_no"),
        }
    if "reviewer_id" in changes and changes["reviewer_id"]:
        reviewer = await session.get(User, changes["reviewer_id"])
        if reviewer is None or reviewer.role != "reviewer":
            raise APIError("invalid_reviewer", "Reviewer not found.", status_code=400)
    if "status_id" in changes and changes["status_id"]:
        status_row = await session.get(InterviewStatus, changes["status_id"])
        if status_row is None:
            raise APIError("not_found", "Status not found.", status_code=404)
        interview.status_id = status_row.id
        interview.status = taxonomy.lifecycle_for(status_row.name)
        if status_row.name == taxonomy.CANCELLED_LABEL:
            interview.cancelled_at = utcnow()
    for key, value in changes.items():
        if key in {"template_id", "status_id"}:
            if key == "template_id":
                interview.template_id = value
            continue
        setattr(interview, key, value)
    if changes.get("status") == "cancelled":
        interview.cancelled_at = utcnow()
    detail = {
        "message": "Interview updated",
    }
    session.add(InterviewEvent(interview_id=interview.id, type="updated", actor_id=user.id, details=detail))
    if interview.reviewer_id:
        await events.emit(
            session,
            audience={"user_ids": [interview.reviewer_id], "roles": []},
            event_type="interview.updated",
            payload={"interview_id": interview.id, "changes": list(changes.keys())},
        )
    await session.commit()
    return await _interview_out(session, interview, user=user)


@router.post("/interviews/{interview_id}/use-generation")
async def use_generation(
    interview_id: str,
    payload: UseGenerationRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    generation = await session.get(Generation, payload.generation_id)
    doc_set = await session.get(DocSet, interview.doc_set_id)
    if generation is None or doc_set is None or generation.job_id != doc_set.job_id:
        raise APIError("not_found", "Generation not found.", status_code=404)
    if generation.status != "ready":
        raise APIError("not_ready", "That generation is not ready.", status_code=409)
    previous = interview.generation_id
    interview.generation_id = generation.id
    interview.template_snapshot = {**(interview.template_snapshot or {}), "generation_no": generation.generation_no}
    session.add(
        InterviewEvent(
            interview_id=interview.id,
            type="generation_changed",
            actor_id=user.id,
            details={"from": previous, "to": generation.id, "generation_no": generation.generation_no},
        )
    )
    if interview.reviewer_id:
        await events.emit(
            session,
            audience={"user_ids": [interview.reviewer_id], "roles": []},
            event_type="interview.updated",
            payload={"interview_id": interview.id, "generation_no": generation.generation_no, "documents_updated": True},
        )
    await session.commit()
    return await _interview_out(session, interview, user=user)


@router.post("/interviews/{interview_id}/cancel")
async def cancel_interview(
    interview_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    interview.status = "cancelled"
    interview.cancelled_at = utcnow()
    session.add(InterviewEvent(interview_id=interview.id, type="cancelled", actor_id=user.id, details={}))
    await session.commit()
    return {"ok": True}


@router.post("/interviews/{interview_id}/duplicate", status_code=201)
async def duplicate_interview(
    interview_id: str,
    payload: DuplicateInterviewRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    reviewer = await session.get(User, payload.reviewer_id)
    if reviewer is None or reviewer.role != "reviewer":
        raise APIError("invalid_reviewer", "Reviewer not found.", status_code=400)
    clone = Interview(
        doc_set_id=interview.doc_set_id,
        generation_id=interview.generation_id,
        reviewer_id=reviewer.id,
        template_id=interview.template_id,
        template_snapshot=interview.template_snapshot,
        values=interview.values,
        meeting_at=interview.meeting_at,
        meeting_tz=interview.meeting_tz,
        status="scheduled",
        created_by=user.id,
    )
    session.add(clone)
    await session.flush()
    session.add(InterviewEvent(interview_id=clone.id, type="duplicated", actor_id=user.id,
                               details={"from": interview.id}))
    await events.emit(
        session,
        audience={"user_ids": [reviewer.id], "roles": []},
        event_type="interview.assigned",
        payload={"interview_id": clone.id, "doc_set_id": clone.doc_set_id},
    )
    await session.commit()
    return await _interview_out(session, clone, user=user)


@router.post("/interviews/{interview_id}/feedback")
async def submit_feedback(
    interview_id: str,
    payload: FeedbackRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    if user.role == "reviewer":
        if interview.reviewer_id != user.id:
            raise APIError("not_found", "Interview not found.", status_code=404)
        if interview.meeting_at and interview.meeting_at > utcnow() and interview.status == "scheduled":
            raise APIError("too_early", "Feedback opens after the meeting time.", status_code=409)
    elif user.role != "manager":
        raise APIError("forbidden", "You cannot submit feedback.", status_code=403)
    existing = (
        await session.execute(
            select(Feedback).where(Feedback.interview_id == interview.id).order_by(Feedback.version_no.desc()).limit(1)
        )
    ).scalar_one_or_none()
    version_no = (existing.version_no + 1) if existing else 1
    if existing and user.role == "reviewer" and existing.author_id != user.id:
        raise APIError("forbidden", "You can only edit your own feedback.", status_code=403)
    session.add(
        Feedback(
            interview_id=interview.id,
            author_id=user.id,
            outcome=payload.outcome,
            rating=payload.rating,
            strengths=payload.strengths,
            concerns=payload.concerns,
            notes=payload.notes,
            version_no=version_no,
        )
    )
    interview.status = "no_show" if payload.outcome == "no_show" else "completed"
    session.add(InterviewEvent(interview_id=interview.id, type="feedback", actor_id=user.id,
                               details={"outcome": payload.outcome, "version_no": version_no}))
    await events.emit(
        session,
        audience={"user_ids": [], "roles": ["manager"]},
        event_type="feedback.added",
        payload={"interview_id": interview.id, "outcome": payload.outcome},
    )
    await session.commit()
    return {"ok": True, "version_no": version_no}


@router.get("/interviews/{interview_id}/feedback")
async def get_feedback(
    interview_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    _ensure_interview_access(user, interview)
    rows = (
        await session.execute(
            select(Feedback).where(Feedback.interview_id == interview.id).order_by(Feedback.version_no.desc())
        )
    ).scalars().all()
    return {
        "items": [feedback_out(row, edited=len(rows) > 1) for row in rows],
        "latest": feedback_out(rows[0], edited=len(rows) > 1) if rows else None,
    }


def _ics_escape(value: str | None) -> str:
    """RFC 5545 text escaping."""
    text = (value or "").replace("\\", "\\\\").replace("\n", "\\n")
    return text.replace(",", "\\,").replace(";", "\\;")


def _ics_moment(moment: datetime | None) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@router.get("/interviews/{interview_id}/ics")
async def interview_ics(
    interview_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """INT-8: one interview as an .ics calendar file."""
    interview = await _load_interview(session, interview_id)
    _ensure_interview_access(user, interview)
    doc_set = await session.get(DocSet, interview.doc_set_id)
    generation = await session.get(Generation, interview.generation_id)
    reviewer = await session.get(User, interview.reviewer_id) if interview.reviewer_id else None
    values = interview.values or {}
    summary = " — ".join(
        part
        for part in (
            "Interview",
            doc_set.company_name if doc_set else None,
            doc_set.job_title if doc_set else None,
        )
        if part
    )
    if interview.meeting_at:
        description_parts = [
            f"Candidate: {doc_set.candidate_name}" if doc_set and doc_set.candidate_name else "",
            f"Meeting link: {values.get('meeting_link')}" if values.get("meeting_link") else "",
            f"Notes: {values.get('notes')}" if values.get("notes") else "",
        ]
        start = _ics_moment(interview.meeting_at)
        end = _ics_moment(interview.meeting_at + timedelta(hours=1))
    else:
        description_parts = []
        start = end = ""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Remote Flow//Interview//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:interview-{interview.id}@remote-flow",
        f"DTSTAMP:{_ics_moment(utcnow())}",
    ]
    if start:
        lines += [f"DTSTART:{start}", f"DTEND:{end}"]
    lines += [
        f"SUMMARY:{_ics_escape(summary)}",
        f"LOCATION:{_ics_escape(str(values.get('location') or ''))}",
        f"DESCRIPTION:{_ics_escape(chr(10).join(part for part in description_parts if part))}",
        f"STATUS:{'CANCELLED' if interview.status == 'cancelled' else 'CONFIRMED'}",
    ]
    if reviewer is not None:
        lines.append(f"ORGANIZER;CN={_ics_escape(reviewer.name)}:mailto:{reviewer.email}")
    if doc_set is not None:
        lines.append(f"SEQUENCE:{max((generation.generation_no if generation else 1), 1)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    filename = f"interview-{(doc_set.company_name if doc_set else 'remote-flow') or 'remote-flow'}.ics"
    return Response(
        content="\r\n".join(lines),
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/interviews/{interview_id}/seen")
async def mark_seen(
    interview_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    _ensure_interview_access(user, interview)
    interview.seen_by_reviewer_at = utcnow()
    await session.commit()
    return {"ok": True}


# ------------------------------------------------- INT-15: per-step history


async def _step_records(session: AsyncSession, interview_id: str) -> list[InterviewStepRecord]:
    return (
        await session.execute(
            select(InterviewStepRecord)
            .where(InterviewStepRecord.interview_id == interview_id)
            .order_by(InterviewStepRecord.position, InterviewStepRecord.created_at)
        )
    ).scalars().all()


@router.post("/interviews/{interview_id}/steps", status_code=201)
async def add_step(
    interview_id: str,
    payload: StepRecordRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """INT-15: move the interview on to its next stage (and un-check Done)."""
    interview = await _load_interview(session, interview_id)
    records = await _step_records(session, interview.id)
    if payload.step_id:
        step = await session.get(InterviewStep, payload.step_id)
        if step is None:
            raise APIError("not_found", "Step not found.", status_code=404)
    reviewer_id = payload.reviewer_id if payload.reviewer_id is not None else interview.reviewer_id
    if reviewer_id:
        reviewer = await session.get(User, reviewer_id)
        if reviewer is None or reviewer.role != "reviewer":
            raise APIError("invalid_reviewer", "Reviewer not found.", status_code=400)
    session.add(
        InterviewStepRecord(
            interview_id=interview.id,
            step_id=payload.step_id,
            position=(records[-1].position + 1) if records else 0,
            reviewer_id=reviewer_id,
            note=payload.note,
        )
    )
    interview.reviewer_id = reviewer_id
    interview.cancelled_at = None
    await taxonomy.apply_label(session, interview, taxonomy.SCHEDULED_LABEL)
    session.add(
        InterviewEvent(interview_id=interview.id, type="step.added", actor_id=user.id,
                       details={"step_id": payload.step_id, "reviewer_id": reviewer_id})
    )
    if reviewer_id:
        await events.emit(
            session,
            audience={"user_ids": [reviewer_id], "roles": []},
            event_type="interview.assigned",
            payload={"interview_id": interview.id},
        )
    await session.commit()
    return await _interview_out(session, interview, user=user)


@router.patch("/interviews/{interview_id}/steps/{record_id}")
async def update_step(
    interview_id: str,
    record_id: str,
    payload: StepRecordUpdate,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """INT-15: Done / Rejected checkboxes and per-step reviewer changes."""
    interview = await _load_interview(session, interview_id)
    record = await session.get(InterviewStepRecord, record_id)
    if record is None or record.interview_id != interview.id:
        raise APIError("not_found", "Step not found.", status_code=404)
    if payload.reviewer_id is not None:
        if payload.reviewer_id:
            reviewer = await session.get(User, payload.reviewer_id)
            if reviewer is None or reviewer.role != "reviewer":
                raise APIError("invalid_reviewer", "Reviewer not found.", status_code=400)
        record.reviewer_id = payload.reviewer_id or None
        interview.reviewer_id = record.reviewer_id
    if payload.note is not None:
        record.note = payload.note
    if payload.done is not None:
        record.done = payload.done
        record.done_at = utcnow() if payload.done else None
        if payload.done:
            record.rejected = False
            interview.cancelled_at = None
            await taxonomy.apply_label(session, interview, taxonomy.DONE_LABEL)
        else:
            await taxonomy.apply_label(session, interview, taxonomy.SCHEDULED_LABEL)
    if payload.rejected is not None:
        record.rejected = payload.rejected
        if payload.rejected:
            record.done = False
            record.done_at = None
            interview.cancelled_at = utcnow()
            await taxonomy.apply_label(session, interview, taxonomy.REJECTED_LABEL)
        else:
            interview.cancelled_at = None
            await taxonomy.apply_label(session, interview, taxonomy.SCHEDULED_LABEL)
    session.add(
        InterviewEvent(interview_id=interview.id, type="step.updated", actor_id=user.id, details={"record_id": record.id})
    )
    if interview.reviewer_id:
        await events.emit(
            session,
            audience={"user_ids": [interview.reviewer_id], "roles": []},
            event_type="interview.updated",
            payload={"interview_id": interview.id},
        )
    await session.commit()
    return await _interview_out(session, interview, user=user)


# ------------------------------------------- INT-16: manual interview files


@router.post("/interviews/attachments", status_code=201)
async def upload_interview_attachment(
    kind: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """INT-16: store a resume/JD as-is for a manually created interview."""
    if kind not in {"resume", "jd"}:
        raise APIError("invalid_kind", "kind must be resume or jd.", status_code=422)
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf"} if kind == "resume" else {".txt", ".md", ".pdf", ".docx"}
    if suffix not in allowed:
        raise APIError("invalid_file", f"Unsupported file type: {suffix or 'unknown'}", status_code=422)
    payload = await file.read()
    if len(payload) == 0:
        raise APIError("invalid_file", "The file is empty.", status_code=422)
    if len(payload) > 20 * 1024 * 1024:
        raise APIError("invalid_file", "Files must be 20 MB or smaller.", status_code=413)
    stored = storage.store_interview_attachment(
        interview_id=str(user.id), kind=kind, filename=file.filename or f"{kind}{suffix}", payload=payload
    )
    row = InterviewAttachment(
        interview_id=None,  # linked when the interview is created
        kind=kind,
        filename=stored.filename,
        path=str(stored.path),
        content_type=file.content_type,
        size_bytes=stored.size_bytes,
    )
    session.add(row)
    await session.commit()
    return {"id": row.id, "kind": kind, "filename": row.filename, "size_bytes": row.size_bytes}


@router.get("/interviews/{interview_id}/attachments/{attachment_id}")
async def download_interview_attachment(
    interview_id: str,
    attachment_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    interview = await _load_interview(session, interview_id)
    _ensure_interview_access(user, interview)
    row = await session.get(InterviewAttachment, attachment_id)
    if row is None or row.interview_id != interview.id:
        raise APIError("not_found", "Attachment not found.", status_code=404)
    return FileResponse(
        row.path,
        filename=row.filename,
        media_type=row.content_type or "application/octet-stream",
    )
