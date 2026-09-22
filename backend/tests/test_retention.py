"""Retention protection and disk thresholds (STO-4, STO-5)."""

from __future__ import annotations

from datetime import timedelta
import pytest
from sqlalchemy import select, update

from app.models import Generation, Interview, Job
from app.services import retention, settings_store, storage
from app.services.llm import LLMResult, set_client_override
from app.utils import utcnow
from tests.helpers import doc_set_for, run_pipeline, submit

VALID = (
    '{"name":"Ada","title":"Engineer","target_company":"Acme","summary":"s",'
    '"experience":[{"title":"Engineer","company":"Acme","bullets":["b"]}]}'
)


class FixedClient:
    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        return LLMResult(json={}, raw=VALID, usage={}, latency_ms=1)


async def build_released_job(db_session, workspace, *, age_days: int = 400):
    """Submit, build and release one job, then backdate it past every retention cutoff."""
    maker = workspace["maker"]
    set_client_override(FixedClient())
    try:
        job = await submit(db_session, maker, "role at Company Old for a backend engineer, remote friendly")
        await run_pipeline()
    finally:
        set_client_override(None)
    await db_session.refresh(job)
    assert job.delivery_status == "released"

    mutable = utcnow() - timedelta(days=age_days)
    await db_session.execute(update(Job).where(Job.id == job.id).values(submitted_at=mutable))
    await db_session.execute(
        update(Generation).where(Generation.job_id == job.id).values(ready_at=mutable, created_at=mutable)
    )
    await db_session.commit()
    await db_session.refresh(job)
    generation = (
        await db_session.execute(select(Generation).where(Generation.job_id == job.id))
    ).scalar_one()
    doc_set = await doc_set_for(db_session, job.id)
    directory = storage.storage_root() / doc_set.storage_dir
    return job, generation, doc_set, directory


@pytest.mark.asyncio
async def test_dry_run_never_expires_or_deletes_anything(db_session, workspace):
    _job, generation, _doc_set, directory = await build_released_job(db_session, workspace)
    assert directory.exists()

    plan = await retention.run_retention(db_session, dry_run=True)
    await db_session.commit()
    assert plan["dry_run"] is True
    assert generation.id in plan["generation_ids"]
    assert plan["bytes"] > 0

    await db_session.refresh(generation)
    assert generation.expired_at is None
    assert directory.exists()


@pytest.mark.asyncio
async def test_selected_doc_sets_are_protected_from_retention(db_session, workspace):
    job, generation, doc_set, directory = await build_released_job(db_session, workspace)
    doc_set.is_selected = True
    await db_session.commit()

    plan = await retention.run_retention(db_session, dry_run=True)
    assert plan["generation_ids"] == []
    assert any(item["doc_set_id"] == doc_set.id and item["is_selected"] for item in plan["protected"])

    result = await retention.run_retention(db_session)
    await db_session.commit()
    assert result["expired_generations"] == 0
    await db_session.refresh(generation)
    assert generation.expired_at is None
    assert directory.exists()


@pytest.mark.asyncio
async def test_kept_doc_sets_survive_a_real_retention_pass(db_session, workspace):
    _job, generation, doc_set, directory = await build_released_job(db_session, workspace)
    doc_set.keep = True
    await db_session.commit()

    result = await retention.run_retention(db_session)
    await db_session.commit()
    assert result["expired_generations"] == 0
    assert directory.exists()
    await db_session.refresh(generation)
    assert generation.expired_at is None


@pytest.mark.asyncio
async def test_interview_pinned_generations_are_never_expired(db_session, workspace):
    _job, generation, doc_set, directory = await build_released_job(db_session, workspace)
    interview = Interview(
        doc_set_id=doc_set.id,
        generation_id=generation.id,
        reviewer_id=workspace["reviewer"].id,
        status="completed",
        meeting_at=utcnow() - timedelta(days=400),
    )
    db_session.add(interview)
    await db_session.commit()

    assert generation.id in await retention.pinned_generation_ids(db_session)
    plan = await retention.run_retention(db_session, dry_run=True)
    assert generation.id not in plan["generation_ids"]
    assert plan["interview_generations"] == []

    result = await retention.run_retention(db_session)
    await db_session.commit()
    assert result["expired_generations"] == 0
    assert directory.exists()


@pytest.mark.asyncio
async def test_retention_expires_and_deletes_unprotected_generations(db_session, workspace):
    _job, generation, doc_set, directory = await build_released_job(db_session, workspace)

    result = await retention.run_retention(db_session)
    await db_session.commit()
    assert result["expired_generations"] == 1
    await db_session.refresh(generation)
    await db_session.refresh(doc_set)
    assert generation.expired_at is not None
    assert doc_set.files_expired_at is not None
    assert not directory.exists()


@pytest.mark.asyncio
async def test_disk_pressure_pauses_intake_and_then_resumes(db_session, workspace, monkeypatch):
    monkeypatch.setattr(storage, "disk_usage_pct", lambda: 91.5)
    state = await retention.check_disk(db_session)
    await db_session.commit()
    assert state["warn"] is True
    assert state["intake_paused"] is True
    paused, reason = await settings_store.intake_paused(db_session)
    assert paused is True
    assert "intake paused automatically" in (reason or "")

    monkeypatch.setattr(storage, "disk_usage_pct", lambda: 50.0)
    state = await retention.check_disk(db_session)
    await db_session.commit()
    assert state["intake_paused"] is False
    paused, _reason = await settings_store.intake_paused(db_session)
    assert paused is False


@pytest.mark.asyncio
async def test_disk_pressure_marks_rendering_paused_without_touching_intake_override(db_session, workspace, monkeypatch):
    await settings_store.set_settings(db_session, {"intake_force_resume": True})
    await db_session.commit()
    monkeypatch.setattr(storage, "disk_usage_pct", lambda: 96.0)
    state = await retention.check_disk(db_session)
    await db_session.commit()
    assert state["rendering_paused"] is True
    paused, _reason = await settings_store.intake_paused(db_session)
    assert paused is True  # 96% is past the intake threshold too
