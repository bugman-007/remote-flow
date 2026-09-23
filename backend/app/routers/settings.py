"""Settings: providers, themes, general/retention/disk and system status (SET-1…SET-15)."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import client_ip, csrf_protect, manager_required
from app.errors import APIError
from app.models import (
    AuditLog,
    EventOutbox,
    Generation,
    GenerationAttempt,
    InterviewTemplate,
    Job,
    LLMProvider,
    Profile,
    Theme,
    User,
    WorkerHeartbeat,
)
from app.schemas import (
    ProviderRequest,
    ProviderUpdate,
    SettingsUpdate,
    ThemeAssignRequest,
    ThemePreviewRequest,
    ThemeRequest,
    ThemeUpdate,
)
from app.serializers import provider_out, theme_out
from app.services import events, metrics, pipeline, render, retention, settings_store, storage
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.theme import (
    RENDER_FONTS,
    ThemeValidationError,
    seed_default_theme,
    theme_form_spec,
    theme_from_docx,
    validate_theme_params,
)
from app.utils import utcnow
from vendor.resume_builder import GENERATOR_VERSION, core

router = APIRouter(tags=["settings"], dependencies=[Depends(csrf_protect)])


# ----------------------------------------------------------------- providers


@router.get("/providers")
async def list_providers(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(LLMProvider).order_by(LLMProvider.display_name))).scalars().all()
    return {"items": [provider_out(row, api_key=safe_decrypt(row)) for row in rows]}


def safe_decrypt(provider: LLMProvider) -> str | None:
    try:
        return decrypt_secret(provider.api_key_enc)
    except Exception:  # noqa: BLE001 - wrong MASTER_KEY must not break the list
        return None


@router.post("/providers", status_code=201)
async def create_provider(
    payload: ProviderRequest,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    if payload.type in {"openai_compatible", "azure_openai"} and not payload.base_url:
        raise APIError("base_url_required", "This provider type needs a base URL.", status_code=422)
    provider = LLMProvider(
        type=payload.type,
        display_name=payload.display_name,
        api_key_enc=encrypt_secret(payload.api_key) if payload.api_key else None,
        base_url=payload.base_url,
        default_model=payload.default_model,
        max_concurrency=payload.max_concurrency,
        rpm=payload.rpm,
        timeout_s=payload.timeout_s,
        is_enabled=payload.is_enabled,
    )
    session.add(provider)
    await session.flush()
    existing_default = (
        await session.execute(select(LLMProvider).where(LLMProvider.is_default.is_(True)))
    ).scalars().all()
    if not existing_default and provider.is_enabled:
        provider.is_default = True
    session.add(
        AuditLog(actor_id=user.id, action="provider.create", entity_type="provider", entity_id=provider.id,
                 after={"type": provider.type, "display_name": provider.display_name}, ip=client_ip(request))
    )
    await session.commit()
    return provider_out(provider, api_key=payload.api_key)


@router.patch("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    data = payload.model_dump(exclude_unset=True)
    api_key = data.pop("api_key", None)
    if api_key:
        provider.api_key_enc = encrypt_secret(api_key)
    for key, value in data.items():
        setattr(provider, key, value)
    session.add(
        AuditLog(actor_id=user.id, action="provider.update", entity_type="provider", entity_id=provider.id,
                 after={k: v for k, v in data.items()}, ip=client_ip(request))
    )
    await session.commit()
    return provider_out(provider, api_key=api_key or safe_decrypt(provider))


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    if provider.is_default:
        raise APIError("default_provider", "Choose another default provider first.", status_code=409)
    in_use = (
        await session.execute(select(func.count(Profile.id)).where(Profile.provider_id == provider.id))
    ).scalar_one()
    session.add(AuditLog(actor_id=user.id, action="provider.delete", entity_type="provider", entity_id=provider.id,
                         before={"display_name": provider.display_name, "profiles": int(in_use or 0)}))
    await session.delete(provider)
    await session.commit()
    return {"ok": True, "profiles_now_on_default": int(in_use or 0)}


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    api_key = safe_decrypt(provider)
    if provider.type == "mock":
        provider.last_status = "ok"
        provider.last_error = None
        await session.commit()
        return {"ok": True, "latency_ms": 5, "model": provider.default_model or "mock"}
    if not api_key:
        raise APIError("missing_api_key", "Add an API key before testing.", status_code=409)
    import time

    try:
        import litellm

        started = time.perf_counter()
        kwargs = {
            "model": provider.default_model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "api_key": api_key,
        }
        if provider.base_url:
            kwargs["api_base"] = provider.base_url
        response = await litellm.acompletion(**kwargs)
        latency = int((time.perf_counter() - started) * 1000)
        provider.last_status = "ok"
        provider.last_error = None
        await session.commit()
        return {"ok": True, "latency_ms": latency, "model": getattr(response, "model", provider.default_model)}
    except Exception as exc:  # noqa: BLE001
        provider.last_status = "error"
        provider.last_error = str(exc)[:900]
        await session.commit()
        raise APIError("provider_test_failed", str(exc)[:400], status_code=502) from exc


@router.post("/providers/{provider_id}/set-default")
async def set_default(
    provider_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    if not provider.is_enabled:
        raise APIError("provider_disabled", "Enable the provider first.", status_code=409)
    rows = (await session.execute(select(LLMProvider))).scalars().all()
    for row in rows:
        row.is_default = row.id == provider.id
        if row.is_default:
            row.is_fallback = False
    session.add(AuditLog(actor_id=user.id, action="provider.set_default", entity_type="provider", entity_id=provider.id))
    await session.commit()
    return {"ok": True}


@router.post("/providers/{provider_id}/set-fallback")
async def set_fallback(
    provider_id: str,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    if provider.is_default:
        raise APIError("is_default", "The default provider cannot also be the fallback.", status_code=409)
    rows = (await session.execute(select(LLMProvider))).scalars().all()
    for row in rows:
        row.is_fallback = row.id == provider.id
    session.add(AuditLog(actor_id=user.id, action="provider.set_fallback", entity_type="provider", entity_id=provider.id))
    await session.commit()
    return {"ok": True}


@router.get("/providers/{provider_id}/models")
async def list_models(provider_id: str, _: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    provider = await session.get(LLMProvider, provider_id)
    if provider is None:
        raise APIError("not_found", "Provider not found.", status_code=404)
    api_key = safe_decrypt(provider)
    if provider.type in {"openai", "openai_compatible"} and api_key:
        base = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
                response.raise_for_status()
                payload = response.json()
                return {"items": sorted(item["id"] for item in payload.get("data", []) if item.get("id"))}
        except Exception as exc:  # noqa: BLE001
            raise APIError("models_unavailable", str(exc)[:300], status_code=502) from exc
    suggestions = {
        "anthropic": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
        "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "google_gemini": ["gemini-2.0-flash", "gemini-1.5-pro"],
        "openrouter": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4"],
        "mock": ["mock"],
    }
    return {"items": suggestions.get(provider.type, []), "guess": True}


# -------------------------------------------------------------------- themes


@router.get("/themes/schema")
async def theme_schema(_: User = Depends(manager_required)):
    return {"fields": theme_form_spec(), "defaults": core.DEFAULTS, "fonts": RENDER_FONTS}


@router.get("/themes")
async def list_themes(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    themes = (await session.execute(select(Theme).order_by(Theme.name))).scalars().all()
    items = []
    for theme in themes:
        profiles = (await session.execute(select(Profile.id, Profile.name).where(Profile.theme_id == theme.id))).all()
        items.append(theme_out(theme, profiles=[{"id": row.id, "name": row.name} for row in profiles]))
    return {"items": items}


@router.post("/themes/preview")
async def preview_theme_params(
    payload: ThemePreviewRequest,
    _: User = Depends(manager_required),
):
    """SET-10: live preview of unsaved params, rendered by the real pipeline."""
    try:
        params = validate_theme_params({**dict(core.DEFAULTS), **(payload.params or {})})
    except ThemeValidationError as exc:
        raise APIError("invalid_theme", str(exc), status_code=422) from exc
    return _render_theme_preview(params, theme_id=None)


@router.post("/themes/import-resume")
async def import_theme_from_resume(
    file: UploadFile = File(...),
    _: User = Depends(manager_required),
):
    """SET-11: guess theme params from an existing resume so the Manager can tweak them."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".docx":
        raise APIError("invalid_file", "Upload a .docx resume.", status_code=422)
    payload = await file.read()
    if not payload:
        raise APIError("invalid_file", "The file is empty.", status_code=422)
    if len(payload) > 20 * 1024 * 1024:
        raise APIError("invalid_file", "Files must be 20 MB or smaller.", status_code=413)
    try:
        return theme_from_docx(payload, filename=file.filename or "resume.docx")
    except ThemeValidationError as exc:
        raise APIError("invalid_docx", str(exc), status_code=422) from exc


@router.post("/themes", status_code=201)
async def create_theme(
    payload: ThemeRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    try:
        params = validate_theme_params(payload.params or dict(core.DEFAULTS))
    except ThemeValidationError as exc:
        raise APIError("invalid_theme", str(exc), status_code=422) from exc
    theme = Theme(name=payload.name, description=payload.description, params=params)
    session.add(theme)
    session.add(AuditLog(actor_id=user.id, action="theme.create", entity_type="theme", after={"name": payload.name}))
    await session.commit()
    return theme_out(theme)


@router.patch("/themes/{theme_id}")
async def update_theme(
    theme_id: str,
    payload: ThemeUpdate,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    theme = await session.get(Theme, theme_id)
    if theme is None:
        raise APIError("not_found", "Theme not found.", status_code=404)
    data = payload.model_dump(exclude_unset=True)
    if "params" in data and data["params"] is not None:
        try:
            data["params"] = validate_theme_params(data["params"])
        except ThemeValidationError as exc:
            raise APIError("invalid_theme", str(exc), status_code=422) from exc
    for key, value in data.items():
        setattr(theme, key, value)
    session.add(AuditLog(actor_id=user.id, action="theme.update", entity_type="theme", entity_id=theme.id,
                         after={k: v for k, v in data.items()}))
    await session.commit()
    return theme_out(theme)


@router.post("/themes/{theme_id}/assign")
async def assign_theme(
    theme_id: str,
    payload: ThemeAssignRequest,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    theme = await session.get(Theme, theme_id)
    if theme is None:
        raise APIError("not_found", "Theme not found.", status_code=404)
    profiles = (await session.execute(select(Profile))).scalars().all()
    for profile in profiles:
        if profile.id in payload.profile_ids:
            profile.theme_id = theme.id
        elif profile.theme_id == theme.id:
            profile.theme_id = None
    session.add(AuditLog(actor_id=user.id, action="theme.assign", entity_type="theme", entity_id=theme.id,
                         after={"profile_ids": payload.profile_ids}))
    await session.commit()
    return {"ok": True, "assigned": len(payload.profile_ids)}


@router.post("/themes/{theme_id}/preview")
async def preview_theme(
    theme_id: str,
    _: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    """SET-10: render the bundled sample.json through the real pipeline."""
    theme = await session.get(Theme, theme_id)
    if theme is None:
        raise APIError("not_found", "Theme not found.", status_code=404)
    return _render_theme_preview(theme.params, theme_id=theme.id)


def _render_theme_preview(params: dict, *, theme_id: str | None) -> Response:
    """SET-10: render the bundled sample.json with the given params (saved or not)."""
    sample_path = Path(__file__).resolve().parents[2] / "vendor" / "resume_builder" / "sample.json"
    data = json.loads(sample_path.read_text(encoding="utf-8"))
    data["job_description"] = "Sample job description used for theme previews."
    doc_set_dir = storage.tmp_dir() / "theme-preview"
    try:
        output = render.render_generation(
            llm_json=data,
            theme_snapshot=params,
            doc_set_path=doc_set_dir,
            generation_no=0,
            meta={"preview": True, "theme_id": theme_id},
        )
    except render.RenderError as exc:
        raise APIError("render_unavailable", exc.message, status_code=503) from exc
    pdf = next((artifact for artifact in output.files if artifact.kind == "pdf"), None)
    if pdf is None:
        raise APIError("render_unavailable", "No PDF was produced.", status_code=503)
    payload = pdf.path.read_bytes()
    shutil.rmtree(output.temp_dir, ignore_errors=True)
    return Response(content=payload, media_type="application/pdf")


# -------------------------------------------------------------- general/disk


@router.get("/system/fonts")
async def system_fonts(_: User = Depends(manager_required)):
    return {"items": RENDER_FONTS}


@router.get("/settings")
async def get_settings_endpoint(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    # `schema` must be a list: a dict_keys object is not JSON-serialisable and
    # made this endpoint fail for every Manager (SET-1).
    return {
        "values": await settings_store.all_settings(session),
        "schema": sorted(settings_store.SETTING_TYPES),
    }


@router.patch("/settings")
async def patch_settings(
    payload: SettingsUpdate,
    request: Request,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    try:
        values = await settings_store.set_settings(
            session, payload.values, actor_id=user.id, ip=client_ip(request)
        )
    except settings_store.SettingsError as exc:
        raise APIError("invalid_setting", str(exc), status_code=422) from exc
    await session.commit()
    return {"values": values}


@router.post("/retention/dry-run")
async def retention_dry_run(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    plan = await retention.run_retention(session, dry_run=True)
    await session.rollback()
    return plan


@router.post("/system/intake/pause")
async def pause_intake(
    request: Request,
    reason: str | None = None,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    await settings_store.set_settings(
        session,
        {"intake_paused": True, "intake_pause_reason": reason or f"Paused by {user.email}"},
        actor_id=user.id,
        ip=client_ip(request),
    )
    await events.emit(session, audience={"user_ids": [], "roles": ["manager", "maker"]},
                      event_type="intake.paused", payload={"reason": reason})
    await session.commit()
    return {"ok": True, "intake_paused": True}


@router.post("/system/intake/resume")
async def resume_intake(
    request: Request,
    force: bool = False,
    user: User = Depends(manager_required),
    session: AsyncSession = Depends(get_session),
):
    usage = storage.disk_usage_pct()
    if usage >= 85 and not force:
        raise APIError(
            "disk_above_threshold",
            f"Storage is {usage:.0f}% full; confirm the risk to resume.",
            status_code=409,
            details={"usage_pct": usage},
        )
    await settings_store.set_settings(
        session,
        {"intake_paused": False, "intake_pause_reason": None, "intake_force_resume": force},
        actor_id=user.id,
        ip=client_ip(request),
    )
    await events.emit(session, audience={"user_ids": [], "roles": ["manager", "maker"]},
                      event_type="intake.resumed", payload={"forced": force})
    await session.commit()
    return {"ok": True, "intake_paused": False, "forced": force}


@router.get("/system/status")
async def system_status(_: User = Depends(manager_required), session: AsyncSession = Depends(get_session)):
    return await build_status(session)


async def build_status(session: AsyncSession) -> dict:
    settings = await settings_store.all_settings(session)
    hour_ago = utcnow() - timedelta(hours=1)
    queues = {
        "llm": (
            await session.execute(
                select(func.count(Generation.id)).where(
                    Generation.stage == "llm", Generation.status.in_(("queued", "llm_running", "retry_wait"))
                )
            )
        ).scalar_one(),
        "render": (
            await session.execute(
                select(func.count(Generation.id)).where(
                    Generation.stage == "render", Generation.status.in_(("rendering", "retry_wait"))
                )
            )
        ).scalar_one(),
        "ops": 0,
    }
    in_flight = (
        await session.execute(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.outcome.is_(None), GenerationAttempt.stage == "llm"
            )
        )
    ).scalar_one()
    active_leases = (
        await session.execute(
            select(func.count(Generation.id)).where(
                Generation.lease_token.is_not(None), Generation.lease_expires_at > utcnow()
            )
        )
    ).scalar_one()
    expired_leases = (
        await session.execute(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.outcome == "timed_out", GenerationAttempt.finished_at >= hour_ago
            )
        )
    ).scalar_one()
    outbox_backlog = (
        await session.execute(select(func.count(EventOutbox.id)).where(EventOutbox.published_at.is_(None)))
    ).scalar_one()
    oldest = (
        await session.execute(select(func.min(Generation.created_at)).where(Generation.status == "queued"))
    ).scalar_one()
    attention = (
        await session.execute(
            select(Generation.kind, func.count(Generation.id))
            .where(Generation.status == "needs_attention")
            .group_by(Generation.kind)
        )
    ).all()
    workers = (
        await session.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_heartbeat_at.desc()).limit(50))
    ).scalars().all()
    heartbeat_cutoff = utcnow() - timedelta(seconds=max(get_settings().worker_stale_after_s, 60))
    sweeps_24h = await metrics.sum_prefix(
        session, ["sweep_redispatched", "reconcile_released", "leases_expired"], days=2
    )
    conversions_today = await metrics.sum_prefix(session, ["unoserver_conversions"], days=1)
    retention_generations = await metrics.sum_prefix(session, ["retention_generations"], days=2)
    retention_bytes = await metrics.sum_prefix(session, ["retention_bytes"], days=2)
    retries = (
        await session.execute(
            select(GenerationAttempt.stage, func.count(GenerationAttempt.id))
            .where(GenerationAttempt.outcome.in_(("failed", "timed_out")), GenerationAttempt.finished_at >= hour_ago)
            .group_by(GenerationAttempt.stage)
        )
    ).all()
    db_size = None
    from app.db import is_postgres

    if is_postgres(session.get_bind()):
        db_size = (await session.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
    return {
        "queues": queues,
        "in_flight_llm": int(in_flight or 0),
        "active_leases": int(active_leases or 0),
        "expired_leases_last_hour": int(expired_leases or 0),
        "outbox_backlog": int(outbox_backlog or 0),
        "oldest_queued_at": oldest,
        "needs_attention": {kind: count for kind, count in attention},
        "needs_attention_total": sum(count for _kind, count in attention),
        "retries_last_hour": {stage: count for stage, count in retries},
        "disk": {
            "usage_pct": storage.disk_usage_pct(),
            "warn_pct": settings["disk_warn_pct"],
            "pause_intake_pct": settings["disk_pause_intake_pct"],
            "pause_render_pct": settings["disk_pause_render_pct"],
            "intake_paused": settings["intake_paused"],
            "intake_pause_reason": settings["intake_pause_reason"],
            "storage_dir": str(storage.storage_root()),
        },
        "database_size_bytes": db_size,
        "workers": [
            {
                "name": worker.name,
                "queues": worker.queues or [],
                "concurrency": worker.concurrency,
                "pid": worker.pid,
                "started_at": worker.started_at,
                "last_heartbeat_at": worker.last_heartbeat_at,
                "alive": bool(worker.last_heartbeat_at and worker.last_heartbeat_at >= heartbeat_cutoff),
            }
            for worker in workers
        ],
        "sweeps_24h": sweeps_24h,
        "retention": {"expired_generations": retention_generations, "bytes_freed": retention_bytes},
        "unoserver": {
            "backend": "unoserver",
            "max_conversions": 200,
            "max_rss_mb": 700,
            "available": bool(shutil.which("unoserver") or shutil.which("soffice")),
            "conversions": conversions_today,
        },
        "versions": {
            "app": get_settings().app_version,
            "generator": GENERATOR_VERSION,
            "libreoffice": _libreoffice_version(),
        },
    }


def _libreoffice_version() -> str | None:
    if not shutil.which("soffice"):
        return None
    try:
        import subprocess

        result = subprocess.run(["soffice", "--version"], capture_output=True, timeout=10, check=False)
        return result.stdout.decode("utf-8", "replace").strip()[:200]
    except Exception:  # noqa: BLE001
        return None
