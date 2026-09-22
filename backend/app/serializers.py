"""ORM -> JSON serialisers that keep secrets out of responses (SEC-4)."""

from __future__ import annotations

from typing import Any

from app.models import (
    DocSet,
    Feedback,
    Generation,
    GenerationAttempt,
    Interview,
    InterviewTemplate,
    Job,
    LLMProvider,
    Profile,
    PromptVersion,
    Theme,
    User,
)
from app.services.crypto import mask_secret
from app.services.derived import doc_set_summary


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "daily_limit": user.daily_limit,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
    }


def provider_out(provider: LLMProvider, *, api_key: str | None = None) -> dict:
    return {
        "id": provider.id,
        "type": provider.type,
        "display_name": provider.display_name,
        "api_key_masked": mask_secret(api_key) if api_key else ("\u2022\u2022\u2022\u2022" if provider.api_key_enc else ""),
        "has_api_key": provider.api_key_enc is not None,
        "base_url": provider.base_url,
        "default_model": provider.default_model,
        "max_concurrency": provider.max_concurrency,
        "rpm": provider.rpm,
        "timeout_s": provider.timeout_s,
        "is_enabled": provider.is_enabled,
        "is_default": provider.is_default,
        "is_fallback": provider.is_fallback,
        "last_used_at": provider.last_used_at,
        "last_status": provider.last_status,
        "last_error": provider.last_error,
    }


def theme_out(theme: Theme, *, profiles: list[dict] | None = None) -> dict:
    return {
        "id": theme.id,
        "name": theme.name,
        "description": theme.description,
        "params": theme.params,
        "status": theme.status,
        "profiles": profiles or [],
        "created_at": theme.created_at,
        "updated_at": theme.updated_at,
    }


def prompt_version_out(version: PromptVersion) -> dict:
    return {
        "id": version.id,
        "profile_id": version.profile_id,
        "version_no": version.version_no,
        "body": version.body,
        "change_note": version.change_note,
        "created_by": version.created_by,
        "created_at": version.created_at,
    }


def profile_out(
    profile: Profile,
    *,
    maker_count: int = 0,
    doc_set_count: int = 0,
    theme_name: str | None = None,
    provider_name: str | None = None,
    active_prompt: PromptVersion | None = None,
    shared_only: bool = False,
) -> dict:
    if shared_only:
        allowed = set(profile.shared_fields or [])
        out: dict[str, Any] = {"id": profile.id}
        out["name"] = profile.name if "Name" in allowed else None
        out["url"] = profile.url if "URL" in allowed else None
        out["description"] = profile.description if "Description" in allowed else None
        out["start_date"] = profile.start_date if "Start date" in allowed else None
        out["end_date"] = profile.end_date if "End date" in allowed else None
        out["tags"] = profile.tags if "Tags" in allowed else None
        custom = {
            key: value
            for key, value in (profile.custom_fields or {}).items()
            if f"Custom: {key}" in allowed or key in allowed
        }
        if custom:
            out["custom_fields"] = custom
        return {key: value for key, value in out.items() if value is not None or key == "id"}
    return {
        "id": profile.id,
        "name": profile.name,
        "url": profile.url,
        "description": profile.description,
        "start_date": profile.start_date,
        "end_date": profile.end_date,
        "theme_id": profile.theme_id,
        "theme_name": theme_name,
        "provider_id": profile.provider_id,
        "provider_name": provider_name,
        "model": profile.model,
        "temperature": profile.temperature,
        "max_tokens": profile.max_tokens,
        "tags": profile.tags or [],
        "custom_fields": profile.custom_fields or {},
        "shared_fields": profile.shared_fields or [],
        "status": profile.status,
        "active_prompt_version_id": profile.active_prompt_version_id,
        "active_prompt": prompt_version_out(active_prompt) if active_prompt else None,
        "maker_count": maker_count,
        "doc_set_count": doc_set_count,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def job_out(
    job: Job,
    *,
    derived: dict | None = None,
    doc_set: DocSet | None = None,
    duplicate_seq: int | None = None,
) -> dict:
    return {
        "id": job.id,
        "maker_id": job.maker_id,
        "profile_id": job.profile_id,
        "seq_no": job.seq_no,
        "submitted_date": job.submitted_date,
        "submitted_at": job.submitted_at,
        "delivery_status": job.delivery_status,
        "released_at": job.released_at,
        "released_late": job.released_late,
        "skipped_at": job.skipped_at,
        "duplicate_of": job.duplicate_of,
        "duplicate_seq": duplicate_seq,
        "status": derived,
        "doc_set": doc_set_summary(doc_set) if doc_set else None,
    }


def generation_out(
    generation: Generation,
    *,
    attempts: list[GenerationAttempt] | None = None,
    files: list[dict] | None = None,
) -> dict:
    return {
        "id": generation.id,
        "job_id": generation.job_id,
        "generation_no": generation.generation_no,
        "kind": generation.kind,
        "status": generation.status,
        "stage": generation.stage,
        "llm_attempts": generation.llm_attempts,
        "render_attempts": generation.render_attempts,
        "next_retry_at": generation.next_retry_at,
        "ready_at": generation.ready_at,
        "expired_at": generation.expired_at,
        "claimed_by": generation.claimed_by,
        "lease_expires_at": generation.lease_expires_at,
        "dispatch_state": generation.dispatch_state,
        "last_error_code": generation.last_error_code,
        "last_error_message": generation.last_error_message,
        "consecutive_provider_errors": generation.consecutive_provider_errors,
        "snapshot": {
            "prompt_version_id": generation.prompt_version_id,
            "provider_id": generation.provider_id,
            "model": generation.model,
            "llm_params": generation.llm_params,
            "theme_id": generation.theme_id,
            "theme_snapshot": generation.theme_snapshot,
        },
        "created_by": generation.created_by,
        "created_at": generation.created_at,
        "attempts": [attempt_out(attempt) for attempt in (attempts or [])],
        "files": files or [],
    }


def attempt_out(attempt: GenerationAttempt) -> dict:
    return {
        "id": attempt.id,
        "stage": attempt.stage,
        "attempt_no": attempt.attempt_no,
        "worker": attempt.worker,
        "provider_id": attempt.provider_id,
        "model": attempt.model,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "outcome": attempt.outcome,
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "tokens_in": attempt.tokens_in,
        "tokens_cached": attempt.tokens_cached,
        "tokens_out": attempt.tokens_out,
        "latency_ms": attempt.latency_ms,
        "artifacts_dir": attempt.artifacts_dir,
    }


def file_out(artifact) -> dict:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "expired_at": artifact.expired_at,
    }


def template_out(template: InterviewTemplate) -> dict:
    return {
        "id": template.id,
        "name": template.name,
        "fields": template.fields,
        "is_default": template.is_default,
        "created_at": template.created_at,
    }


def interview_out(
    interview: Interview,
    *,
    doc_set: DocSet | None = None,
    job: Job | None = None,
    reviewer: User | None = None,
    files: list[dict] | None = None,
    profile: dict | None = None,
    pinned_generation_no: int | None = None,
) -> dict:
    newer = bool(
        doc_set is not None
        and doc_set.current_generation_id is not None
        and doc_set.current_generation_id != interview.generation_id
    )
    return {
        "id": interview.id,
        "doc_set_id": interview.doc_set_id,
        "generation_id": interview.generation_id,
        "pinned_generation_no": pinned_generation_no,
        "newer_generation_available": newer,
        "reviewer_id": interview.reviewer_id,
        "reviewer_name": reviewer.name if reviewer else None,
        "template_id": interview.template_id,
        "template_snapshot": interview.template_snapshot,
        "values": interview.values,
        "meeting_at": interview.meeting_at,
        "meeting_tz": interview.meeting_tz,
        "status": interview.status,
        "created_by": interview.created_by,
        "cancelled_at": interview.cancelled_at,
        "seen_by_reviewer_at": interview.seen_by_reviewer_at,
        "created_at": interview.created_at,
        "doc_set": doc_set_summary(doc_set) if doc_set else None,
        "job": ({"id": job.id, "seq_no": job.seq_no, "submitted_date": job.submitted_date} if job else None),
        "files": files or [],
        "profile": profile,
    }


def feedback_out(feedback: Feedback, *, edited: bool = False) -> dict:
    return {
        "id": feedback.id,
        "interview_id": feedback.interview_id,
        "author_id": feedback.author_id,
        "outcome": feedback.outcome,
        "rating": feedback.rating,
        "strengths": feedback.strengths,
        "concerns": feedback.concerns,
        "notes": feedback.notes,
        "version_no": feedback.version_no,
        "edited": edited,
        "created_at": feedback.created_at,
    }
