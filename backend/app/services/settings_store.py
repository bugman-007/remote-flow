"""Typed access to the ``settings`` key/value table (SET-12, SET-15, STO-5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Setting

#: Defaults and allowed types for every general/retention/disk setting.
SETTING_DEFAULTS: dict[str, Any] = {
    # General (SET-12)
    "timezone": "UTC",
    "default_daily_limit": 100,
    "min_jd_chars": 50,
    "max_jd_chars": 20000,
    "llm_timeout_s": 600,
    "render_timeout_s": 180,
    "max_llm_attempts": 10,
    "max_render_attempts": 3,
    "show_selection_to_makers": True,
    "default_interview_template_id": None,
    # Retention (STO-4)
    "attempt_artifacts_retention_days": 30,
    "superseded_generation_retention_days": 90,
    "docset_retention_days": 365,
    "interview_retention_days": 365,
    # Disk thresholds (STO-5)
    "disk_warn_pct": 85,
    "disk_pause_intake_pct": 90,
    "disk_pause_render_pct": 95,
    # Runtime switches (SET-14)
    "intake_paused": False,
    "intake_pause_reason": None,
    "intake_force_resume": False,
    "executor_paused": False,
}

SETTING_TYPES: dict[str, type | tuple[type, ...]] = {
    "timezone": str,
    "default_daily_limit": int,
    "min_jd_chars": int,
    "max_jd_chars": int,
    "llm_timeout_s": int,
    "render_timeout_s": int,
    "max_llm_attempts": int,
    "max_render_attempts": int,
    "show_selection_to_makers": bool,
    "default_interview_template_id": (str, type(None)),
    "attempt_artifacts_retention_days": int,
    "superseded_generation_retention_days": int,
    "docset_retention_days": int,
    "interview_retention_days": int,
    "disk_warn_pct": int,
    "disk_pause_intake_pct": int,
    "disk_pause_render_pct": int,
    "intake_paused": bool,
    "intake_pause_reason": (str, type(None)),
    "intake_force_resume": bool,
    "executor_paused": bool,
}


class SettingsError(ValueError):
    pass


def validate_setting(key: str, value: Any) -> Any:
    if key not in SETTING_DEFAULTS:
        raise SettingsError(f"unknown setting: {key}")
    expected = SETTING_TYPES[key]
    if expected is bool:
        if not isinstance(value, bool):
            raise SettingsError(f"{key} must be a boolean")
        return value
    if expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{key} must be an integer")
        if value < 0:
            raise SettingsError(f"{key} must be >= 0")
        return value
    if isinstance(expected, tuple):
        if not isinstance(value, expected):
            raise SettingsError(f"{key} has the wrong type")
        return value
    if not isinstance(value, expected):
        raise SettingsError(f"{key} must be a {expected.__name__}")
    return value


async def ensure_defaults(session: AsyncSession) -> None:
    existing = {row[0] for row in (await session.execute(select(Setting.key))).all()}
    for key, value in SETTING_DEFAULTS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))
    await session.flush()


async def get_setting(session: AsyncSession, key: str) -> Any:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        return SETTING_DEFAULTS.get(key)
    return row.value


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    merged = dict(SETTING_DEFAULTS)
    merged.update({row.key: row.value for row in rows})
    return merged


async def set_settings(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    actor_id: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for key, value in values.items():
        cleaned = validate_setting(key, value)
        row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        if row is None:
            row = Setting(key=key, value=cleaned, updated_by=actor_id)
            session.add(row)
        else:
            before[key] = row.value
            row.value = cleaned
            row.updated_by = actor_id
        after[key] = cleaned
    session.add(
        AuditLog(actor_id=actor_id, action="settings.update", entity_type="settings", before=before, after=after, ip=ip)
    )
    await session.flush()
    return await all_settings(session)


async def intake_paused(session: AsyncSession) -> tuple[bool, str | None]:
    paused = bool(await get_setting(session, "intake_paused"))
    reason = await get_setting(session, "intake_pause_reason")
    return paused, reason
