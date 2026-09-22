"""Shared fixtures. Sets the environment before importing the application."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LTMyLWJ5dGVzLWxvbmch")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
import httpx  # noqa: E402

from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.db import Base, configure_engine, create_engine_for, dispose_engine  # noqa: E402


@pytest.fixture(autouse=True)
def fast_settings(tmp_path, monkeypatch):
    """Fast, isolated settings for every test."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    reset_settings_cache()
    settings = get_settings()
    settings.mock_llm_delay_ms_min = 0
    settings.mock_llm_delay_ms_max = 0
    settings.mock_llm_fail_rate = 0.0
    settings.mock_render_fail_rate = 0.0
    settings.llm_timeout_s = 1
    settings.render_timeout_s = 1
    settings.lease_grace_s = 0
    settings.auto_create_schema = False
    from app.services import pipeline, storage

    pipeline.RETRY_BACKOFF_SCALE = 0.0
    pipeline.reset_provider_throttles()
    storage.ensure_dir(settings.storage_path())
    yield settings
    reset_settings_cache()


@pytest_asyncio.fixture
async def engine(fast_settings):
    engine = create_engine_for(fast_settings.database_url)
    configure_engine(engine)
    import app.models  # noqa: F401 - register tables

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


@pytest_asyncio.fixture(autouse=True)
async def clean_broker():
    from app.services.broker import MemoryBroker, set_broker

    set_broker(MemoryBroker())
    yield
    set_broker(None)


@pytest_asyncio.fixture(autouse=True)
async def fake_pdf():
    """LibreOffice is not available in CI; substitute a deterministic PDF writer."""
    from app.services import render

    def converter(docx_path: str, pdf_path: str, *, timeout_s: int = 60) -> str:
        Path(pdf_path).write_bytes(b"%PDF-1.4\n% remote-flow test fixture\n%%EOF\n")
        return pdf_path

    render.set_pdf_converter(converter)
    yield
    render.set_pdf_converter(None)


@pytest_asyncio.fixture
async def db_session(engine):
    from app.db import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def workspace(db_session):
    """Seed the standard demo data and hand back the object graph."""
    from app.services import settings_store
    from app.services.seeds import seed_demo_data
    from app.services.theme import seed_default_theme

    await settings_store.ensure_defaults(db_session)
    await seed_default_theme(db_session)
    info = await seed_demo_data(db_session)
    await db_session.commit()

    from sqlalchemy import select

    from app.models import InterviewTemplate, LLMProvider, Profile, Theme, User

    assigned = (await db_session.execute(select(User).where(User.email == "maker@example.com"))).scalar_one()
    unassigned = (await db_session.execute(select(User).where(User.email == "maker2@example.com"))).scalar_one()
    data = {
        "manager": (await db_session.execute(select(User).where(User.role == "manager"))).scalars().first(),
        "maker": assigned,
        "maker_unassigned": unassigned,
        "makers": [assigned, unassigned],
        "reviewer": (await db_session.execute(select(User).where(User.role == "reviewer"))).scalars().first(),
        "profile": (await db_session.execute(select(Profile))).scalars().first(),
        "provider": (await db_session.execute(select(LLMProvider))).scalars().first(),
        "theme": (await db_session.execute(select(Theme))).scalars().first(),
        "template": (await db_session.execute(select(InterviewTemplate))).scalars().first(),
        "password": info["password"],
    }
    yield data


@pytest_asyncio.fixture
async def client(engine):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


class ApiUser:
    """Small helper that keeps the CSRF header in sync for a logged-in user."""

    def __init__(self, client: httpx.AsyncClient, payload: dict) -> None:
        self.client = client
        self.user = payload["user"]
        self.client.headers["X-CSRF-Token"] = payload["csrf_token"]

    async def get(self, url: str, **kwargs):
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self.client.post(url, **kwargs)

    async def patch(self, url: str, **kwargs):
        return await self.client.patch(url, **kwargs)

    async def put(self, url: str, **kwargs):
        return await self.client.put(url, **kwargs)

    async def delete(self, url: str, **kwargs):
        return await self.client.delete(url, **kwargs)


class _ApiClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def login(self, email: str, password: str, *, as_admin: bool = False) -> ApiUser:
        response = await self.http.post(
            "/api/v1/auth/login", json={"email": email, "password": password, "as_admin": as_admin}
        )
        assert response.status_code == 200, response.text
        return ApiUser(self.http, response.json())


@pytest_asyncio.fixture
async def api(client) -> _ApiClient:
    return _ApiClient(client)
