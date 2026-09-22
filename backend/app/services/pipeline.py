"""Generation pipeline: atomic claims with leases, stage-aware retries, recovery.

Implements PIPE-2…PIPE-11. The database is the source of truth; the queue is a
hint, so every entry point re-reads and claims before doing work.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.models import (
    DocSet,
    Generation,
    GenerationAttempt,
    Job,
    LLMProvider,
    Profile,
    PromptVersion,
)
from app.services import events, release, render, settings_store, storage
from app.services.broker import Broker, get_broker
from app.services.dispatch import enqueue_after_commit
from app.services.llm import (
    LLMError,
    LLMResult,
    ProviderConfig,
    generate_resume,
    inject_job_description,
    make_client,
    provider_config_from_row,
)
from app.utils import utcnow

logger = logging.getLogger(__name__)

#: Multiplier applied to every backoff so tests can fast-forward (0.0 == immediate).
RETRY_BACKOFF_SCALE = 1.0
LLM_BACKOFF_S = (5, 15, 45, 120, 300, 600)
RENDER_BACKOFF_S = (5, 15, 45)
FALLBACK_AFTER_PROVIDER_ERRORS = 3

#: CONC-4: on HTTP 429 the provider's effective concurrency drops by 25% for a minute.
RATE_LIMIT_REDUCTION = 0.75
RATE_LIMIT_WINDOW_S = 60.0
RATE_LIMIT_FLOOR = 0.25

#: provider_id -> (factor, expires_at_monotonic). Per-process: every worker converges on
#: its own view of the throttle, and the semaphore itself lives in Redis.
_provider_throttle: dict[str, tuple[float, float]] = {}


def note_provider_rate_limit(provider_id: str | None, *, moment: float | None = None) -> None:
    """CONC-4: shrink the provider's slot budget by 25% for the next 60 s."""
    if not provider_id:
        return
    stamp = time.monotonic() if moment is None else moment
    factor, expires = _provider_throttle.get(provider_id, (1.0, 0.0))
    if expires <= stamp:
        factor = 1.0
    factor = max(RATE_LIMIT_FLOOR, factor * RATE_LIMIT_REDUCTION)
    _provider_throttle[provider_id] = (factor, stamp + RATE_LIMIT_WINDOW_S)


def provider_slot_limit(provider, *, moment: float | None = None) -> int:
    """CONC-4: the provider's configured concurrency, reduced while the throttle is hot."""
    base = max(int(provider.max_concurrency or 1), 1)
    entry = _provider_throttle.get(provider.id)
    if entry is None:
        return base
    factor, expires = entry
    stamp = time.monotonic() if moment is None else moment
    if expires <= stamp:
        _provider_throttle.pop(provider.id, None)
        return base
    return max(1, int(base * factor))


def reset_provider_throttles() -> None:
    """Test helper: forget every adaptive backoff."""
    _provider_throttle.clear()



def now():
    return utcnow()


def actionable_statuses(stage: str) -> tuple[str, ...]:
    return ("queued", "retry_wait") if stage == "llm" else ("rendering", "retry_wait")


def running_status(stage: str) -> str:
    return "llm_running" if stage == "llm" else "rendering"


@dataclass
class Claim:
    generation_id: str
    stage: str
    lease_token: str
    attempt_no: int
    attempt: GenerationAttempt


async def load_generation(session: AsyncSession, generation_id: str) -> Generation | None:
    return (
        await session.execute(
            select(Generation)
            .where(Generation.id == generation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def claim_build(
    session: AsyncSession,
    generation_id: str,
    stage: str,
    worker: str,
    *,
    timeout_s: int,
) -> Claim | None:
    """PIPE-2: single conditional UPDATE; zero rows means another worker owns it."""
    import uuid as uuid_module

    settings = get_settings()
    moment = now()
    lease_token = str(uuid_module.uuid4())
    lease_expires = moment + timedelta(seconds=timeout_s + settings.lease_grace_s)
    stmt = (
        update(Generation)
        .where(
            Generation.id == generation_id,
            Generation.stage == stage,
            Generation.status.in_(actionable_statuses(stage)),
            or_(Generation.next_retry_at.is_(None), Generation.next_retry_at <= moment),
        )
        .values(
            status=running_status(stage),
            claimed_by=worker,
            lease_token=lease_token,
            lease_expires_at=lease_expires,
            next_retry_at=None,
            updated_at=moment,
            llm_attempts=Generation.llm_attempts + (1 if stage == "llm" else 0),
            render_attempts=Generation.render_attempts + (1 if stage == "render" else 0),
        )
    )
    result = await session.execute(stmt)
    if result.rowcount != 1:
        return None
    generation = await load_generation(session, generation_id)
    assert generation is not None
    attempt_no = generation.llm_attempts if stage == "llm" else generation.render_attempts
    attempt = GenerationAttempt(
        generation_id=generation.id,
        stage=stage,
        attempt_no=attempt_no,
        lease_token=lease_token,
        worker=worker,
        provider_id=generation.provider_id,
        model=generation.model,
        started_at=moment,
    )
    session.add(attempt)
    await session.flush()
    return Claim(
        generation_id=generation.id,
        stage=stage,
        lease_token=lease_token,
        attempt_no=attempt_no,
        attempt=attempt,
    )


async def lease_update(session: AsyncSession, generation_id: str, token: str, **values) -> bool:
    """PIPE-2: every later write is conditional on the lease token."""
    values.setdefault("updated_at", now())
    stmt = update(Generation).where(
        Generation.id == generation_id,
        Generation.lease_token == token,
        or_(Generation.lease_expires_at.is_(None), Generation.lease_expires_at > now()),
    ).values(**values)
    result = await session.execute(stmt)
    return result.rowcount == 1


async def budget_used(session: AsyncSession, generation: Generation, stage: str) -> int:
    count = (
        await session.execute(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.generation_id == generation.id,
                GenerationAttempt.stage == stage,
                GenerationAttempt.started_at >= generation.retry_budget_reset_at,
            )
        )
    ).scalar_one()
    return int(count or 0)


def backoff_seconds(stage: str, attempt_no: int, *, retry_after_s: int | None = None) -> int:
    schedule = LLM_BACKOFF_S if stage == "llm" else RENDER_BACKOFF_S
    base = schedule[min(max(attempt_no - 1, 0), len(schedule) - 1)]
    delay = max(base, retry_after_s or 0)
    return int(delay * RETRY_BACKOFF_SCALE)


async def record_failure(
    session: AsyncSession,
    generation: Generation,
    claim: Claim,
    *,
    error_code: str,
    error_message: str,
    timed_out: bool = False,
    provider_error: bool = False,
    retry_after_s: int | None = None,
) -> str:
    """PIPE-4/5: apply the stage-aware retry policy and return the new build status."""
    settings = get_settings()
    stage = claim.stage
    moment = now()
    claim.attempt.outcome = "timed_out" if timed_out else "failed"
    claim.attempt.finished_at = moment
    claim.attempt.error_code = error_code
    claim.attempt.error_message = (error_message or "")[:2000]
    generation.last_error_code = error_code
    generation.last_error_message = (error_message or "")[:2000]
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.claimed_by = None
    generation.dispatch_state = "pending"

    if stage == "llm" and error_code == "provider_rate_limited":
        note_provider_rate_limit(generation.provider_id)

    if provider_error and stage == "llm":
        generation.consecutive_provider_errors = (generation.consecutive_provider_errors or 0) + 1
        await maybe_switch_to_fallback(session, generation, actor="pipeline")
    else:
        generation.consecutive_provider_errors = 0

    used = await budget_used(session, generation, stage)
    budget = settings.max_llm_attempts if stage == "llm" else settings.max_render_attempts
    if used >= budget:
        generation.status = "needs_attention"
        generation.stage = stage
        generation.next_retry_at = None
        new_status = "needs_attention"
        await emit_build_event(session, generation, "build.needs_attention", {
            "blocking": generation.kind != "regenerate",
            "kind": generation.kind,
            "stage": stage,
            "attempt": used,
            "error_code": error_code,
        })
    else:
        delay = backoff_seconds(stage, used, retry_after_s=retry_after_s)
        generation.status = "retry_wait"
        generation.stage = stage
        generation.next_retry_at = moment + timedelta(seconds=delay)
        new_status = "retry_wait"
        enqueue_after_commit(session, generation.id, stage, high_priority=True)

    await emit_job_status(session, generation, extra={"status": new_status, "attempt": used, "stage": stage})
    return new_status


async def maybe_switch_to_fallback(session: AsyncSession, generation: Generation, *, actor: str) -> bool:
    """PIPE-7: after 3 consecutive provider errors, switch to the fallback provider."""
    if (generation.consecutive_provider_errors or 0) < FALLBACK_AFTER_PROVIDER_ERRORS:
        return False
    fallback = (
        await session.execute(
            select(LLMProvider).where(LLMProvider.is_fallback.is_(True), LLMProvider.is_enabled.is_(True)).limit(1)
        )
    ).scalar_one_or_none()
    if fallback is None or fallback.id == generation.provider_id:
        return False
    previous = {"provider_id": generation.provider_id, "model": generation.model}
    generation.provider_id = fallback.id
    generation.model = fallback.default_model or generation.model
    generation.consecutive_provider_errors = 0
    await events.emit(
        session,
        audience=events.managers_only(),
        event_type="system.notice",
        payload={
            "message": f"Fell back to provider {fallback.display_name} for generation {generation.generation_no}",
            "generation_id": generation.id,
        },
        job_id=generation.job_id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": None,
            "to_state": generation.status,
            "stage": "llm",
            "actor": actor,
            "details": {"snapshot_change": "provider_fallback", "previous": previous, "new": {"provider_id": fallback.id, "model": generation.model}},
        },
    )
    return True


async def emit_job_status(session: AsyncSession, generation: Generation, *, extra: dict | None = None) -> None:
    job = await session.get(Job, generation.job_id)
    if job is None:
        return
    payload = await release.status_payload(session, job)
    payload.update(extra or {})
    await events.emit(
        session,
        audience=events.job_audience(job.maker_id),
        event_type="job.status",
        payload=payload,
        job_id=job.id,
        generation_id=generation.id,
    )


async def emit_build_event(session: AsyncSession, generation: Generation, event_type: str, payload: dict) -> None:
    job = await session.get(Job, generation.job_id)
    if job is None:
        return
    await events.emit(
        session,
        audience=events.job_audience(job.maker_id),
        event_type=event_type,
        payload={"job_id": job.id, "generation_id": generation.id, "generation_no": generation.generation_no, **payload},
        job_id=job.id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": None,
            "to_state": generation.status,
            "stage": generation.stage,
            "attempt_no": None,
            "actor": "pipeline",
            "details": payload,
        },
    )


# ------------------------------------------------------------------ provider IO


async def acquire_provider_slot(broker: Broker, provider: LLMProvider) -> bool:
    """CONC-3: semaphore + request-per-minute bucket, acquired before the claim."""
    key = f"rf:provider:{provider.id}:slots"
    acquired = await broker.acquire_slot(
        key, limit=provider_slot_limit(provider), timeout_s=provider.timeout_s
    )
    if not acquired:
        return False
    for _ in range(4):
        minute = int(time.time() // 60)
        count = await broker.incr(f"rf:provider:{provider.id}:rpm:{minute}", ttl_seconds=120)
        if count <= max(provider.rpm, 1):
            return True
        await broker.release_slot(key)
        return False
    return True


async def release_provider_slot(broker: Broker, provider: LLMProvider) -> None:
    await broker.release_slot(f"rf:provider:{provider.id}:slots")


async def load_provider_config(session: AsyncSession, generation: Generation) -> tuple[LLMProvider, ProviderConfig]:
    provider = await session.get(LLMProvider, generation.provider_id) if generation.provider_id else None
    if provider is None:
        raise LLMError("provider_missing", "the build's provider no longer exists", provider_error=True)
    params = generation.llm_params or {}
    config = provider_config_from_row(
        provider,
        model=generation.model or provider.default_model,
        temperature=params.get("temperature"),
        max_tokens=params.get("max_tokens"),
    )
    return provider, config


async def prompt_body(session: AsyncSession, generation: Generation) -> str:
    if not generation.prompt_version_id:
        return ""
    version = await session.get(PromptVersion, generation.prompt_version_id)
    return version.body if version else ""


async def write_llm_artifacts(
    session: AsyncSession,
    generation: Generation,
    *,
    attempt_no: int,
    request_payload: dict,
    raw: str,
    canonical: dict | None,
    error: dict | None = None,
) -> Path:
    doc_set = await release.doc_set_for_job(session, generation.job_id)
    base = storage.storage_root() / (doc_set.storage_dir if doc_set and doc_set.storage_dir else "tmp")
    directory = storage.attempts_dir(base, "llm", attempt_no)
    storage.ensure_dir(directory)
    storage.write_bytes(
        directory / "llm_request.json",
        json.dumps(request_payload, indent=2, ensure_ascii=False).encode("utf-8"),
        kind="meta",
    )
    if raw:
        storage.write_bytes(directory / "llm_response.raw", raw.encode("utf-8"), kind="meta")
    if canonical is not None:
        storage.write_bytes(
            directory / "llm.json", json.dumps(canonical, indent=2, ensure_ascii=False).encode("utf-8"), kind="meta"
        )
    if error is not None:
        storage.write_bytes(directory / "error.json", json.dumps(error, indent=2).encode("utf-8"), kind="meta")
    return directory


async def llm_json_path(session: AsyncSession, generation: Generation) -> Path | None:
    attempt = (
        await session.execute(
            select(GenerationAttempt)
            .where(
                GenerationAttempt.generation_id == generation.id,
                GenerationAttempt.stage == "llm",
                GenerationAttempt.outcome == "succeeded",
            )
            .order_by(GenerationAttempt.attempt_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None or not attempt.artifacts_dir:
        return None
    path = storage.storage_root() / attempt.artifacts_dir / "llm.json"
    return path if path.exists() else None


# ----------------------------------------------------------------- execution


async def execute_llm_attempt(session: AsyncSession, generation_id: str, *, worker: str = "worker-llm") -> str | None:
    settings = get_settings()
    generation = await load_generation(session, generation_id)
    if generation is None or generation.stage != "llm":
        return None
    if generation.status not in actionable_statuses("llm"):
        return None
    broker = get_broker()
    try:
        provider, config = await load_provider_config(session, generation)
    except LLMError as exc:
        claim = await claim_build(session, generation_id, "llm", worker, timeout_s=settings.llm_timeout_s)
        if claim is None:
            return None
        result = await record_failure(
            session, generation, claim, error_code=exc.code, error_message=exc.message, provider_error=True
        )
        logger.warning("llm attempt failed before call", extra={"generation_id": generation_id})
        return result

    if not await acquire_provider_slot(broker, provider):
        enqueue_after_commit(session, generation_id, "llm", high_priority=True)
        return None
    try:
        claim = await claim_build(session, generation_id, "llm", worker, timeout_s=settings.llm_timeout_s)
        if claim is None:
            return None
        job = await session.get(Job, generation.job_id)
        profile_prompt = await prompt_body(session, generation)
        request_payload = {
            "provider_id": provider.id,
            "provider_type": provider.type,
            "model": config.model,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "prompt_version_id": generation.prompt_version_id,
            "attempt": claim.attempt_no,
            "jd_chars": len(job.jd_text) if job else 0,
        }
        client = make_client(config)
        try:
            canonical, raw, call_log = await generate_resume(
                client, provider=config, profile_prompt=profile_prompt, jd_text=job.jd_text if job else ""
            )
            canonical = inject_job_description(canonical, job.jd_text if job else "")
        except LLMError as exc:
            await write_llm_artifacts(
                session,
                generation,
                attempt_no=claim.attempt_no,
                request_payload=request_payload,
                raw="",
                canonical=None,
                error={"code": exc.code, "message": exc.message},
            )
            return await record_failure(
                session,
                generation,
                claim,
                error_code=exc.code,
                error_message=exc.message,
                provider_error=exc.provider_error,
                retry_after_s=exc.retry_after_s,
            )

        artifacts_dir = await write_llm_artifacts(
            session,
            generation,
            attempt_no=claim.attempt_no,
            request_payload={**request_payload, "call_log": call_log},
            raw=raw,
            canonical=canonical,
        )
        usage: dict = next((entry.get("usage") for entry in reversed(call_log) if entry.get("usage")), {})
        latency = next((entry.get("latency_ms") for entry in reversed(call_log) if entry.get("latency_ms")), None)
        published = await lease_update(
            session,
            generation.id,
            claim.lease_token,
            status="rendering",
            stage="render",
            claimed_by=None,
            lease_token=None,
            lease_expires_at=None,
            last_error_code=None,
            last_error_message=None,
            consecutive_provider_errors=0,
        )
        if not published:
            claim.attempt.outcome = "superseded"
            claim.attempt.finished_at = now()
            claim.attempt.artifacts_dir = storage.relative_to_root(artifacts_dir)
            return None
        claim.attempt.outcome = "succeeded"
        claim.attempt.finished_at = now()
        claim.attempt.artifacts_dir = storage.relative_to_root(artifacts_dir)
        claim.attempt.tokens_in = usage.get("tokens_in")
        claim.attempt.tokens_cached = usage.get("tokens_cached")
        claim.attempt.tokens_out = usage.get("tokens_out")
        claim.attempt.latency_ms = latency
        if provider.last_used_at is None or provider.last_used_at < now():
            provider.last_used_at = now()
            provider.last_status = "ok"
        generation.lease_token = None
        generation.lease_expires_at = None
        generation.claimed_by = None
        enqueue_after_commit(session, generation.id, "render")
        await emit_job_status(session, generation, extra={"status": "rendering", "stage": "render"})
        return "rendering"
    finally:
        await release_provider_slot(broker, provider)


async def execute_render_attempt(session: AsyncSession, generation_id: str, *, worker: str = "worker-render") -> str | None:
    settings = get_settings()
    generation = await load_generation(session, generation_id)
    if generation is None or generation.stage != "render":
        return None
    if generation.status not in actionable_statuses("render"):
        return None
    claim = await claim_build(session, generation_id, "render", worker, timeout_s=settings.render_timeout_s)
    if claim is None:
        return None
    doc_set = await release.doc_set_for_job(session, generation.job_id)
    if doc_set is None or not doc_set.storage_dir:
        return await record_failure(
            session, generation, claim, error_code="render_output_missing", error_message="doc set storage is missing"
        )
    json_path = await llm_json_path(session, generation)
    if json_path is None:
        return await record_failure(
            session, generation, claim, error_code="llm_json_missing", error_message="stored LLM JSON not found"
        )
    data = json.loads(json_path.read_text(encoding="utf-8"))
    doc_set_path = storage.storage_root() / doc_set.storage_dir
    try:
        output = render.render_generation(
            llm_json=data,
            theme_snapshot=generation.theme_snapshot,
            doc_set_path=doc_set_path,
            generation_no=generation.generation_no,
            meta={
                "job_id": generation.job_id,
                "generation_id": generation.id,
                "kind": generation.kind,
                "attempt": claim.attempt_no,
                "provider_id": generation.provider_id,
                "model": generation.model,
                "prompt_version_id": generation.prompt_version_id,
            },
        )
    except render.RenderError as exc:
        return await record_failure(
            session, generation, claim, error_code=exc.code, error_message=exc.message
        )
    except Exception as exc:  # noqa: BLE001 - anything else is a render failure
        logger.exception("render crashed", extra={"generation_id": generation.id})
        return await record_failure(
            session, generation, claim, error_code="render_failed", error_message=str(exc)[:500]
        )

    published = await lease_update(
        session,
        generation.id,
        claim.lease_token,
        status="ready",
        ready_at=now(),
        claimed_by=None,
        lease_token=None,
        lease_expires_at=None,
        next_retry_at=None,
        last_error_code=None,
        last_error_message=None,
        dispatch_state="sent",
    )
    if not published:
        render.discard(output)
        claim.attempt.outcome = "superseded"
        claim.attempt.finished_at = now()
        logger.warning("lease expired before publish; result discarded", extra={"generation_id": generation.id})
        return None

    final_dir = storage.generation_dir(doc_set_path, generation.generation_no)
    storage.atomic_publish(output.temp_dir, final_dir)
    generation.storage_dir = storage.relative_to_root(final_dir)
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.claimed_by = None
    for artifact in output.files:
        session.add(
            _file_row(generation, doc_set, artifact, final_dir)
        )
    claim.attempt.outcome = "succeeded"
    claim.attempt.finished_at = now()
    from app.services import metrics

    # SET-14: conversions since restart (per-day counter) for the unoserver health card.
    await metrics.increment(session, metrics.daily_key("unoserver_conversions"))
    await apply_ready_effects(session, generation, data)
    await session.flush()
    return "ready"


def _file_row(generation: Generation, doc_set: DocSet, artifact: storage.StoredFile, final_dir: Path):
    """GEN-2/STO-3: record the *published* path, never the discarded temp folder."""
    from app.models import FileArtifact

    published = final_dir / Path(str(artifact.path)).name
    return FileArtifact(
        generation_id=generation.id,
        doc_set_id=doc_set.id,
        kind=artifact.kind,
        path=storage.relative_to_root(published),
        filename=artifact.filename,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
    )


async def apply_ready_effects(session: AsyncSession, generation: Generation, data: dict) -> None:
    """PIPE-8: names, current generation and the ordered release pass."""
    job = await session.get(Job, generation.job_id)
    if job is None:
        return
    doc_set = await release.doc_set_for_job(session, job.id)
    if doc_set is None:
        doc_set = DocSet(job_id=job.id)
        session.add(doc_set)
        await session.flush()

    if generation.kind in {"initial", "retry_after_skip"}:
        doc_set = await release.fill_doc_set_names(session, job, generation, data)
        doc_set.current_generation_id = generation.id
        await session.flush()
        if generation.kind == "initial":
            job.initial_generation_id = generation.id
            await session.flush()
            await release.release_pass(session, job.maker_id, actor="pipeline")
        else:
            await release.late_release(session, job, generation, actor="pipeline")
    elif generation.kind == "regenerate":
        doc_set.current_generation_id = generation.id
        await events.emit(
            session,
            audience=events.job_audience(job.maker_id),
            event_type="docset.regenerated",
            payload={
                "doc_set_id": doc_set.id,
                "job_id": job.id,
                "generation_id": generation.id,
                "generation_no": generation.generation_no,
            },
            job_id=job.id,
            generation_id=generation.id,
        )
    await session.flush()
    await emit_job_status(session, generation, extra={"status": "ready"})


# ---------------------------------------------------------------- maintenance


async def dispatch_sweep(session: AsyncSession, *, stale_after_s: int = 120) -> int:
    """PIPE-3: re-enqueue work the queue may have lost."""
    moment = now()
    stale_cutoff = moment - timedelta(seconds=stale_after_s)
    rows = (
        await session.execute(
            select(Generation)
            .where(
                Generation.status.in_(("queued", "rendering", "retry_wait")),
                or_(Generation.next_retry_at.is_(None), Generation.next_retry_at <= moment),
                Generation.dispatch_state == "pending",
            )
            .order_by(Generation.created_at)
            .limit(500)
        )
    ).scalars().all()
    stale = (
        await session.execute(
            select(Generation)
            .where(
                Generation.status.in_(("queued", "rendering", "retry_wait")),
                or_(Generation.next_retry_at.is_(None), Generation.next_retry_at <= moment),
                Generation.dispatch_state == "sent",
                Generation.dispatched_at.is_not(None),
                Generation.dispatched_at <= stale_cutoff,
                Generation.lease_token.is_(None),
            )
            .order_by(Generation.created_at)
            .limit(500)
        )
    ).scalars().all()
    dispatched = 0
    for generation in [*rows, *stale]:
        generation.dispatch_state = "sent"
        generation.dispatched_at = moment
        enqueue_after_commit(session, generation.id, generation.stage, high_priority=generation.status == "retry_wait")
        dispatched += 1
    return dispatched


async def lease_watchdog(session: AsyncSession) -> int:
    """PIPE-5: fail attempts whose lease expired and apply the retry policy."""
    moment = now()
    rows = (
        await session.execute(
            select(Generation)
            .where(
                Generation.status.in_(("llm_running", "rendering")),
                Generation.lease_expires_at.is_not(None),
                Generation.lease_expires_at <= moment,
            )
            .limit(200)
        )
    ).scalars().all()
    recovered = 0
    for generation in rows:
        stage = "llm" if generation.status == "llm_running" else "render"
        attempt = (
            await session.execute(
                select(GenerationAttempt)
                .where(
                    GenerationAttempt.generation_id == generation.id,
                    GenerationAttempt.stage == stage,
                    GenerationAttempt.lease_token == generation.lease_token,
                    GenerationAttempt.outcome.is_(None),
                )
                .order_by(GenerationAttempt.attempt_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if attempt is None:
            attempt = GenerationAttempt(
                generation_id=generation.id,
                stage=stage,
                attempt_no=await budget_used(session, generation, stage),
                lease_token=generation.lease_token,
                worker="watchdog",
                started_at=moment,
            )
            session.add(attempt)
            await session.flush()
        claim = Claim(generation.id, stage, generation.lease_token or "", attempt.attempt_no, attempt)
        await record_failure(
            session,
            generation,
            claim,
            error_code="lease_expired",
            error_message="the worker did not finish within its lease",
            timed_out=True,
        )
        recovered += 1
    return recovered


async def reconcile_releases(session: AsyncSession) -> int:
    """ORD-8: catch a crashed release pass."""
    makers = await release.makers_with_ready_cursor(session)
    released = 0
    for maker_id in makers:
        released += len(await release.release_pass(session, maker_id, actor="reconciliation"))
    return released


# ------------------------------------------------------------------- actions


async def retry_now(session: AsyncSession, *, job: Job, mode: str, actor_id: str | None) -> Generation | None:
    """PIPE-6: Manager retry. ``render_only`` never calls the LLM again."""
    from app.services import snapshots

    if job.delivery_status == "skipped":
        profile = await session.get(Profile, job.profile_id) if job.profile_id else None
        if profile is None:
            raise ValueError("job has no profile")
        return await snapshots.create_generation(
            session, job=job, profile=profile, kind="retry_after_skip", created_by=actor_id
        )
    generation = await release.initial_generation(session, job)
    if generation is None:
        raise ValueError("job has no initial generation")
    if generation.status == "ready":
        raise ValueError("generation is already ready")
    generation.retry_budget_reset_at = now()
    generation.next_retry_at = None
    generation.last_error_code = None
    generation.last_error_message = None
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.claimed_by = None
    generation.dispatch_state = "pending"
    if mode == "new_llm_call":
        profile = await session.get(Profile, job.profile_id) if job.profile_id else None
        if profile is None:
            raise ValueError("job has no profile")
        snapshot = await snapshots.resolve_profile_snapshot(session, profile)
        previous = {
            "prompt_version_id": generation.prompt_version_id,
            "provider_id": generation.provider_id,
            "model": generation.model,
            "theme_id": generation.theme_id,
        }
        for key, value in snapshot.items():
            setattr(generation, key, value)
        generation.status = "queued"
        generation.stage = "llm"
        await events.emit(
            session,
            audience=events.job_audience(job.maker_id),
            event_type="job.status",
            payload={"job_id": job.id, "seq_no": job.seq_no, "status": "queued", "snapshot_changed": True},
            job_id=job.id,
            generation_id=generation.id,
            pipeline_event={
                "from_state": "needs_attention",
                "to_state": "queued",
                "stage": "llm",
                "actor": actor_id or "manager",
                "details": {"mode": mode, "previous": previous, "new": snapshot},
            },
        )
        enqueue_after_commit(session, generation.id, "llm", high_priority=True)
    elif mode == "render_only":
        if await llm_json_path(session, generation) is None:
            raise ValueError("no stored LLM JSON to re-render")
        generation.status = "rendering"
        generation.stage = "render"
        await events.emit(
            session,
            audience=events.job_audience(job.maker_id),
            event_type="job.status",
            payload={"job_id": job.id, "seq_no": job.seq_no, "status": "rendering"},
            job_id=job.id,
            generation_id=generation.id,
            pipeline_event={
                "from_state": "needs_attention",
                "to_state": "rendering",
                "stage": "render",
                "actor": actor_id or "manager",
                "details": {"mode": mode},
            },
        )
        enqueue_after_commit(session, generation.id, "render", high_priority=True)
    else:
        raise ValueError(f"unknown retry mode: {mode}")
    await session.flush()
    return generation


async def skip_job(session: AsyncSession, *, job: Job, actor_id: str | None) -> None:
    """ORD-5: remove a blocker from the order and release everything ready behind it."""
    job.delivery_status = "skipped"
    job.skipped_at = now()
    job.skipped_by = actor_id
    generation = await release.initial_generation(session, job)
    if generation is not None and generation.status != "ready":
        generation.status = "cancelled"
        generation.lease_token = None
        generation.lease_expires_at = None
        generation.claimed_by = None
        generation.next_retry_at = None
    await events.emit(
        session,
        audience=events.job_audience(job.maker_id),
        event_type="job.status",
        payload={"job_id": job.id, "seq_no": job.seq_no, "status": "skipped"},
        job_id=job.id,
        generation_id=generation.id if generation else None,
        pipeline_event={
            "from_state": "pending",
            "to_state": "skipped",
            "stage": "release",
            "actor": actor_id or "manager",
            "details": {},
        },
    )
    await session.flush()
    await release.release_pass(session, job.maker_id, actor="skip")


async def cancel_generation(session: AsyncSession, *, generation: Generation, actor_id: str | None) -> None:
    if generation.kind == "initial":
        raise ValueError("initial builds are skipped, not cancelled")
    if generation.status == "ready":
        raise ValueError("a ready generation cannot be cancelled")
    generation.status = "cancelled"
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.claimed_by = None
    generation.next_retry_at = None
    await events.emit(
        session,
        audience=events.managers_only(),
        event_type="job.status",
        payload={"job_id": generation.job_id, "generation_id": generation.id, "status": "cancelled"},
        job_id=generation.job_id,
        generation_id=generation.id,
        pipeline_event={
            "from_state": None,
            "to_state": "cancelled",
            "stage": generation.stage,
            "actor": actor_id or "manager",
            "details": {},
        },
    )
    await session.flush()


async def regenerate(session: AsyncSession, *, doc_set: DocSet, job: Job, actor_id: str | None) -> Generation:
    """PIPE-8: a new generation with a fresh snapshot; delivery status untouched."""
    from app.services import snapshots

    if job.delivery_status != "released":
        raise ValueError("only released doc sets can be regenerated")
    profile = await session.get(Profile, job.profile_id) if job.profile_id else None
    if profile is None:
        raise ValueError("job has no profile")
    return await snapshots.create_generation(session, job=job, profile=profile, kind="regenerate", created_by=actor_id)


# --------------------------------------------------------------------- driver


async def next_actionable(session: AsyncSession) -> tuple[str, str] | None:
    moment = now()
    row = (
        await session.execute(
            select(Generation.id, Generation.stage)
            .join(Job, Job.id == Generation.job_id)
            .where(
                Generation.status.in_(("queued", "rendering", "retry_wait")),
                or_(Generation.next_retry_at.is_(None), Generation.next_retry_at <= moment),
                Generation.lease_token.is_(None),
            )
            .order_by(Job.submitted_date, Job.seq_no, Generation.generation_no)
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def process_one(session: AsyncSession, generation_id: str, stage: str, *, worker: str = "inline") -> str | None:
    if stage == "llm":
        return await execute_llm_attempt(session, generation_id, worker=worker)
    return await execute_render_attempt(session, generation_id, worker=worker)


async def run_until_idle(*, max_steps: int = 1000, worker: str = "inline") -> int:
    """Inline driver used by tests and `make dev`; production uses Celery tasks."""
    from app.services.dispatch import flush_dispatches

    steps = 0
    while steps < max_steps:
        async with session_scope() as session:
            pair = await next_actionable(session)
            if pair is None:
                break
            generation_id, stage = pair
            await process_one(session, generation_id, stage, worker=worker)
            await session.commit()
            await flush_dispatches(session)
        steps += 1
    return steps
