"""``GET /api/foryou`` (PR4b): the ``max_age_hours`` window, the
caller's hidden stories, and paused feeds, which For You always
leaves out."""

from __future__ import annotations

import datetime as dt

import pytest

from app.auth.deps import ANONYMOUS_USER_ID
from app.models import UserPreference
from factories import make_entry, make_source


def _ago(hours: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


def _ids(resp) -> list[int]:
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()]


async def _entry(db_session, source, title, *, hours_ago: float, score: float = 10.0):
    return await make_entry(
        db_session,
        source,
        title,
        published_at=_ago(hours_ago),
        fetched_at=_ago(hours_ago),
        composite_score=score,
    )


async def _hide(db_session, value, *, user_id: str = ANONYMOUS_USER_ID) -> None:
    db_session.add(UserPreference(user_id=user_id, key="hidden_entries", value=value))
    await db_session.commit()


async def test_window_filters_by_arrival(app_client, db_session):
    feed = await make_source(db_session, "fy_window_feed", category="news")
    old = await _entry(db_session, feed, "three days ago", hours_ago=72, score=90.0)
    new = await _entry(db_session, feed, "an hour ago", hours_ago=1)
    late = await make_entry(
        db_session,
        feed,
        "published last week, arrived now",
        published_at=_ago(24 * 7),
        fetched_at=_ago(2),
        composite_score=5.0,
    )

    assert _ids(await app_client.get("/api/foryou")) == [old.id, new.id, late.id]
    assert _ids(await app_client.get("/api/foryou", params={"max_age_hours": 48})) == [
        new.id,
        late.id,
    ]
    assert _ids(await app_client.get("/api/foryou", params={"max_age_hours": 8760})) == [
        old.id,
        new.id,
        late.id,
    ]


async def test_window_bounds_are_validated(app_client):
    for bad in (0, 8761):
        resp = await app_client.get("/api/foryou", params={"max_age_hours": bad})
        assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("params", [{}, {"max_age_hours": 48}, {"category": "news"}])
async def test_a_paused_feeds_story_is_absent(app_client, db_session, params):
    live = await make_source(db_session, "fy_live_feed", category="news")
    paused = await make_source(db_session, "fy_paused_feed", category="news", active=False)
    kept = await _entry(db_session, live, "live", hours_ago=1)
    # Inside the window and scored far higher: only the filter keeps it out.
    await _entry(db_session, paused, "paused", hours_ago=1, score=99.0)

    assert _ids(await app_client.get("/api/foryou", params=params)) == [kept.id]


@pytest.mark.parametrize("params", [{}, {"max_age_hours": 48}])
async def test_hidden_stories_are_left_out(app_client, db_session, params):
    feed = await make_source(db_session, "fy_hide_feed", category="news")
    hidden = await _entry(db_session, feed, "hidden", hours_ago=1, score=99.0)
    kept = await _entry(db_session, feed, "kept", hours_ago=2)
    await _hide(db_session, [hidden.id])

    assert _ids(await app_client.get("/api/foryou", params=params)) == [kept.id]


async def test_hidden_stories_dont_use_up_the_limit(app_client, db_session):
    feed = await make_source(db_session, "fy_limit_feed", category="news")
    hidden = await _entry(db_session, feed, "hidden", hours_ago=1, score=99.0)
    kept = await _entry(db_session, feed, "kept", hours_ago=2)
    await _hide(db_session, [hidden.id])

    assert _ids(await app_client.get("/api/foryou", params={"limit": 1})) == [kept.id]


async def test_another_users_hidden_stories_dont_apply(app_client, db_session):
    feed = await make_source(db_session, "fy_other_feed", category="news")
    entry = await _entry(db_session, feed, "theirs", hours_ago=1)
    await _hide(db_session, [entry.id], user_id="someone-else")

    assert _ids(await app_client.get("/api/foryou")) == [entry.id]


@pytest.mark.parametrize("value", [{"ids": [1]}, "1", None, [True, "1", 1.0, -1, 2**63]])
async def test_a_malformed_hidden_row_returns_200(app_client, db_session, value):
    feed = await make_source(db_session, "fy_malformed_feed", category="news")
    first = await _entry(db_session, feed, "first", hours_ago=1)
    assert first.id == 1
    await _hide(db_session, value)

    assert _ids(await app_client.get("/api/foryou")) == [first.id]
