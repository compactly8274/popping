"""Story counts on every endpoint that returns a source.

``SourceOut`` carries ``stories_24h``, ``stories_7d``,
``newest_story_at`` and ``has_stories``. They are computed, not
columns, and before ``routes.sources._health_counts`` only the list
filled a computed field: GET by id, POST, PATCH and ``/auto`` would
have returned zeros, so a feed's status (PR9b) would flicker after
every save. Every endpoint now fills them from the same query.

- Arrival time (``fetched_at``) decides the windows.
- ``newest_story_at`` is the newest arrival in the last 7 days: null
  when nothing arrived this week, even if older stories exist.
- ``has_stories`` is whether the feed ever stored a story.
- The health columns (``last_attempt_at``, ``last_item_count``,
  ``last_new_count``) come straight off the row.
"""

from __future__ import annotations

import datetime as dt

import pytest

from factories import make_entry, make_source
from ingest_harness import utcnow

_CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
_YT_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={_CHANNEL_ID}"


async def _stories(db_session, source, *hours_ago: float) -> list[dt.datetime]:
    """One stored story per age, by arrival time. Returns the arrivals."""
    arrivals = []
    for n, hours in enumerate(hours_ago):
        at = utcnow() - dt.timedelta(hours=hours)
        await make_entry(db_session, source, f"{source.name} {n}", fetched_at=at)
        arrivals.append(at)
    return arrivals


@pytest.fixture
async def feeds(db_session):
    """Three feeds: a busy one, one whose stories are all older than a
    week, and one that never stored a story."""
    busy = await make_source(db_session, "sc_busy")
    # 2 in the last day, 3 more this week, 2 older than a week.
    arrivals = await _stories(db_session, busy, 1, 20, 30, 72, 160, 200, 400)
    stale = await make_source(db_session, "sc_stale")
    await _stories(db_session, stale, 24 * 8, 24 * 30)
    empty = await make_source(db_session, "sc_empty")
    return {"busy": busy, "stale": stale, "empty": empty, "newest": max(arrivals)}


def _counts(body: dict) -> tuple:
    return (body["stories_24h"], body["stories_7d"], body["has_stories"])


def _when(value: str | None) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromisoformat(value)


def _assert_feeds(by_name: dict, feeds) -> None:
    busy = by_name["sc_busy"]
    assert _counts(busy) == (2, 5, True)
    assert _when(busy["newest_story_at"]) == feeds["newest"]
    stale = by_name["sc_stale"]
    assert _counts(stale) == (0, 0, True)
    assert stale["newest_story_at"] is None
    empty = by_name["sc_empty"]
    assert _counts(empty) == (0, 0, False)
    assert empty["newest_story_at"] is None


async def test_the_list_carries_the_counts(app_client, feeds):
    resp = await app_client.get("/api/sources")
    assert resp.status_code == 200
    _assert_feeds({s["name"]: s for s in resp.json()}, feeds)


async def test_get_by_id_carries_the_same_counts(app_client, feeds):
    by_name = {}
    for key in ("busy", "stale", "empty"):
        resp = await app_client.get(f"/api/sources/{feeds[key].id}")
        assert resp.status_code == 200
        by_name[resp.json()["name"]] = resp.json()
    _assert_feeds(by_name, feeds)


async def test_get_by_id_matches_the_list(app_client, feeds):
    listed = {s["id"]: s for s in (await app_client.get("/api/sources")).json()}
    for key in ("busy", "stale", "empty"):
        one = (await app_client.get(f"/api/sources/{feeds[key].id}")).json()
        assert one == listed[feeds[key].id] | {"net_vote_score": one["net_vote_score"]}


async def test_patch_returns_the_counts(app_client, feeds):
    resp = await app_client.patch(f"/api/sources/{feeds['busy'].id}", json={"display_name": "Busy"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Busy"
    assert _counts(body) == (2, 5, True)
    assert _when(body["newest_story_at"]) == feeds["newest"]


async def test_a_patch_that_changes_nothing_still_returns_the_counts(app_client, feeds):
    resp = await app_client.patch(f"/api/sources/{feeds['stale'].id}", json={})
    assert resp.status_code == 200, resp.text
    assert _counts(resp.json()) == (0, 0, True)


async def test_post_returns_the_fields_for_a_new_feed(app_client, fake_dns):
    resp = await app_client.post(
        "/api/sources",
        json={"name": "sc_new", "type": "youtube_channel", "category": "video", "url": _CHANNEL_ID},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _counts(body) == (0, 0, False)
    assert body["newest_story_at"] is None
    assert (body["last_attempt_at"], body["last_item_count"], body["last_new_count"]) == (None, None, None)


async def test_auto_returns_the_counts_of_a_feed_already_followed(app_client, db_session, fake_dns):
    existing = await make_source(db_session, "sc_auto_existing", type="youtube_channel", url=_YT_FEED)
    await _stories(db_session, existing, 2, 50)

    resp = await app_client.post("/api/sources/auto", json={"url": _CHANNEL_ID, "category": "video"})

    assert resp.status_code == 200, resp.text
    source = resp.json()["source"]
    assert source["id"] == existing.id
    assert _counts(source) == (1, 2, True)


async def test_the_health_columns_come_off_the_row(app_client, db_session, feeds):
    row = feeds["busy"]
    attempt = utcnow() - dt.timedelta(minutes=5)
    row.last_attempt_at = attempt
    row.last_item_count = 30
    row.last_new_count = 4
    db_session.add(row)
    await db_session.commit()

    for body in (
        (await app_client.get(f"/api/sources/{row.id}")).json(),
        next(s for s in (await app_client.get("/api/sources")).json() if s["id"] == row.id),
    ):
        assert _when(body["last_attempt_at"]) == attempt
        assert (body["last_item_count"], body["last_new_count"]) == (30, 4)


async def test_a_story_from_just_over_a_day_ago_counts_for_the_week_only(app_client, db_session):
    source = await make_source(db_session, "sc_edge")
    await _stories(db_session, source, 24.1, 167.9)
    body = (await app_client.get(f"/api/sources/{source.id}")).json()
    assert _counts(body) == (0, 2, True)


async def test_counts_are_per_feed(app_client, db_session):
    a = await make_source(db_session, "sc_per_a")
    b = await make_source(db_session, "sc_per_b")
    await _stories(db_session, a, 1, 2, 3)
    await _stories(db_session, b, 1)
    listed = {s["name"]: s for s in (await app_client.get("/api/sources")).json()}
    assert _counts(listed["sc_per_a"]) == (3, 3, True)
    assert _counts(listed["sc_per_b"]) == (1, 1, True)
