"""Profile groups (PRO-11), Bulk Resumes (BULK-1) and automatic duplicate skips."""

from __future__ import annotations

import csv
import io
import json
from datetime import date

import pytest
from sqlalchemy import select

from app.models import Job, Profile, ProfileAssignment, User
from app.services import bulk
from app.services.llm import LLMResult, mock_resume, set_client_override
from app.services.storage import storage_root
from tests.helpers import run_pipeline, submit


def jd(n: int) -> str:
    return f"Job {n}: we are hiring a senior backend engineer at Company {n} to build APIs and data pipelines."


def make_csv(rows: list[tuple[str, str]], header=("Job Links", "JD")) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


class CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_json(self, *, provider, system, user, json_schema, timeout_s):
        self.calls += 1
        return LLMResult(json={}, raw=json.dumps(mock_resume(user)), usage={}, latency_ms=1, finish_reason="stop")


async def _second_profile(db_session, *, with_maker: User | None, name: str) -> Profile:
    base = (await db_session.execute(select(Profile))).scalars().first()
    profile = Profile(
        name=name,
        theme_id=base.theme_id,
        provider_id=base.provider_id,
        active_prompt_version_id=base.active_prompt_version_id,
    )
    db_session.add(profile)
    await db_session.flush()
    if with_maker is not None:
        db_session.add(ProfileAssignment(profile_id=profile.id, maker_id=with_maker.id))
    await db_session.commit()
    return profile


# --------------------------------------------------------------------- groups


@pytest.mark.asyncio
async def test_groups_can_be_saved_renamed_emptied_and_deleted(api, workspace, db_session):
    other = await _second_profile(db_session, with_maker=None, name="Second Profile")
    first_id = str(workspace["profile"].id)
    manager = await api.login("manager@example.com", workspace["password"])

    created = await manager.post("/api/v1/profile-groups", json={"name": "Backend", "profile_ids": [first_id]})
    assert created.status_code == 201, created.text
    group = created.json()
    assert group["color"].startswith("#") and group["profile_ids"] == [first_id]

    # Saving under an existing name adds to that group.
    again = await manager.post("/api/v1/profile-groups", json={"name": "backend", "profile_ids": [other.id]})
    assert again.json()["id"] == group["id"] and again.json()["profile_count"] == 2

    # A profile belongs to one group: a new group takes it away from the old one.
    moved = await manager.post("/api/v1/profile-groups", json={"name": "Frontend", "profile_ids": [other.id]})
    assert moved.json()["profile_ids"] == [other.id]
    assert moved.json()["color"] != group["color"]
    listed = (await manager.get("/api/v1/profiles")).json()["items"]
    by_id = {item["id"]: item for item in listed}
    assert by_id[first_id]["group"]["name"] == "Backend"
    assert by_id[other.id]["group"]["name"] == "Frontend"

    renamed = await manager.patch(f"/api/v1/profile-groups/{group['id']}", json={"name": "Platform"})
    assert renamed.json()["name"] == "Platform"
    clash = await manager.patch(f"/api/v1/profile-groups/{group['id']}", json={"name": "Frontend"})
    assert clash.status_code == 409

    removed = await manager.post(f"/api/v1/profile-groups/{group['id']}/remove", json={"profile_ids": [first_id]})
    assert removed.json()["profile_ids"] == []
    deleted = await manager.delete(f"/api/v1/profile-groups/{moved.json()['id']}")
    assert deleted.status_code == 200
    listed = (await manager.get("/api/v1/profiles")).json()["items"]
    assert all(item["group"] is None for item in listed)

    maker = await api.login("maker@example.com", workspace["password"])
    assert (await maker.get("/api/v1/profile-groups")).status_code == 403


# ------------------------------------------------------------------- CSV parse


def test_csv_rows_need_a_valid_link_and_a_jd():
    data = make_csv([
        ("https://jobs.example.com/1", jd(1)),
        ("", jd(2)),
        ("https://jobs.example.com/3", ""),
        ("javascript:alert(1)", jd(4)),
        ("", ""),
        ("https://jobs.example.com/5", "short"),
    ])
    rows, rejected = bulk.parse_csv(data, min_chars=50, max_chars=200000)
    assert [row["row"] for row in rows] == [2]
    assert [(item["row"], item["reason"]) for item in rejected] == [
        (3, "missing job link"),
        (4, "missing JD"),
        (5, "invalid job link"),
        (7, "JD shorter than 50 characters"),
    ]
    with pytest.raises(bulk.BulkError):
        bulk.parse_csv(make_csv([("x", "y")], header=("Link", "Description")), min_chars=1, max_chars=10)


def test_semicolon_csv_and_multiline_jds_are_read():
    text = 'Job Links;JD\nhttps://a.example/1;"line one of a long job description\nline two of it, hiring engineers now"\n'
    rows, rejected = bulk.parse_csv(text.encode("utf-8"), min_chars=10, max_chars=1000)
    assert not rejected and "line two" in rows[0]["jd"]


# ---------------------------------------------------------------- generation


@pytest.mark.asyncio
async def test_bulk_generates_the_first_n_rows_per_profile_and_fills_past_duplicates(api, workspace, db_session):
    maker = workspace["maker"]
    maker_id, profile_id = maker.id, workspace["profile"].id
    maker.daily_limit = 1  # bulk jobs never count against it
    await db_session.commit()
    no_maker = await _second_profile(db_session, with_maker=None, name="Unassigned Profile")
    # The Maker already has JD 2 from earlier: it is a duplicate for this profile.
    await submit(db_session, maker, jd(2))

    data = make_csv([
        ("https://jobs.example.com/1", jd(1)),
        ("https://jobs.example.com/2", jd(2)),          # history duplicate
        ("https://jobs.example.com/1/", jd(30)),        # same link as row 2 -> CSV duplicate
        ("https://jobs.example.com/4", jd(1)),          # same JD as row 2 -> CSV duplicate
        ("https://jobs.example.com/5", jd(5)),
        ("https://jobs.example.com/6", jd(6)),
        ("https://jobs.example.com/7", jd(7)),
    ])
    manager = await api.login("manager@example.com", workspace["password"])
    uploaded = await manager.post("/api/v1/bulk-resumes/upload", files={"file": ("jobs.csv", data, "text/csv")})
    assert uploaded.status_code == 201, uploaded.text
    batch = uploaded.json()
    assert batch["usable_rows"] == 7

    summary = await manager.post(
        f"/api/v1/bulk-resumes/{batch['id']}/generate",
        json={"profile_ids": [str(workspace["profile"].id), no_maker.id], "count": 3},
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["created"] == 3 and body["csv_duplicates"] == 2
    assert body["profiles"][0]["skipped_duplicates"] == 1
    assert body["skipped_profiles"] == [
        {"profile_id": no_maker.id, "profile_name": "Unassigned Profile", "reason": "no maker assigned"}
    ]

    db_session.expire_all()
    jobs = (
        await db_session.execute(select(Job).where(Job.source == "bulk").order_by(Job.seq_no))
    ).scalars().all()
    # Rows 2, 6 and 7 (1, 5 and 6 in the file) - in CSV order, after the Maker's own #1.
    assert [job.job_link for job in jobs] == [
        "https://jobs.example.com/1",
        "https://jobs.example.com/5",
        "https://jobs.example.com/6",
    ]
    assert [job.seq_no for job in jobs] == [2, 3, 4]
    assert all(job.maker_id == maker_id and job.profile_id == profile_id for job in jobs)

    # The daily limit (1, already used) did not block the bulk run, and bulk jobs do not use it up.
    maker_api = await api.login("maker@example.com", workspace["password"])
    limit = (await maker_api.get("/api/v1/me/limit")).json()
    assert limit["used"] == 1

    manager = await api.login("manager@example.com", workspace["password"])
    again = await manager.post(f"/api/v1/bulk-resumes/{batch['id']}/generate", json={"profile_ids": [str(profile_id)], "count": 1})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_bulk_by_group_and_the_job_link_reaches_the_resume(api, workspace, db_session):
    manager = await api.login("manager@example.com", workspace["password"])
    group = (await manager.post(
        "/api/v1/profile-groups", json={"name": "Team A", "profile_ids": [str(workspace["profile"].id)]}
    )).json()
    data = make_csv([("https://jobs.example.com/acme", jd(11)), ("https://jobs.example.com/beta", jd(12))])
    batch = (await manager.post("/api/v1/bulk-resumes/upload", files={"file": ("a.csv", data, "text/csv")})).json()
    client = CountingClient()
    set_client_override(client)
    try:
        summary = await manager.post(
            f"/api/v1/bulk-resumes/{batch['id']}/generate", json={"group_id": group["id"], "count": 1}
        )
        assert summary.status_code == 200, summary.text
        await run_pipeline()
    finally:
        set_client_override(None)
    assert client.calls == 1

    maker = await api.login("maker@example.com", workspace["password"])
    rows = (await maker.get("/api/v1/doc-sets", params={"date": date.today().isoformat()})).json()["items"]
    assert len(rows) == 1
    assert rows[0]["job_link"] == "https://jobs.example.com/acme" and rows[0]["source"] == "bulk"
    assert rows[0]["status"]["status"] == "released"

    job = (await db_session.execute(select(Job).where(Job.source == "bulk"))).scalar_one()
    from app.models import FileArtifact, Generation

    generation = (await db_session.execute(select(Generation).where(Generation.job_id == job.id))).scalar_one()
    txt = (await db_session.execute(
        select(FileArtifact).where(FileArtifact.generation_id == generation.id, FileArtifact.kind == "txt")
    )).scalar_one()
    assert "Job link: https://jobs.example.com/acme" in (storage_root() / txt.path).read_text()
    llm_json = (await db_session.execute(
        select(FileArtifact).where(FileArtifact.generation_id == generation.id, FileArtifact.kind == "llm_json")
    )).scalar_one()
    assert json.loads((storage_root() / llm_json.path).read_text())["job_link"] == "https://jobs.example.com/acme"


# ------------------------------------------------------------ duplicate skips


@pytest.mark.asyncio
async def test_a_pasted_duplicate_is_skipped_without_an_llm_call(db_session, workspace):
    client = CountingClient()
    set_client_override(client)
    try:
        first = await submit(db_session, workspace["maker"], jd(40))
        second = await submit(db_session, workspace["maker"], jd(40))
        await run_pipeline()
    finally:
        set_client_override(None)
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert client.calls == 1
    assert first.delivery_status == "released"
    assert second.delivery_status == "skipped" and second.skip_reason == "duplicate"
    assert second.duplicate_of == first.id

    # A cancelled JD is not a copy to protect: submitting it again is fine.
    from app.services import pipeline

    third = await submit(db_session, workspace["maker"], jd(41))
    await pipeline.cancel_job(db_session, job=third, actor_id=workspace["maker"].id)
    await db_session.commit()
    fourth = await submit(db_session, workspace["maker"], jd(41))
    assert fourth.delivery_status == "pending" and fourth.duplicate_of is None
