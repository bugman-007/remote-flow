"""JD Upload's Recent submissions list pages through the whole day (infinite scroll)."""

from __future__ import annotations

import pytest

from tests.helpers import submit_many


@pytest.mark.asyncio
async def test_a_busy_day_pages_through_every_submission_newest_first(api, workspace, db_session):
    maker = workspace["maker"]
    maker.daily_limit = 500
    await db_session.commit()
    jobs = await submit_many(db_session, maker, 150)
    today = jobs[0].submitted_date.isoformat()

    user = await api.login("maker@example.com", workspace["password"])
    first = (await user.get("/api/v1/jobs", params={"date": today, "page": 1, "page_size": 100})).json()
    second = (await user.get("/api/v1/jobs", params={"date": today, "page": 2, "page_size": 100})).json()

    assert first["pagination"] == {"page": 1, "page_size": 100, "total": 150, "pages": 2}
    assert len(first["items"]) == 100 and len(second["items"]) == 50
    seqs = [row["seq_no"] for row in first["items"] + second["items"]]
    assert seqs == sorted(seqs, reverse=True), "newest first, pages continue each other"
    assert len(set(seqs)) == 150, "no row missing or repeated across pages"
