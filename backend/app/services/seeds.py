"""Seed data for `make dev` and CI (NFR-11)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InterviewTemplate, LLMProvider, Profile, ProfileAssignment, PromptVersion, User
from app.security import hash_password, normalise_email
from app.services.theme import DEFAULT_THEME_NAME, seed_default_theme
from app.services import settings_store

DEFAULT_PROMPT = (
    "You are an expert technical resume writer. Rewrite the candidate's resume so it targets the "
    "company and role described in the job description. Keep every fact true, mirror the "
    "vocabulary of the job description where it is accurate, and emphasise measurable outcomes."
)

DEFAULT_TEMPLATE_FIELDS = [
    {"key": "meeting_link", "label": "Meeting link", "type": "url", "required": False,
     "visible_to_reviewer": True, "builtin": True},
    {"key": "location", "label": "Location", "type": "text", "required": False,
     "visible_to_reviewer": True, "builtin": True},
    {"key": "notes", "label": "Notes", "type": "textarea", "required": False,
     "visible_to_reviewer": True, "builtin": True},
    {"key": "focus_areas", "label": "Focus areas", "type": "multiselect", "required": False,
     "visible_to_reviewer": True,
     "options": ["Backend", "Frontend", "Data", "DevOps", "Communication"]},
]


async def ensure_user(session: AsyncSession, *, email: str, name: str, role: str, password: str,
                      daily_limit: int | None = None) -> tuple[User, bool]:
    email = normalise_email(email)
    existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None:
        return existing, False
    user = User(
        name=name,
        email=email,
        role=role,
        password_hash=hash_password(password),
        daily_limit=daily_limit if role == "maker" else None,
    )
    session.add(user)
    await session.flush()
    return user, True


async def seed_demo_data(session: AsyncSession, *, password: str = "remote-flow-demo") -> dict:
    """Creates a manager, two makers, a reviewer, a mock provider, a profile and a template."""
    settings = await settings_store.all_settings(session)
    await settings_store.ensure_defaults(session)
    theme = await seed_default_theme(session)

    provider = (
        await session.execute(select(LLMProvider).where(LLMProvider.display_name == "Mock provider"))
    ).scalar_one_or_none()
    if provider is None:
        provider = LLMProvider(
            type="mock",
            display_name="Mock provider",
            default_model="mock",
            is_default=True,
            is_enabled=True,
        )
        session.add(provider)
        await session.flush()

    manager, _ = await ensure_user(
        session, email="manager@example.com", name="Morgan Manager", role="manager", password=password
    )
    maker_one, _ = await ensure_user(
        session, email="maker@example.com", name="Mia Maker", role="maker", password=password,
        daily_limit=int(settings["default_daily_limit"]),
    )
    await ensure_user(
        session, email="maker2@example.com", name="Max Maker", role="maker", password=password,
        daily_limit=int(settings["default_daily_limit"]),
    )
    await ensure_user(
        session, email="reviewer@example.com", name="Rae Reviewer", role="reviewer", password=password
    )

    profile = (await session.execute(select(Profile).where(Profile.name == "Demo profile"))).scalar_one_or_none()
    if profile is None:
        profile = Profile(
            name="Demo profile",
            url="https://example.com",
            description="Seeded demo product.",
            theme_id=theme.id,
            provider_id=provider.id,
            shared_fields=["Name", "URL"],
        )
        session.add(profile)
        await session.flush()
        version = PromptVersion(
            profile_id=profile.id, version_no=1, body=DEFAULT_PROMPT, change_note="Seed", created_by=manager.id
        )
        session.add(version)
        await session.flush()
        profile.active_prompt_version_id = version.id
    if profile is not None:
        existing_assignment = (
            await session.execute(
                select(ProfileAssignment).where(
                    ProfileAssignment.maker_id == maker_one.id, ProfileAssignment.ended_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existing_assignment is None:
            session.add(
                ProfileAssignment(profile_id=profile.id, maker_id=maker_one.id, assigned_by=manager.id)
            )

    if not (
        await session.execute(select(InterviewTemplate).where(InterviewTemplate.is_default.is_(True)))
    ).scalar_one_or_none():
        session.add(
            InterviewTemplate(
                name="Standard technical interview",
                fields=DEFAULT_TEMPLATE_FIELDS,
                is_default=True,
                created_by=manager.id,
            )
        )
    await session.flush()
    settings_row = (await session.execute(select(settings_store.Setting).where(settings_store.Setting.key == "timezone"))).scalar_one_or_none()
    if settings_row is None:
        await settings_store.ensure_defaults(session)
    return {
        "manager": manager.email,
        "maker": maker_one.email,
        "profile": profile.name,
        "theme": DEFAULT_THEME_NAME,
        "password": password,
    }
