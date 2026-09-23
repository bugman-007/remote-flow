"""End-to-end API: auth, permissions, submit → ordered release → ZIP (SEC-2…SEC-6, NFR-10)."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.main import app
from app.models import Generation, Job
from app.utils import utcnow
from tests.helpers import run_pipeline

JD = "We are hiring a senior backend engineer at Company API to join the platform team."


@pytest.fixture
async def fresh_client():
    """An isolated cookie jar, for tests that need two sessions at once."""
    opened: list[httpx.AsyncClient] = []

    async def login(email: str, password: str):
        from tests.conftest import _ApiClient

        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append(http)
        return await _ApiClient(http).login(email, password)

    yield login
    for http in opened:
        await http.aclose()


async def submit_via_api(user, text: str = JD, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key else {}
    response = await user.post("/api/v1/jobs", json={"jd_text": text}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_login_me_logout_and_csrf_protection(client, workspace):
    unauthenticated = await client.get("/api/v1/auth/me")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "unauthorized"

    user = await client.post(
        "/api/v1/auth/login", json={"email": "maker@example.com", "password": workspace["password"]}
    )
    assert user.status_code == 200
    body = user.json()
    assert body["user"]["email"] == "maker@example.com"
    assert client.cookies.get("rf_access") and client.cookies.get("rf_csrf")

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "maker"
    assert me.json()["home"] == "/jd-upload"

    # A mutation without the CSRF header is rejected before it reaches the router.
    client.headers["X-CSRF-Token"] = body["csrf_token"]
    del client.headers["X-CSRF-Token"]
    blocked = await client.post("/api/v1/jobs", json={"jd_text": JD})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "csrf_failed"

    client.headers["X-CSRF-Token"] = body["csrf_token"]
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_validation_errors_use_the_error_envelope(api, workspace):
    maker = await api.login("maker@example.com", workspace["password"])
    too_short = await maker.post("/api/v1/jobs", json={"jd_text": "too short"})
    assert too_short.status_code == 422
    assert too_short.json()["error"]["code"] == "invalid_jd"

    malformed = await maker.post("/api/v1/jobs", json={})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_submit_run_pipeline_and_download_the_zip(api, workspace):
    maker = await api.login("maker@example.com", workspace["password"])
    created = await submit_via_api(maker, key="api-e2e-1")
    assert created["seq_no"] == 1
    assert created["created"] is True

    # Idempotency-Key replays the same job instead of creating a second one.
    replay = await submit_via_api(maker, key="api-e2e-1")
    assert replay["job_id"] == created["job_id"]
    assert replay["created"] is False

    await run_pipeline()

    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    assert listing.status_code == 200, listing.text
    rows = listing.json()["items"]
    assert len(rows) == 1
    row = rows[0]
    assert row["seq_no"] == 1
    assert row["status"]["status"] == "released"
    assert row["doc_set"]["company_name"].startswith("Company API")
    assert row["files"] and {f["kind"] for f in row["files"]} >= {"pdf", "docx"}

    doc_set_id = row["doc_set_id"]
    zip_response = await maker.get(f"/api/v1/doc-sets/{doc_set_id}/zip")
    assert zip_response.status_code == 200, zip_response.text
    assert zip_response.headers["content-type"] == "application/zip"
    assert zip_response.headers["X-Zip-Included"] == "1"
    archive = zipfile.ZipFile(io.BytesIO(zip_response.content))
    names = archive.namelist()
    assert len(names) == 3
    assert any(name.endswith(".pdf") for name in names)
    assert any(name.endswith(".docx") for name in names)
    folders = {name.split("/")[0] for name in names}
    assert len(folders) == 1, folders
    folder = folders.pop()
    # RES-13: date-time first so extracted folders sort like the submission order.
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}_", folder), folder
    assert folder.endswith("Company_API_to_join_the_platform_team_Senior_Full_Stack_Developer")

    # Individual file download is authorised for the owning maker.
    pdf = next(f for f in row["files"] if f["kind"] == "pdf")
    download = await maker.get(f"/api/v1/files/{pdf['id']}")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_ordered_release_over_the_api_keeps_submission_order(api, workspace, fast_settings):
    """The queue releases 1..N in order even when build 2 is the slow one."""
    from app.services.llm import LLMError, LLMResult, set_client_override

    valid = (
        '{"name":"Ada","title":"Engineer","target_company":"Acme","summary":"s",'
        '"experience":[{"title":"Engineer","company":"Acme","bullets":["b"]}]}'
    )

    class Client:
        def __init__(self) -> None:
            self.seen = 0

        async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
            self.seen += 1
            if self.seen == 2:  # the second job fails hard
                raise LLMError("provider_server_error", "boom", provider_error=True)
            return LLMResult(json={}, raw=valid, usage={}, latency_ms=1)

    fast_settings.max_llm_attempts = 1
    maker = await api.login("maker@example.com", workspace["password"])
    set_client_override(Client())
    try:
        for index in range(3):
            await submit_via_api(maker, f"role {index} at Company Queue for a senior backend engineer, remote friendly team", key=f"q{index}")
        await run_pipeline()
    finally:
        set_client_override(None)

    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    rows = {row["seq_no"]: row for row in listing.json()["items"]}
    assert rows[1]["status"]["status"] == "released"
    assert rows[2]["status"]["status"] == "needs_attention"
    assert rows[3]["status"]["status"] == "waiting"
    assert rows[3]["status"]["blocker_seq"] == 2

    # Manager skips the blocker; everything ready behind it releases in order.
    manager = await api.login("manager@example.com", workspace["password"])
    job_id = rows[2]["id"]
    skipped = await manager.post(f"/api/v1/jobs/{job_id}/skip")
    assert skipped.status_code == 200, skipped.text

    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    rows = {row["seq_no"]: row for row in listing.json()["items"]}
    assert rows[1]["status"]["status"] == "released"
    assert rows[2]["status"]["status"] == "skipped"
    assert rows[3]["status"]["status"] == "released"


@pytest.mark.asyncio
async def test_makers_only_see_their_own_doc_sets_and_files(api, workspace):
    maker = await api.login("maker@example.com", workspace["password"])
    created = await submit_via_api(maker)
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]
    file_id = row["files"][0]["id"]

    other = await api.login("maker2@example.com", workspace["password"])
    other_listing = await other.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    assert other_listing.status_code == 200
    assert other_listing.json()["items"] == []
    assert (await other.get(f"/api/v1/doc-sets/{row['doc_set_id']}")).status_code == 404
    assert (await other.get(f"/api/v1/files/{file_id}")).status_code == 404
    assert (await other.get("/api/v1/jobs", params={"date": date.today().isoformat()})).json()["items"] == []

    # The maker cannot reach manager-only surfaces.
    assert (await maker.get("/api/v1/settings")).status_code == 403
    assert (await maker.post("/api/v1/retention/dry-run")).status_code == 403
    assert (await maker.post("/api/v1/users/import", json={"csv": "email,role\n"})).status_code == 403


@pytest.mark.asyncio
async def test_reviewers_are_limited_to_their_pinned_generation(api, workspace, db_session, fresh_client):
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker)
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]

    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    assert (await reviewer.get("/api/v1/doc-sets")).status_code == 403
    assert (await reviewer.get("/api/v1/jobs")).status_code == 403
    assert (await reviewer.get(f"/api/v1/files/{row['files'][0]['id']}")).status_code == 404

    # Scheduling an interview pins the generation for that reviewer.
    generation = (
        await db_session.execute(select(Generation).where(Generation.job_id == row["id"]))
    ).scalars().first()
    job = (await db_session.execute(select(Job).where(Job.id == row["id"]))).scalar_one()
    assert job.delivery_status == "released"
    scheduled = await fresh_client("manager@example.com", workspace["password"])
    response = await scheduled.post(
        "/api/v1/interviews",
        json={
            "doc_set_id": row["doc_set_id"],
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": utcnow().isoformat(),
            "values": {},
        },
    )
    assert response.status_code == 201, response.text

    pinned = await reviewer.get(f"/api/v1/files/{row['files'][0]['id']}")
    assert pinned.status_code == 200
    assert (await reviewer.get(f"/api/v1/doc-sets/{row['doc_set_id']}/zip")).status_code == 200
    assert (await reviewer.post("/api/v1/doc-sets/zip", json={"ids": [row["doc_set_id"]]})).status_code == 403
    assert generation is not None


@pytest.mark.asyncio
async def test_health_and_readiness_endpoints(client):
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    ready = await client.get("/readyz")
    assert ready.status_code in {200, 503}


@pytest.mark.asyncio
async def test_system_status_reports_workers_and_retention(api, workspace, db_session):
    from app.services import metrics

    # SET-14: a fresh heartbeat is alive, an old one is stale - this is what the
    # Celery signals write, so a NameError there would silently hide every worker.
    await metrics.record_heartbeat(db_session, name="worker:llm@box", queues=["llm"], concurrency=16, pid=1, started=True)
    stale = await metrics.record_heartbeat(db_session, name="worker:render@box", queues=["render"], concurrency=1, pid=2, started=True)
    stale.last_heartbeat_at = utcnow() - timedelta(minutes=30)
    await db_session.commit()

    manager = await api.login("manager@example.com", workspace["password"])
    status = await manager.get("/api/v1/system/status")
    assert status.status_code == 200, status.text
    payload = status.json()
    assert set(payload) >= {"workers", "sweeps_24h", "retention", "unoserver"}
    assert set(payload["retention"]) == {"expired_generations", "bytes_freed"}
    assert payload["unoserver"]["max_conversions"] == 200
    workers = {worker["name"]: worker for worker in payload["workers"]}
    assert workers["worker:llm@box"]["alive"] is True
    assert workers["worker:llm@box"]["queues"] == ["llm"]
    assert workers["worker:render@box"]["alive"] is False


@pytest.mark.asyncio
async def test_inline_driver_runs_the_pipeline_without_a_broker(api, workspace):
    """`make dev` contract: INLINE_PIPELINE=true processes submissions in-process."""
    from app.workers.inline import InlineDriver

    driver = InlineDriver()
    await driver.start()
    try:
        maker = await api.login("maker@example.com", workspace["password"])
        await submit_via_api(maker, key="inline-1")
        for _ in range(200):
            await asyncio.sleep(0.05)
            listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
            items = listing.json()["items"]
            if items and items[0]["status"]["status"] == "released":
                break
        else:  # pragma: no cover - timing
            pytest.fail("the inline driver never released the job")

        pdf = next(f for f in items[0]["files"] if f["kind"] == "pdf")
        download = await maker.get(f"/api/v1/files/{pdf['id']}")
        assert download.status_code == 200
        assert download.content.startswith(b"%PDF")
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_provider_usage_is_windowed_to_24h(api, workspace, db_session):
    """SET-13: the Providers card is labelled "Usage (24 h)" and must be windowed."""
    from sqlalchemy import update

    from app.models import GenerationAttempt

    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, key="usage-1")
    await run_pipeline()

    manager = await api.login("manager@example.com", workspace["password"])
    fresh = await manager.get("/api/v1/stats/providers")
    assert fresh.status_code == 200, fresh.text
    provider_id = workspace["provider"].id
    fresh_row = next(item for item in fresh.json()["items"] if item["provider_id"] == provider_id)
    assert fresh_row["requests"] >= 1

    # Push every attempt outside the window: the counts must drop back to zero.
    await db_session.execute(update(GenerationAttempt).values(started_at=utcnow() - timedelta(days=3)))
    await db_session.commit()
    aged = await manager.get("/api/v1/stats/providers")
    aged_row = next(item for item in aged.json()["items"] if item["provider_id"] == provider_id)
    assert aged_row["requests"] == 0
    assert aged_row["tokens"] == 0


@pytest.mark.asyncio
async def test_all_stats_endpoints_answer_for_a_manager(api, workspace):
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, key="stats-1")
    await run_pipeline()
    assert (await maker.get("/api/v1/stats/makers")).status_code == 403

    manager = await api.login("manager@example.com", workspace["password"])
    makers = await manager.get("/api/v1/stats/makers")
    assert makers.status_code == 200, makers.text
    rows = {row["maker_id"]: row for row in makers.json()["items"]}
    assert rows[maker.user["id"]]["submitted"] >= 1
    assert rows[maker.user["id"]]["ready"] >= 1

    profiles = await manager.get("/api/v1/stats/profiles")
    assert profiles.status_code == 200, profiles.text
    assert any(row["submitted"] >= 1 for row in profiles.json()["items"])


MANAGER_GET_ROUTES = (
    "/api/v1/auth/me",
    "/api/v1/doc-sets",
    "/api/v1/jobs",
    "/api/v1/me/limit",
    "/api/v1/me/eta",
    "/api/v1/profiles",
    "/api/v1/users",
    "/api/v1/interviews",
    "/api/v1/interview-templates",
    "/api/v1/providers",
    "/api/v1/themes",
    "/api/v1/themes/schema",
    "/api/v1/settings",
    "/api/v1/system/status",
    "/api/v1/system/fonts",
    "/api/v1/stats/makers",
    "/api/v1/stats/profiles",
    "/api/v1/stats/providers",
    "/metrics",
    "/healthz",
    "/readyz",
)


@pytest.mark.asyncio
async def test_manager_get_routes_never_return_a_server_error(api, workspace):
    """Every read-only route the Manager UI loads must answer on a fresh install."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, key="smoke-route-1")
    await run_pipeline()

    manager = await api.login("manager@example.com", workspace["password"])
    broken = []
    for route in MANAGER_GET_ROUTES:
        try:
            response = await manager.get(route)
        except Exception as exc:  # noqa: BLE001 - the test transport re-raises app errors
            broken.append((route, type(exc).__name__, str(exc)[:300]))
            continue
        if response.status_code >= 500:
            broken.append((route, response.status_code, response.text[:300]))
    assert not broken, broken


@pytest.mark.asyncio
async def test_detail_routes_never_return_a_server_error(api, workspace):
    """The Manager/Maker drawers read these with real ids; none may 500."""
    maker = await api.login("maker@example.com", workspace["password"])
    created = await submit_via_api(maker, key="smoke-detail-1")
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]

    manager = await api.login("manager@example.com", workspace["password"])
    interview = await manager.post(
        "/api/v1/interviews",
        json={"doc_set_id": row["doc_set_id"], "reviewer_id": str(workspace["reviewer"].id)},
    )
    assert interview.status_code == 201, interview.text
    interview_id = interview.json()["id"]

    routes = (
        f"/api/v1/doc-sets/{row['doc_set_id']}",
        f"/api/v1/doc-sets/{row['doc_set_id']}/zip",
        f"/api/v1/jobs/{created['job_id']}",
        f"/api/v1/jobs/{created['job_id']}/generations",
        f"/api/v1/jobs/{created['job_id']}/attempts",
        f"/api/v1/profiles/{workspace['profile'].id}",
        f"/api/v1/profiles/{workspace['profile'].id}/prompt-versions",
        f"/api/v1/profiles/{workspace['profile'].id}/assignments",
        f"/api/v1/profiles/{workspace['profile'].id}/doc-sets",
        f"/api/v1/files/{row['files'][0]['id']}",
        f"/api/v1/interviews/{interview_id}",
        f"/api/v1/interviews/{interview_id}/feedback",
        f"/api/v1/doc-sets/zip?date={date.today().isoformat()}",
    )
    broken = []
    for route in routes:
        try:
            response = await manager.get(route)
        except Exception as exc:  # noqa: BLE001
            broken.append((route, type(exc).__name__, str(exc)[:300]))
            continue
        if response.status_code >= 500:
            broken.append((route, response.status_code, response.text[:300]))
    assert not broken, broken


@pytest.mark.asyncio
async def test_manager_mutation_routes_work(api, workspace, fresh_client, db_session):
    """Settings/Users/Providers/Themes/Profiles/Doc-sets write paths used by the UI."""
    failures: list[tuple[str, object, str]] = []

    def check(label: str, response, expected=(200, 201)):
        if isinstance(expected, int):
            expected = (expected,)
        if response.status_code not in expected:
            failures.append((label, response.status_code, response.text[:200]))
        return response

    maker = await api.login("maker@example.com", workspace["password"])
    created = await submit_via_api(maker, key="mut-1")
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]

    manager = await api.login("manager@example.com", workspace["password"])

    # Settings + maintenance
    check("settings.patch", await manager.patch("/api/v1/settings", json={"values": {"default_daily_limit": 12}}))
    values = (await manager.get("/api/v1/settings")).json()["values"]
    if values.get("default_daily_limit") != 12:
        failures.append(("settings.patch.readback", values.get("default_daily_limit"), ""))
    check("intake.pause", await manager.post("/api/v1/system/intake/pause?reason=smoke"))
    check("intake.resume", await manager.post("/api/v1/system/intake/resume"))
    check("retention.dry-run", await manager.post("/api/v1/retention/dry-run"))

    # Doc set actions
    check(
        "docset.patch",
        await manager.patch(
            f"/api/v1/doc-sets/{row['doc_set_id']}", json={"keep": True, "company_name": "Company Renamed"}
        ),
    )
    detail = check("docset.detail", await manager.get(f"/api/v1/doc-sets/{row['doc_set_id']}"))
    assert detail.json()["doc_set"]["keep"] is True
    check("docset.select", await manager.post(f"/api/v1/doc-sets/{row['doc_set_id']}/select"))
    check("docset.deselect", await manager.delete(f"/api/v1/doc-sets/{row['doc_set_id']}/select"))
    check(
        "docset.bulk-select",
        await manager.post("/api/v1/doc-sets/bulk-select", json={"ids": [row["doc_set_id"]], "selected": True}),
    )
    check("docset.zip.post", await manager.post("/api/v1/doc-sets/zip", json={"ids": [row["doc_set_id"]]}))
    check(
        "docset.regenerate",
        await manager.post(f"/api/v1/doc-sets/{row['doc_set_id']}/regenerate"),
    )
    generations = (
        await manager.get(f"/api/v1/jobs/{created['job_id']}/generations")
    ).json()["items"]
    regeneration = next((item for item in generations if item["kind"] == "regenerate"), None)
    assert regeneration is not None, generations
    check("generation.cancel", await manager.post(f"/api/v1/generations/{regeneration['id']}/cancel"))

    # Profiles
    profile = check(
        "profile.create",
        await manager.post(
            "/api/v1/profiles", json={"name": "Smoke Profile", "prompt_body": "Write a resume for the role."}
        ),
    ).json()
    profile_id = profile["id"]
    check("profile.patch", await manager.patch(f"/api/v1/profiles/{profile_id}", json={"description": "smoke"}))
    version = check(
        "profile.prompt-version",
        await manager.post(f"/api/v1/profiles/{profile_id}/prompt-versions", json={"body": "v2 prompt body"}),
    ).json()
    check(
        "profile.prompt-activate",
        await manager.post(f"/api/v1/profiles/{profile_id}/prompt-versions/{version['id']}/activate"),
    )
    check(
        "profile.test",
        await manager.post(f"/api/v1/profiles/{profile_id}/test", json={"jd_text": JD}),
    )
    check(
        "profile.assign",
        await manager.post(
            f"/api/v1/profiles/{profile_id}/assignments", json={"maker_ids": [str(workspace["maker_unassigned"].id)]}
        ),
    )
    assignments = (await manager.get(f"/api/v1/profiles/{profile_id}/assignments")).json()["items"]
    if assignments:
        check(
            "profile.unassign",
            await manager.post(f"/api/v1/profiles/assignments/{assignments[0]['assignment_id']}/end"),
        )
    check("profile.archive", await manager.post(f"/api/v1/profiles/{profile_id}/archive"))

    # Providers
    provider = check(
        "provider.create",
        await manager.post(
            "/api/v1/providers",
            json={"type": "mock", "display_name": "Smoke provider", "default_model": "mock", "api_key": "sk-smoke"},
        ),
    ).json()
    provider_id = provider["id"]
    check("provider.patch", await manager.patch(f"/api/v1/providers/{provider_id}", json={"rpm": 30}))
    check("provider.models", await manager.get(f"/api/v1/providers/{provider_id}/models"))
    check("provider.test", await manager.post(f"/api/v1/providers/{provider_id}/test"))
    check("provider.fallback", await manager.post(f"/api/v1/providers/{provider_id}/set-fallback"))
    check("provider.default", await manager.post(f"/api/v1/providers/{provider_id}/set-default"))
    check("provider.default.back", await manager.post(f"/api/v1/providers/{workspace['provider'].id}/set-default"))

    # Themes
    theme = check(
        "theme.create",
        await manager.post(
            "/api/v1/themes", json={"name": "Smoke theme", "params": {"accent": "#336699"}}
        ),
    ).json()
    theme_id = theme["id"]
    check("theme.patch", await manager.patch(f"/api/v1/themes/{theme_id}", json={"description": "smoke"}))
    check("theme.assign", await manager.post(f"/api/v1/themes/{theme_id}/assign", json={"profile_ids": []}))
    check("theme.preview", await manager.post(f"/api/v1/themes/{theme_id}/preview"))

    # Users
    new_user = check(
        "user.create",
        await manager.post(
            "/api/v1/users",
            json={"name": "Smoke User", "email": "smoke.user@example.com", "role": "maker", "daily_limit": 5},
        ),
    ).json()
    user_id = new_user["user"]["id"]
    check("user.patch", await manager.patch(f"/api/v1/users/{user_id}", json={"daily_limit": 7}))
    check("user.reset-password", await manager.post(f"/api/v1/users/{user_id}/reset-password"))
    check("user.revoke-sessions", await manager.post(f"/api/v1/users/{user_id}/revoke-sessions"))
    check("user.deactivate", await manager.post(f"/api/v1/users/{user_id}/deactivate"))
    check("user.reactivate", await manager.post(f"/api/v1/users/{user_id}/reactivate"))
    check("user.import", await manager.post("/api/v1/users/import", json={"csv": "email,role\nsmoke2@example.com,maker\n"}))

    # Interview templates + lifecycle
    template = check(
        "template.create",
        await manager.post("/api/v1/interview-templates", json={"name": "Smoke template", "fields": []}),
    ).json()
    check(
        "template.put",
        await manager.put(
            f"/api/v1/interview-templates/{template['id']}", json={"name": "Smoke template v2", "fields": []}
        ),
    )
    interview = check(
        "interview.create",
        await manager.post(
            "/api/v1/interviews",
            json={"doc_set_id": row["doc_set_id"], "reviewer_id": str(workspace["reviewer"].id), "values": {"notes": "x"}},
        ),
    ).json()
    interview_id = interview["id"]
    check("interview.patch", await manager.patch(f"/api/v1/interviews/{interview_id}", json={"meeting_tz": "UTC"}))
    check("interview.seen", await manager.post(f"/api/v1/interviews/{interview_id}/seen"))
    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    check(
        "interview.feedback",
        await reviewer.post(
            f"/api/v1/interviews/{interview_id}/feedback", json={"outcome": "pass", "rating": 4, "notes": "good"}
        ),
    )
    check("interview.feedback.get", await reviewer.get(f"/api/v1/interviews/{interview_id}/feedback"))
    check(
        "interview.duplicate",
        await manager.post(f"/api/v1/interviews/{interview_id}/duplicate", json={"reviewer_id": str(workspace["reviewer"].id)}),
    )
    check(
        "interview.use-generation",
        await manager.post(
            f"/api/v1/interviews/{interview_id}/use-generation",
            json={"generation_id": row["doc_set"]["current_generation_id"]},
        ),
    )
    check("interview.cancel", await manager.post(f"/api/v1/interviews/{interview_id}/cancel"))
    check("template.delete", await manager.delete(f"/api/v1/interview-templates/{template['id']}"))

    # Job actions: force a retryable state, retry it, then skip it
    generation = (
        await db_session.execute(select(Generation).where(Generation.job_id == created["job_id"]))
    ).scalars().first()
    generation.status = "needs_attention"
    generation.stage = "llm"
    generation.dispatch_state = "pending"
    generation.last_error_code = "provider_server_error"
    generation.lease_token = None
    generation.claimed_by = None
    generation.lease_expires_at = None
    await db_session.commit()
    check("job.retry", await manager.post(f"/api/v1/jobs/{created['job_id']}/retry", json={"mode": "render_only"}))
    check("job.skip", await manager.post(f"/api/v1/jobs/{created['job_id']}/skip"))

    assert not failures, failures


@pytest.mark.asyncio
async def test_security_headers_and_manager_ip_allowlist(api, workspace, fast_settings):
    """SEC-8 (headers) and SEC-9 (optional manager IP allow-list)."""
    from app.config import get_settings

    anonymous = await api.http.get("/api/v1/auth/me") if hasattr(api, "http") else None
    if anonymous is not None:
        assert anonymous.status_code == 401
        assert anonymous.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in anonymous.headers["Content-Security-Policy"]
        assert anonymous.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    manager = await api.login("manager@example.com", workspace["password"])
    settings = get_settings()
    original = settings.manager_ip_allowlist
    try:
        settings.manager_ip_allowlist = "10.0.0.0/8"
        blocked = await manager.get("/api/v1/users")
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["error"]["code"] == "ip_not_allowed"
        # Non-manager endpoints are unaffected.
        assert (await manager.get("/api/v1/jobs")).status_code == 200

        settings.manager_ip_allowlist = "127.0.0.1"
        assert (await manager.get("/api/v1/users")).status_code == 200
    finally:
        settings.manager_ip_allowlist = original


@pytest.mark.asyncio
async def test_interview_ics_download(api, workspace, fresh_client):
    """INT-8: an interview downloads as a valid .ics only for its manager and reviewer."""
    from datetime import datetime, timezone

    maker = await api.login("maker@example.com", workspace["password"])
    created = await submit_via_api(maker, key="ics-1")
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]
    manager = await api.login("manager@example.com", workspace["password"])
    meeting_at = datetime(2030, 1, 2, 15, 30, tzinfo=timezone.utc)
    interview = (
        await manager.post(
            "/api/v1/interviews",
            json={
                "doc_set_id": row["doc_set_id"],
                "reviewer_id": str(workspace["reviewer"].id),
                "meeting_at": meeting_at.isoformat(),
                "values": {"location": "Room 1, floor 2", "meeting_link": "https://meet.example.com/x"},
            },
        )
    ).json()

    response = await manager.get(f"/api/v1/interviews/{interview['id']}/ics")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/calendar")
    body = response.text
    assert "BEGIN:VCALENDAR" in body and "END:VCALENDAR" in body
    assert "DTSTART:20300102T153000Z" in body
    assert "DTEND:20300102T163000Z" in body
    assert "Room 1\\, floor 2" in body  # RFC 5545 escaping
    assert "https://meet.example.com/x" in body

    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    assert (await reviewer.get(f"/api/v1/interviews/{interview['id']}/ics")).status_code == 200

    maker_client = await fresh_client("maker@example.com", workspace["password"])
    assert (await maker_client.get(f"/api/v1/interviews/{interview['id']}/ics")).status_code == 404
    assert created["job_id"]


@pytest.mark.asyncio
async def test_doc_set_access_and_downloads_are_audited(api, workspace, db_session):
    """SEC-7: opening a doc set and downloading its documents are recorded."""
    from app.models import AuditLog

    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, key="audit-1")
    await run_pipeline()
    listing = await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})
    row = listing.json()["items"][0]

    detail = await maker.get(f"/api/v1/doc-sets/{row['doc_set_id']}")
    assert detail.status_code == 200, detail.text
    files = [file for generation in detail.json()["generations"] for file in generation["files"]]
    pdf = next(file for file in files if file["kind"] == "pdf")
    assert (await maker.get(f"/api/v1/files/{pdf['id']}")).status_code == 200
    assert (await maker.get(f"/api/v1/doc-sets/{row['doc_set_id']}/zip")).status_code == 200

    actions = (
        await db_session.execute(select(AuditLog.action).where(AuditLog.entity_id == row["doc_set_id"]))
    ).scalars().all()
    assert "docset.view" in actions
    assert "docset.download" in actions
    file_actions = (
        await db_session.execute(select(AuditLog.action).where(AuditLog.entity_id == pdf["id"]))
    ).scalars().all()
    assert "file.download" in file_actions
    await db_session.rollback()


@pytest.mark.asyncio
async def test_maker_limit_exposes_jd_bounds(api, workspace):
    """JD-3: the Maker UI reads the real bounds instead of a hardcoded cap."""
    maker = await api.login("maker@example.com", workspace["password"])
    body = (await maker.get("/api/v1/me/limit")).json()
    assert body["min_jd_chars"] == 50
    assert body["max_jd_chars"] >= 200_000


@pytest.mark.asyncio
async def test_doc_sets_sort_by_submitted_time(api, workspace):
    """RES-13: Makers can sort by submission time, not only by ready time."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, "Sort check one: hiring a backend engineer at Company Sort One.", key="sort-1")
    await submit_via_api(maker, "Sort check two: hiring a data engineer at Company Sort Two, remote.", key="sort-2")
    await run_pipeline()

    def seqs(payload):
        return [row["seq_no"] for row in payload["items"]]

    params = {"date": date.today().isoformat(), "sort": "submitted"}
    newest = (await maker.get("/api/v1/doc-sets", params={**params, "order": "desc"})).json()
    oldest = (await maker.get("/api/v1/doc-sets", params={**params, "order": "asc"})).json()
    assert seqs(newest) == [2, 1]
    assert seqs(oldest) == [1, 2]


@pytest.mark.asyncio
async def test_maker_new_filter_clears_after_download(api, workspace):
    """RES-14: "New" lists never-downloaded sets and clears once the Maker takes them."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, "New filter: hiring a platform engineer at Company Filter One.", key="new-1")
    await run_pipeline()

    day = {"date": date.today().isoformat()}
    fresh = (await maker.get("/api/v1/doc-sets", params={**day, "status": "new"})).json()
    assert len(fresh["items"]) == 1
    row = fresh["items"][0]
    assert row["downloaded_at"] is None

    response = await maker.get(f"/api/v1/doc-sets/{row['doc_set_id']}/zip")
    assert response.status_code == 200, response.text

    after = (await maker.get("/api/v1/doc-sets", params={**day, "status": "new"})).json()
    assert after["items"] == []
    all_rows = (await maker.get("/api/v1/doc-sets", params=day)).json()["items"]
    assert all_rows[0]["downloaded_at"] is not None


@pytest.mark.asyncio
async def test_date_zip_accepts_a_range(api, workspace):
    """RES-1: "This week"/"This month" downloads span a date range, not a single day."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, "Range zip: hiring a designer at Company Range One.", key="range-1")
    await run_pipeline()

    today = date.today().isoformat()
    response = await maker.get("/api/v1/doc-sets/zip", params={"date_from": today, "date_to": today})
    assert response.status_code == 200, response.text
    assert response.headers["X-Zip-Included"] == "1"


@pytest.mark.asyncio
async def test_reviewer_tabs_and_overview_counts(api, workspace, fresh_client):
    """INT-4: reviewers get All/Upcoming/Done tabs plus an overview count block."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, "Interview tabs: hiring a QA engineer at Company Tabs One.", key="tabs-1")
    await run_pipeline()
    row = (await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})).json()["items"][0]

    manager = await fresh_client("manager@example.com", workspace["password"])
    created = await manager.post(
        "/api/v1/interviews",
        json={
            "doc_set_id": row["doc_set_id"],
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": utcnow().isoformat(),
            "values": {"meeting_end_time": "11:30"},
        },
    )
    assert created.status_code == 201, created.text
    interview_id = created.json()["id"]

    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    todo = (await reviewer.get("/api/v1/interviews", params={"tab": "todo"})).json()
    assert [item["id"] for item in todo["items"]] == [interview_id]
    assert todo["counts"] == {"total": 1, "todo": 1, "done": 0}
    assert todo["items"][0]["values"]["meeting_end_time"] == "11:30"

    assert (await reviewer.get("/api/v1/interviews", params={"tab": "done"})).json()["items"] == []

    feedback = await reviewer.post(
        f"/api/v1/interviews/{interview_id}/feedback",
        json={"outcome": "pass", "rating": 4, "notes": "Solid candidate."},
    )
    assert feedback.status_code == 200, feedback.text

    done = (await reviewer.get("/api/v1/interviews", params={"tab": "done"})).json()
    assert [item["id"] for item in done["items"]] == [interview_id]
    assert done["counts"] == {"total": 1, "todo": 0, "done": 1}
    assert (await reviewer.get("/api/v1/interviews", params={"tab": "all"})).json()["counts"]["total"] == 1


@pytest.mark.asyncio
async def test_interview_date_range_filter(api, workspace, fresh_client):
    """INT-4: the table's date filter spans a from/to window."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, "Interview date filter: hiring a PM at Company Dates One.", key="dates-1")
    await run_pipeline()
    row = (await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})).json()["items"][0]

    manager = await fresh_client("manager@example.com", workspace["password"])
    created = await manager.post(
        "/api/v1/interviews",
        json={
            "doc_set_id": row["doc_set_id"],
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": "2030-01-15T10:00:00+00:00",
        },
    )
    assert created.status_code == 201, created.text

    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    inside = await reviewer.get(
        "/api/v1/interviews",
        params={"tab": "all", "date_from": "2030-01-01T00:00:00", "date_to": "2030-01-31T23:59:59"},
    )
    outside = await reviewer.get(
        "/api/v1/interviews",
        params={"tab": "all", "date_from": "2031-01-01T00:00:00", "date_to": "2031-01-31T23:59:59"},
    )
    assert [item["id"] for item in inside.json()["items"]] == [created.json()["id"]]
    assert outside.json()["items"] == []


@pytest.mark.asyncio
async def test_maker_sees_their_shared_profile(api, workspace):
    """PRO-2: a Maker can read the shared fields of the assigned Profile and nothing else."""
    maker = await api.login("maker@example.com", workspace["password"])
    body = (await maker.get("/api/v1/me/profile")).json()
    assert body["profile"] is not None
    assert body["profile"]["name"] == workspace["profile"].name
    # Secrets/LLM configuration are never part of the shared payload.
    assert "model" not in body["profile"]
    assert "active_prompt" not in body["profile"]
    assert "id" in body["profile"]

    reviewer = await api.login("reviewer@example.com", workspace["password"])
    assert (await reviewer.get("/api/v1/me/profile")).status_code == 403


@pytest.mark.asyncio
async def test_shared_profile_uses_the_information_field(api, workspace, fresh_client, db_session):
    """PRO-2: sharing "Information" exposes the description (the renamed Description tab)."""
    manager = await fresh_client("manager@example.com", workspace["password"])
    profile = workspace["profile"]
    response = await manager.patch(
        f"/api/v1/profiles/{profile.id}",
        json={"description": "Senior backend engineer, remote.", "shared_fields": ["Name", "Information"]},
    )
    assert response.status_code == 200, response.text

    maker = await api.login("maker@example.com", workspace["password"])
    shared = (await maker.get("/api/v1/me/profile")).json()["profile"]
    assert shared["description"] == "Senior backend engineer, remote."


@pytest.mark.asyncio
async def test_prompt_test_can_render_a_pdf(api, workspace, fresh_client):
    """LLM-5: the Prompt tab's test run can come back as JSON or as a rendered PDF."""
    manager = await fresh_client("manager@example.com", workspace["password"])
    profile_id = workspace["profile"].id

    as_json = await manager.post(
        f"/api/v1/profiles/{profile_id}/test",
        json={"jd_text": "We are hiring a backend engineer to build APIs."},
    )
    assert as_json.status_code == 200, as_json.text
    assert "json" in as_json.json()

    as_pdf = await manager.post(
        f"/api/v1/profiles/{profile_id}/test/pdf",
        json={"jd_text": "We are hiring a backend engineer to build APIs."},
    )
    assert as_pdf.status_code == 200, as_pdf.text
    assert as_pdf.headers["content-type"] == "application/pdf"
    assert as_pdf.content.startswith(b"%PDF")
    assert "attachment" in as_pdf.headers["content-disposition"]


@pytest.mark.asyncio
async def test_theme_preview_renders_unsaved_params(api, workspace, fresh_client):
    """SET-10: the theme dialog previews edits before they are saved."""
    manager = await fresh_client("manager@example.com", workspace["password"])
    response = await manager.post(
        "/api/v1/themes/preview",
        json={"params": {"font": "Inter", "size": 11, "accent": "#ff0000"}},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")

    bad = await manager.post("/api/v1/themes/preview", json={"params": {"size": "huge"}})
    assert bad.status_code == 422, bad.text


async def _scheduled_interview(api, workspace, fresh_client, client_key: str = "p3-1"):
    """Helper: submit, run and schedule one interview; returns (manager, row, interview)."""
    maker = await api.login("maker@example.com", workspace["password"])
    await submit_via_api(maker, f"Phase three: hiring a staff engineer at Company {client_key}.", key=client_key)
    await run_pipeline()
    row = (await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})).json()["items"][0]
    manager = await fresh_client("manager@example.com", workspace["password"])
    created = await manager.post(
        "/api/v1/interviews",
        json={
            "doc_set_id": row["doc_set_id"],
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": "2030-05-01T09:00:00+00:00",
            "tech_stack": "Python, Postgres",
        },
    )
    assert created.status_code == 201, created.text
    return manager, row, created.json()


@pytest.mark.asyncio
async def test_interview_taxonomy_crud(api, workspace, fresh_client):
    """INT-14: managers own the ordered step and status lists."""
    manager = await fresh_client("manager@example.com", workspace["password"])
    taxonomy = (await manager.get("/api/v1/interview-taxonomy")).json()
    assert [step["name"] for step in taxonomy["steps"]][:4] == [
        "Phone call",
        "Initial meeting",
        "Tech meeting",
        "Final meeting",
    ]
    assert {row["name"] for row in taxonomy["statuses"]} >= {"Scheduled", "Rescheduled", "Done", "Cancelled", "Rejected"}

    created = await manager.post("/api/v1/interview-steps", json={"name": "Take-home review", "color": "#123456"})
    assert created.status_code == 201, created.text
    step_id = created.json()["id"]
    assert created.json()["position"] == 4

    clash = await manager.post("/api/v1/interview-steps", json={"name": "Take-home review"})
    assert clash.status_code == 409

    renamed = await manager.patch(f"/api/v1/interview-steps/{step_id}", json={"name": "Take-home", "position": 0})
    assert renamed.json()["name"] == "Take-home"
    assert renamed.json()["position"] == 0

    assert (await manager.delete(f"/api/v1/interview-steps/{step_id}")).status_code == 200
    assert (await manager.get("/api/v1/interview-steps")).json()["items"][0]["name"] == "Phone call"

    status = await manager.post("/api/v1/interview-statuses", json={"name": "Offer", "color": "#00ff00"})
    assert status.status_code == 201
    assert (await manager.delete(f"/api/v1/interview-statuses/{status.json()['id']}")).status_code == 200

    maker = await fresh_client("maker@example.com", workspace["password"])
    assert (await maker.get("/api/v1/interview-taxonomy")).status_code == 403


@pytest.mark.asyncio
async def test_interview_steps_done_and_rejected(api, workspace, fresh_client):
    """INT-15: Done/Rejected checkboxes drive the status label and the history."""
    manager, _row, interview = await _scheduled_interview(api, workspace, fresh_client, "p3-steps")
    assert interview["status_label"]["name"] == "Scheduled"
    assert len(interview["steps"]) == 1
    first = interview["steps"][0]
    assert first["step_name"] == "Phone call"
    assert first["done"] is False

    done = await manager.patch(
        f"/api/v1/interviews/{interview['id']}/steps/{first['id']}", json={"done": True}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status_label"]["name"] == "Done"
    assert done.json()["status"] == "completed"
    assert done.json()["steps"][0]["done_at"] is not None

    # Adding the next step automatically un-checks Done and returns to Scheduled.
    steps = (await manager.get("/api/v1/interview-steps")).json()["items"]
    tech = next(step for step in steps if step["name"] == "Tech meeting")
    added = await manager.post(
        f"/api/v1/interviews/{interview['id']}/steps",
        json={"step_id": tech["id"], "reviewer_id": str(workspace["reviewer"].id)},
    )
    assert added.status_code == 201, added.text
    body = added.json()
    assert body["status_label"]["name"] == "Scheduled"
    assert body["status"] == "scheduled"
    assert [step["step_name"] for step in body["steps"]] == ["Phone call", "Tech meeting"]
    # The earlier step keeps the record that it was handled.
    assert body["steps"][0]["done"] is True

    current = body["steps"][1]
    rejected = await manager.patch(
        f"/api/v1/interviews/{interview['id']}/steps/{current['id']}", json={"rejected": True}
    )
    assert rejected.json()["status_label"]["name"] == "Rejected"
    assert rejected.json()["steps"][1]["rejected"] is True
    assert rejected.json()["steps"][1]["done"] is False

    cleared = await manager.patch(
        f"/api/v1/interviews/{interview['id']}/steps/{current['id']}", json={"rejected": False}
    )
    assert cleared.json()["status_label"]["name"] == "Scheduled"


@pytest.mark.asyncio
async def test_interview_filter_bar(api, workspace, fresh_client):
    """INT-14: Step x All/New/Done x Profile x search filter the manager table."""
    manager, row, interview = await _scheduled_interview(api, workspace, fresh_client, "p3-filter")
    steps = (await manager.get("/api/v1/interview-steps")).json()["items"]
    phone = next(step for step in steps if step["name"] == "Phone call")

    params = {"flow": "new", "step_id": phone["id"]}
    fresh = (await manager.get("/api/v1/interviews", params=params)).json()
    assert [item["id"] for item in fresh["items"]] == [interview["id"]]
    assert fresh["items"][0]["tech_stack"] == "Python, Postgres"

    other = next(step for step in steps if step["name"] == "Final meeting")
    assert (await manager.get("/api/v1/interviews", params={"step_id": other["id"]})).json()["items"] == []

    profile_id = (await manager.get("/api/v1/profiles")).json()["items"][0]["id"]
    assert len((await manager.get("/api/v1/interviews", params={"profile_id": profile_id})).json()["items"]) == 1
    assert (await manager.get("/api/v1/interviews", params={"q": "Company p3-filter"})).json()["items"] != []
    assert (await manager.get("/api/v1/interviews", params={"q": "nothing-matches"})).json()["items"] == []

    first = interview["steps"][0]
    await manager.patch(f"/api/v1/interviews/{interview['id']}/steps/{first['id']}", json={"done": True})
    assert (await manager.get("/api/v1/interviews", params={"flow": "new"})).json()["items"] == []
    assert len((await manager.get("/api/v1/interviews", params={"flow": "done"})).json()["items"]) == 1


@pytest.mark.asyncio
async def test_manual_interview_with_attachments(api, workspace, fresh_client):
    """INT-16: Create New attaches a resume and a JD as-is, with no pipeline run."""
    manager = await fresh_client("manager@example.com", workspace["password"])

    resume = await manager.post(
        "/api/v1/interviews/attachments",
        data={"kind": "resume"},
        files={"file": ("candidate.pdf", b"%PDF-1.4 resume", "application/pdf")},
    )
    assert resume.status_code == 201, resume.text
    jd = await manager.post(
        "/api/v1/interviews/attachments",
        data={"kind": "jd"},
        files={"file": ("role.txt", b"Hiring a designer.", "text/plain")},
    )
    assert jd.status_code == 201, jd.text

    bad = await manager.post(
        "/api/v1/interviews/attachments",
        data={"kind": "resume"},
        files={"file": ("resume.exe", b"nope", "application/octet-stream")},
    )
    assert bad.status_code == 422

    created = await manager.post(
        "/api/v1/interviews",
        json={
            "company_name": "Manual Co",
            "job_title": "Product Designer",
            "reviewer_id": str(workspace["reviewer"].id),
            "meeting_at": "2030-06-01T14:00:00+00:00",
            "tech_stack": "Figma",
            "attachment_ids": [resume.json()["id"], jd.json()["id"]],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["doc_set"] is None
    assert body["company_name"] == "Manual Co"
    assert body["job_title"] == "Product Designer"
    assert {item["kind"] for item in body["attachments"]} == {"resume", "jd"}

    download = await manager.get(f"/api/v1/interviews/{body['id']}/attachments/{resume.json()['id']}")
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 resume"

    reviewer = await fresh_client("reviewer@example.com", workspace["password"])
    listed = (await reviewer.get("/api/v1/interviews", params={"tab": "all"})).json()["items"]
    assert body["id"] in [item["id"] for item in listed]
    assert (await reviewer.get(f"/api/v1/interviews/{body['id']}/attachments/{resume.json()['id']}")).status_code == 200

    missing = await manager.post(
        "/api/v1/interviews",
        json={"company_name": "No files", "job_title": "Nope", "attachment_ids": []},
    )
    assert missing.status_code == 422


@pytest.mark.asyncio
async def test_users_page_cards_and_information(api, workspace, fresh_client):
    """USR-9: the Users page lists card stats and stores the Manager's Information note."""
    manager = await fresh_client("manager@example.com", workspace["password"])
    maker_id = str(workspace["maker"].id)

    listed = (await manager.get("/api/v1/users", params={"role": "maker"})).json()["items"]
    maker = next(row for row in listed if row["id"] == maker_id)
    assert maker["stats"] is not None
    for key in ("total_resumes", "resumes_today", "interviews", "interview_pct"):
        assert key in maker["stats"]
    assert maker["info"] is None

    updated = await manager.patch(
        f"/api/v1/users/{maker_id}",
        json={"info": "Phone 555-0100 · Berlin", "profile_id": str(workspace["profile"].id)},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["info"] == "Phone 555-0100 · Berlin"

    # The profile move goes through the assignment table (the edit dialog used to 422 here).
    listed = (await manager.get("/api/v1/users", params={"role": "maker"})).json()["items"]
    maker = next(row for row in listed if row["id"] == maker_id)
    assert maker["profile_id"] == str(workspace["profile"].id)

    reviewer_id = str(workspace["reviewer"].id)
    reviewer = next(
        row
        for row in (await manager.get("/api/v1/users", params={"role": "reviewer"})).json()["items"]
        if row["id"] == reviewer_id
    )
    assert {"reviews_today", "total_interviews", "by_step"} <= set(reviewer["stats"])


@pytest.mark.asyncio
async def test_reviewer_stats_count_interview_steps(api, workspace, fresh_client):
    """USR-9: a reviewer's card overview is built from the per-step interview history."""
    manager, row, interview = await _scheduled_interview(api, workspace, fresh_client, "p4-stats")
    steps = (await manager.get("/api/v1/interview-steps")).json()["items"]
    tech = next(step for step in steps if step["name"] == "Tech meeting")
    first = interview["steps"][0]
    await manager.patch(
        f"/api/v1/interviews/{interview['id']}/steps/{first['id']}",
        json={"done": True, "reviewer_id": str(workspace["reviewer"].id)},
    )
    await manager.post(
        f"/api/v1/interviews/{interview['id']}/steps",
        json={"step_id": tech["id"], "reviewer_id": str(workspace["reviewer"].id)},
    )

    reviewer_id = str(workspace["reviewer"].id)
    reviewer = next(
        item
        for item in (await manager.get("/api/v1/users", params={"role": "reviewer"})).json()["items"]
        if item["id"] == reviewer_id
    )
    assert reviewer["stats"]["total_interviews"] >= 1
    assert reviewer["stats"]["reviews_today"] >= 1
    names = [entry["name"] for entry in reviewer["stats"]["by_step"]]
    assert "Phone call" in names and "Tech meeting" in names


@pytest.mark.asyncio
async def test_theme_import_from_resume(api, workspace, fresh_client):
    """SET-11: an uploaded DOCX is turned into theme params the Manager can tweak."""
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    document = Document()
    run = document.add_paragraph().add_run("Jane Doe")
    run.font.name = "Georgia"
    run.font.size = Pt(14)
    run.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
    body = document.add_paragraph().add_run("Backend engineer with API experience.")
    body.font.name = "Georgia"
    body.font.size = Pt(11)
    section = document.sections[0]
    section.top_margin = Inches(0.8)
    section.left_margin = Inches(0.9)
    buffer = io.BytesIO()
    document.save(buffer)

    manager = await fresh_client("manager@example.com", workspace["password"])
    response = await manager.post(
        "/api/v1/themes/import-resume",
        files={
            "file": (
                "jane_resume.docx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["params"]["font"] == "Georgia"
    assert body["params"]["size"] == 11.0
    assert body["params"]["accent"] == "#1F4E79"
    assert body["params"]["margin_top"] == 0.8
    assert body["params"]["margin_left"] == 0.9
    assert "jane resume" in body["name"]

    rejected = await manager.post(
        "/api/v1/themes/import-resume",
        files={"file": ("resume.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert rejected.status_code == 422
