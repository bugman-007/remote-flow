"""Maker-facing derived status (§4.1)."""

from __future__ import annotations

from app.models import DocSet, Generation, Job

BUILD_STATUS_LABELS = {
    "queued": "Queued",
    "llm_running": "Generating",
    "rendering": "Rendering",
    "retry_wait": "Retrying",
    "needs_attention": "Waiting for a manager",
    "ready": "Ready",
    "cancelled": "Skipped",
}


def derived_status(
    job: Job,
    initial: Generation | None,
    *,
    blocker_seq: int | None = None,
    blocker_state: str | None = None,
    blocker_attempt: int | None = None,
    blocker: dict | None = None,
    generation_count: int | None = None,
) -> dict:
    if job.delivery_status == "released":
        return {
            "status": "released",
            "label": "Ready",
            # Callers that do not count generations (e.g. the JD list) pass None.
            "generation": generation_count or 0,
            "stage": "done",
            "attempt": None,
            "released_late": bool(job.released_late),
        }
    if job.delivery_status == "skipped":
        return {"status": "skipped", "label": "Skipped", "generation": 0, "stage": None, "attempt": None}

    if initial is None:
        return {"status": "queued", "label": "Queued", "generation": 0, "stage": "llm", "attempt": 0}

    if blocker and blocker.get("job_id") == job.id:
        blocker = None

    if blocker:
        # ORD-6: name the blocker and its state so the UI can explain the wait.
        blocker_seq = blocker.get("seq_no")
        blocker_state = "needs_attention" if blocker.get("needs_attention") else blocker.get("status")
        blocker_attempt = blocker.get("attempt")

    if initial.status == "ready":
        return {
            "status": "waiting",
            "label": f"Waiting for #{blocker_seq:03d}" if blocker_seq else "Queued",
            "generation": initial.generation_no,
            "stage": "waiting",
            "attempt": None,
            "blocker_seq": blocker_seq,
            "blocker_state": blocker_state,
            "blocker_attempt": blocker_attempt,
        }

    attempt = initial.llm_attempts if initial.stage == "llm" else initial.render_attempts
    label = BUILD_STATUS_LABELS.get(initial.status, initial.status)
    if initial.status == "retry_wait":
        label = f"Retrying ({attempt})"
    return {
        "status": initial.status,
        "label": label,
        "generation": initial.generation_no,
        "stage": initial.stage,
        "attempt": attempt,
        "error": initial.last_error_message,
        "error_code": initial.last_error_code,
    }


def plain_error_message(code: str | None, message: str | None) -> str:
    mapping = {
        "provider_rate_limited": "Provider rate-limited",
        "provider_timeout": "Provider timed out",
        "provider_server_error": "Provider error",
        "provider_auth_error": "Provider rejected the API key",
        "provider_error": "Provider error",
        "invalid_json": "Model returned invalid JSON",
        "invalid_schema": "Model output did not match the schema",
        "render_failed": "PDF conversion failed",
        "render_timeout": "PDF conversion timed out",
        "render_output_missing": "Rendered files were incomplete",
        "lease_expired": "The worker stopped responding",
    }
    return mapping.get(code or "", message or "Unknown error")


def doc_set_summary(doc_set: DocSet) -> dict:
    return {
        "id": doc_set.id,
        "candidate_name": doc_set.candidate_name,
        "company_name": doc_set.company_name,
        "job_title": doc_set.job_title,
        "docx_basename": doc_set.docx_basename,
        "is_selected": doc_set.is_selected,
        "keep": doc_set.keep,
        "current_generation_id": doc_set.current_generation_id,
        "files_expired_at": doc_set.files_expired_at,
    }
