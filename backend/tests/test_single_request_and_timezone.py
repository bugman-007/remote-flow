"""One LLM request per resume, and the platform's business timezone."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, date, datetime

import httpx
import pytest
import pytest_asyncio

from app.main import app
from app.models import Generation
from app.services import settings_store
from app.services.llm import LiteLLMClient, LLMError, LLMResult, ProviderConfig, mock_resume, set_client_override
from tests.helpers import initial_build, llm_attempt_count, run_pipeline, submit

VALID = json.dumps(mock_resume("We are hiring at Acme"))


class ScriptedClient:
    """Replies from a script: an exception to raise or a raw string to return."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls += 1
        reply = self.replies[min(self.calls, len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(json={}, raw=reply, usage={}, latency_ms=1, finish_reason="stop")


@pytest_asyncio.fixture
async def fresh_client():
    opened: list[httpx.AsyncClient] = []

    async def login(email: str, password: str):
        from tests.conftest import _ApiClient

        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append(http)
        return await _ApiClient(http).login(email, password)

    yield login
    for http in opened:
        await http.aclose()


async def _run(db_session, workspace, client, **settings):
    if settings:
        await settings_store.set_settings(db_session, settings)
        await db_session.commit()
    set_client_override(client)
    try:
        job = await submit(db_session, workspace["maker"], "We are hiring a backend engineer at Acme to build APIs.")
        await run_pipeline()
    finally:
        set_client_override(None)
    generation = await initial_build(db_session, job)
    await db_session.refresh(generation)
    return generation


# --------------------------------------------------------- one request per resume


@pytest.mark.asyncio
async def test_single_request_mode_never_resends_a_failed_request(db_session, workspace):
    client = ScriptedClient(LLMError("provider_server_error", "503", provider_error=True), VALID)
    generation = await _run(db_session, workspace, client, llm_single_request=True)
    assert client.calls == 1
    assert generation.status == "needs_attention"
    assert await llm_attempt_count(db_session, generation.id) == 1


@pytest.mark.asyncio
async def test_single_request_mode_does_not_repair_an_invalid_reply(db_session, workspace):
    client = ScriptedClient('{"name": "Ada"}', VALID)
    generation = await _run(db_session, workspace, client, llm_single_request=True)
    assert client.calls == 1, "no repair round"
    assert generation.status == "needs_attention"
    assert generation.last_error_code == "invalid_schema"


@pytest.mark.asyncio
async def test_single_request_mode_still_succeeds_in_one_request(db_session, workspace):
    client = ScriptedClient(VALID)
    generation = await _run(db_session, workspace, client, llm_single_request=True)
    assert client.calls == 1
    assert generation.status == "ready"


@pytest.mark.asyncio
async def test_without_single_request_an_invalid_reply_is_repaired(db_session, workspace):
    client = ScriptedClient('{"name": "Ada"}', VALID)
    generation = await _run(db_session, workspace, client)
    assert client.calls == 2
    assert generation.status == "ready"


@pytest.mark.asyncio
async def test_the_managers_attempt_budget_is_honoured(db_session, workspace, fast_settings):
    fast_settings.max_llm_attempts = 10  # the server's ceiling
    client = ScriptedClient(LLMError("provider_server_error", "503", provider_error=True))
    generation = await _run(db_session, workspace, client, max_llm_attempts=2)
    assert client.calls == 2, "Settings -> General said 2, not the server default"
    assert generation.status == "needs_attention"


def _fake_litellm(monkeypatch, *errors):
    calls: list[dict] = []
    queue = list(errors)

    async def acompletion(**kwargs):
        calls.append(kwargs)
        if queue:
            raise queue.pop(0)
        message = types.SimpleNamespace(content=VALID)
        choice = types.SimpleNamespace(message=message, finish_reason="stop")
        return types.SimpleNamespace(choices=[choice], usage=None)

    module = types.ModuleType("litellm")
    module.acompletion = acompletion
    monkeypatch.setitem(sys.modules, "litellm", module)
    return calls


def _provider(**overrides) -> ProviderConfig:
    values = dict(id="p", type="google_gemini", display_name="Gemini", model="gemini-x", api_key="k")
    values.update(overrides)
    return ProviderConfig(**values)


@pytest.mark.asyncio
async def test_single_request_turns_off_the_http_clients_silent_retries(monkeypatch):
    calls = _fake_litellm(monkeypatch)
    await LiteLLMClient().generate_json(provider=_provider(resend=False), system="s", user="u", json_schema={}, timeout_s=5)
    assert calls[0]["num_retries"] == 0 and calls[0]["max_retries"] == 0

    calls = _fake_litellm(monkeypatch)
    await LiteLLMClient().generate_json(provider=_provider(), system="s", user="u", json_schema={}, timeout_s=5)
    assert "max_retries" not in calls[0], "normal mode keeps LiteLLM's own behaviour"


@pytest.mark.asyncio
async def test_single_request_skips_the_json_mode_fallback_call(monkeypatch):
    rejected = Exception("response_format json_schema is not supported")
    calls = _fake_litellm(monkeypatch, rejected)
    with pytest.raises(LLMError):
        await LiteLLMClient().generate_json(provider=_provider(resend=False), system="s", user="u", json_schema={}, timeout_s=5)
    assert len(calls) == 1

    calls = _fake_litellm(monkeypatch, rejected)
    await LiteLLMClient().generate_json(provider=_provider(), system="s", user="u", json_schema={}, timeout_s=5)
    assert len(calls) == 2, "normal mode retries once in JSON-object mode"


# -------------------------------------------------------------------- timezone


def test_timezone_setting_must_be_a_real_region_name():
    assert settings_store.validate_setting("timezone", "America/New_York") == "America/New_York"
    for bad in ("EST", "UTC-5", "Mars/Olympus_Mons"):
        with pytest.raises(settings_store.SettingsError):
            settings_store.validate_setting("timezone", bad)
    assert settings_store.SETTING_DEFAULTS["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_the_business_day_is_the_eastern_day(db_session, workspace, monkeypatch):
    # 02:00 UTC on Sep 30 is still 22:00 on Sep 29 in New York.
    monkeypatch.setattr(settings_store, "utcnow", lambda: datetime(2026, 9, 30, 2, 0, tzinfo=UTC))
    assert await settings_store.platform_today(db_session) == date(2026, 9, 29)
    job = await submit(db_session, workspace["maker"], "We are hiring a backend engineer at Acme to build APIs.")
    assert job.submitted_date == date(2026, 9, 29)


@pytest.mark.asyncio
async def test_the_ui_is_told_the_platform_timezone(api, workspace):
    user = await api.login("maker@example.com", workspace["password"])
    me = await user.get("/api/v1/auth/me")
    assert me.json()["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_meeting_times_without_an_offset_are_eastern(api, workspace, fresh_client):
    manager = await fresh_client("manager@example.com", workspace["password"])
    attachments = []
    for kind, name, mime in (("resume", "cv.pdf", "application/pdf"), ("jd", "role.txt", "text/plain")):
        uploaded = await manager.post(
            "/api/v1/interviews/attachments", data={"kind": kind}, files={"file": (name, b"%PDF-1.4 x", mime)}
        )
        assert uploaded.status_code == 201, uploaded.text
        attachments.append(uploaded.json()["id"])
    created = await manager.post(
        "/api/v1/interviews",
        json={
            "company_name": "Manual Co",
            "job_title": "Designer",
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": "2026-09-28T09:55:00",
            "meeting_tz": "America/New_York",
            "attachment_ids": attachments,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    stored = datetime.fromisoformat(body["meeting_at"].replace("Z", "+00:00"))
    assert stored == datetime(2026, 9, 28, 13, 55, tzinfo=UTC), "09:55 EDT is 13:55 UTC"

    moved = await manager.patch(f"/api/v1/interviews/{body['id']}", json={"meeting_at": "2026-12-01T09:00:00"})
    assert moved.status_code == 200, moved.text
    stored = datetime.fromisoformat(moved.json()["meeting_at"].replace("Z", "+00:00"))
    assert stored == datetime(2026, 12, 1, 14, 0, tzinfo=UTC), "09:00 EST is 14:00 UTC"

    listed = await manager.get(
        "/api/v1/interviews",
        params={"tab": "all", "date_from": "2026-12-01T00:00:00", "date_to": "2026-12-01T23:59:59"},
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]]
