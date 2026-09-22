"""Claims, leases, stage-aware retries and frozen snapshots (PIPE-2…PIPE-9)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import Generation, GenerationAttempt
from app.services import dispatch, pipeline
from app.services.llm import LLMError, LLMResult, set_client_override
from app.utils import utcnow
from tests.helpers import initial_build, llm_attempt_count, render_attempt_count, run_pipeline, submit

VALID = (
    '{"name":"Ada","title":"Engineer","target_company":"Acme","summary":"s",'
    '"experience":[{"title":"Engineer","company":"Acme","bullets":["b"]}]}'
)


class FailingClient:
    def __init__(self, *, code: str = "provider_rate_limited", provider_error: bool = True, raw: str = "") -> None:
        self.code = code
        self.provider_error = provider_error
        self.raw = raw
        self.calls = 0

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls += 1
        if self.raw:
            return LLMResult(json={}, raw=self.raw, usage={}, latency_ms=1)
        raise LLMError(self.code, "injected failure", provider_error=self.provider_error)


class FixedClient:
    def __init__(self, raw: str = VALID) -> None:
        self.raw = raw
        self.calls = 0

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls += 1
        return LLMResult(json={}, raw=self.raw, usage={"tokens_in": 1, "tokens_out": 1, "tokens_cached": 0}, latency_ms=7)


@pytest.mark.asyncio
async def test_two_concurrent_claims_leave_exactly_one_winner(db_session, workspace):
    from app.db import get_session_factory

    job = await submit(db_session, workspace["maker"], "role at Company A for a backend engineer, remote friendly")
    generation = await initial_build(db_session, job)
    await db_session.commit()
    factory = get_session_factory()

    async def attempt(worker: str):
        async with factory() as session:
            claim = await pipeline.claim_build(session, generation.id, "llm", worker, timeout_s=60)
            await session.commit()
            return claim

    claims = await asyncio.gather(attempt("worker-a"), attempt("worker-b"))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert await llm_attempt_count(db_session, generation.id) == 1


@pytest.mark.asyncio
async def test_write_with_an_expired_lease_is_rejected(db_session, workspace):
    from sqlalchemy import update

    job = await submit(db_session, workspace["maker"], "role at Company B for a platform engineer, remote role")
    generation = await initial_build(db_session, job)
    await db_session.commit()
    claim = await pipeline.claim_build(db_session, generation.id, "llm", "worker-a", timeout_s=60)
    await db_session.commit()
    assert claim is not None
    await db_session.execute(
        update(Generation).where(Generation.id == generation.id).values(lease_expires_at=utcnow() - timedelta(seconds=1))
    )
    await db_session.commit()
    accepted = await pipeline.lease_update(db_session, generation.id, claim.lease_token, status="rendering")
    assert accepted is False
    attempt = (await db_session.execute(select(GenerationAttempt).where(GenerationAttempt.id == claim.attempt.id))).scalar_one()
    assert attempt.outcome is None


@pytest.mark.asyncio
async def test_render_failure_never_triggers_an_llm_call(db_session, workspace, fast_settings):
    from app.services import render

    fast_settings.max_render_attempts = 3
    client = FixedClient()
    set_client_override(client)
    try:
        job = await submit(db_session, workspace["maker"], "role at Company C for a data engineer, remote friendly")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        # First run the LLM stage alone, then make every render fail.
        await pipeline.process_one(db_session, generation.id, "llm")
        await db_session.commit()
        render.set_pdf_converter(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("libreoffice exploded")))
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status == "needs_attention"
        assert generation.stage == "render"
        assert client.calls == 1
        assert await llm_attempt_count(db_session, generation.id) == 1
        assert await render_attempt_count(db_session, generation.id) == 3
    finally:
        set_client_override(None)


@pytest.mark.asyncio
async def test_manager_retries_grant_a_fresh_budget_without_resetting_attempt_numbers(
    db_session, workspace, fast_settings
):
    from app.services import render

    fast_settings.max_render_attempts = 2
    client = FixedClient()
    set_client_override(client)
    try:
        job = await submit(db_session, workspace["maker"], "role at Company D for a staff engineer, remote friendly")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        await pipeline.process_one(db_session, generation.id, "llm")
        await db_session.commit()
        render.set_pdf_converter(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status == "needs_attention"
        assert generation.render_attempts == 2

        render.set_pdf_converter(None)
        from app.services import render as render_module

        def ok_converter(docx_path, pdf_path, *, timeout_s=60):
            from pathlib import Path

            Path(pdf_path).write_bytes(b"%PDF-1.4 ok")
            return pdf_path

        render_module.set_pdf_converter(ok_converter)
        await pipeline.retry_now(db_session, job=job, mode="render_only", actor_id=workspace["manager"].id)
        await db_session.commit()
        await dispatch.flush_dispatches(db_session)
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status == "ready"
        assert generation.render_attempts == 3  # numbers keep increasing
        assert client.calls == 1
    finally:
        set_client_override(None)


@pytest.mark.asyncio
async def test_snapshot_is_frozen_until_a_new_llm_call(db_session, workspace, fast_settings):
    from app.models import Theme
    from app.services import render

    fast_settings.max_llm_attempts = 1
    client = FixedClient()
    set_client_override(client)
    try:
        job = await submit(db_session, workspace["maker"], "role at Company E for a backend engineer, remote friendly")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        original_accent = generation.theme_snapshot["accent"]
        assert original_accent == "#1F4E79"

        theme = await db_session.get(Theme, workspace["theme"].id)
        theme.params = {**theme.params, "accent": "#AA0000"}
        await db_session.commit()
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status == "ready"
        assert generation.theme_snapshot["accent"] == original_accent

        await db_session.refresh(job)
        assert job.delivery_status == "released"

        failing = FailingClient(code="invalid_json", provider_error=False, raw="not json")
        set_client_override(failing)
        await pipeline.regenerate(session=db_session, doc_set=await _doc_set(db_session, job),
                                  job=job, actor_id=workspace["manager"].id)
        await db_session.commit()
        await dispatch.flush_dispatches(db_session)
        await run_pipeline()
        regenerated = (await db_session.execute(
            select(Generation).where(Generation.job_id == job.id, Generation.kind == "regenerate")
        )).scalar_one()
        assert regenerated.theme_snapshot["accent"] == "#AA0000"
    finally:
        set_client_override(None)


async def _doc_set(session, job):
    from app.models import DocSet

    return (await session.execute(select(DocSet).where(DocSet.job_id == job.id))).scalar_one()


@pytest.mark.asyncio
async def test_new_llm_call_reresolves_the_snapshot(db_session, workspace, fast_settings):
    fast_settings.max_llm_attempts = 1
    set_client_override(FailingClient())
    try:
        job = await submit(db_session, workspace["maker"], "role at Company F for an SRE, remote friendly team")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status == "needs_attention"
        old_snapshot = dict(generation.theme_snapshot)

        from app.models import Profile

        profile = await db_session.get(Profile, workspace["profile"].id)
        provider = workspace["provider"]
        provider.default_model = "mock-v2"
        await db_session.commit()

        set_client_override(FixedClient())
        await pipeline.retry_now(db_session, job=job, mode="new_llm_call", actor_id=workspace["manager"].id)
        await db_session.commit()
        await dispatch.flush_dispatches(db_session)
        await db_session.refresh(generation)
        assert generation.status == "queued"
        assert generation.model == "mock-v2"
        assert generation.retry_budget_reset_at is not None
        assert old_snapshot["accent"] == generation.theme_snapshot["accent"]
    finally:
        set_client_override(None)


@pytest.mark.asyncio
async def test_fallback_provider_is_used_after_three_provider_errors(db_session, workspace, fast_settings):
    from app.models import LLMProvider

    fast_settings.max_llm_attempts = 10
    fallback = LLMProvider(type="mock", display_name="Fallback mock", default_model="fallback-model", is_fallback=True)
    db_session.add(fallback)
    await db_session.commit()

    class CountingFail(FailingClient):
        calls = 0

        async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
            self.calls += 1
            if provider.model == "fallback-model":
                return LLMResult(json={}, raw=VALID, usage={}, latency_ms=1)
            raise LLMError("provider_server_error", "boom", provider_error=True)

    client = CountingFail()
    set_client_override(client)
    try:
        job = await submit(db_session, workspace["maker"], "role at Company G for a lead engineer, remote role")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        await run_pipeline()
        await db_session.refresh(generation)
        assert generation.status in {"ready", "rendering"}
        assert generation.model == "fallback-model"
    finally:
        set_client_override(None)


@pytest.mark.asyncio
async def test_lease_watchdog_recovers_an_abandoned_attempt(db_session, workspace):
    job = await submit(db_session, workspace["maker"], "role at Company H for a senior engineer, remote role")
    generation = await initial_build(db_session, job)
    await db_session.commit()
    claim = await pipeline.claim_build(db_session, generation.id, "llm", "worker-a", timeout_s=0)
    await db_session.commit()
    assert claim is not None
    recovered = await pipeline.lease_watchdog(db_session)
    await db_session.commit()
    assert recovered == 1
    await db_session.refresh(generation)
    assert generation.status in {"retry_wait", "needs_attention"}
    attempt = (await db_session.execute(select(GenerationAttempt).where(GenerationAttempt.id == claim.attempt.id))).scalar_one()
    assert attempt.outcome == "timed_out"


@pytest.mark.asyncio
async def test_dispatch_sweep_reenqueues_pending_builds(db_session, workspace):
    recorder = dispatch.NoopDispatcher()
    dispatch.set_dispatcher(recorder)
    try:
        job = await submit(db_session, workspace["maker"], "role at Company I for a junior engineer, remote role")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        generation.dispatch_state = "pending"
        await db_session.commit()
        count = await pipeline.dispatch_sweep(db_session)
        await db_session.commit()
        await dispatch.flush_dispatches(db_session)
        assert count >= 1
        assert any(call[0] == generation.id for call in recorder.calls)
    finally:
        dispatch.set_dispatcher(None)


@pytest.mark.asyncio
async def test_published_files_are_recorded_at_their_final_path(db_session, workspace):
    """STO-3/GEN-2: file rows point at the published generation folder, not the temp dir."""
    from sqlalchemy import select

    from app.models import FileArtifact, Generation
    from app.services import storage
    from tests.helpers import doc_set_for

    job = await submit(db_session, workspace["maker"], "role at Company Path for a backend engineer, remote team")
    await run_pipeline()
    await db_session.refresh(job)
    assert job.delivery_status == "released"

    generation = (
        await db_session.execute(select(Generation).where(Generation.job_id == job.id))
    ).scalar_one()
    doc_set = await doc_set_for(db_session, job.id)
    artifacts = (
        await db_session.execute(select(FileArtifact).where(FileArtifact.generation_id == generation.id))
    ).scalars().all()
    kinds = {artifact.kind for artifact in artifacts}
    assert {"docx", "pdf", "txt"} <= kinds
    final_dir = storage.storage_root() / (generation.storage_dir or "")
    for artifact in artifacts:
        assert not artifact.path.startswith("tmp/"), artifact.path
        assert artifact.path.startswith(doc_set.storage_dir), artifact.path
        assert storage.safe_relative(artifact.path).exists(), artifact.path
    assert final_dir.exists()


@pytest.mark.asyncio
async def test_rate_limit_shrinks_provider_concurrency_for_a_minute(db_session, workspace, fast_settings):
    """CONC-4: a 429 drops the effective slot budget by 25% for 60 s, then restores it."""
    pipeline.reset_provider_throttles()
    provider = workspace["provider"]
    base = max(provider.max_concurrency, 1)
    assert pipeline.provider_slot_limit(provider) == base

    fast_settings.max_llm_attempts = 1
    set_client_override(FailingClient(code="provider_rate_limited", provider_error=True))
    try:
        job = await submit(db_session, workspace["maker"], "role at Company H for a platform engineer, remote friendly team")
        generation = await initial_build(db_session, job)
        await db_session.commit()
        await run_pipeline()
    finally:
        set_client_override(None)

    await db_session.refresh(generation)
    assert generation.status == "needs_attention"
    now_ts = time.monotonic()
    assert pipeline.provider_slot_limit(provider, moment=now_ts) == max(1, int(base * 0.75))
    assert pipeline.provider_slot_limit(provider, moment=now_ts + 61) == base
    pipeline.reset_provider_throttles()
