"""``GET /api/entries`` window parameters (PR4b): ``max_age_hours``,
``per_source_cap``, ``active_only``, and the server-side exclusion of
the caller's hidden stories.

Two query paths take them. The per-category path
(``per_category_limit`` with no category, source or ``q``) is the
dashboard's main poll; the flat path is everything else (a wider tab,
a saved view, a custom tab, All). Each test names the path it covers.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.auth.deps import ANONYMOUS_USER_ID
from app.models import Entry, UserPreference
from factories import make_entry, make_source

NOW = dt.datetime.now(dt.timezone.utc)


def _ago(hours: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


def _ids(resp) -> list[int]:
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()]


async def _entry(db_session, source, title, *, hours_ago: float, score: float = 0.0):
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


async def _one_transaction(db_session, source, scores: list[float]) -> list[Entry]:
    """Rows of one poll: inserted in one transaction, so they share a
    ``fetched_at`` (the server default is the transaction time)."""
    rows = [
        Entry(
            source_id=source.id,
            title=f"{source.name} batch {i}",
            url=f"https://example.com/{source.name}/batch/{i}",
            published_at=NOW,
            composite_score=score,
        )
        for i, score in enumerate(scores)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    for row in rows:
        await db_session.refresh(row)
    assert len({row.fetched_at for row in rows}) == 1
    return rows


# --- no parameters --------------------------------------------------------


async def test_no_parameters_keeps_old_stories_and_the_score_order(app_client, db_session):
    feed = await make_source(db_session, "plain_feed", category="tech")
    old = await _entry(db_session, feed, "old but good", hours_ago=24 * 30, score=90.0)
    new = await _entry(db_session, feed, "new", hours_ago=1, score=10.0)

    # Flat path.
    assert _ids(await app_client.get("/api/entries")) == [old.id, new.id]
    # Per-category path.
    assert _ids(await app_client.get("/api/entries", params={"per_category_limit": 5})) == [
        old.id,
        new.id,
    ]


# --- max_age_hours ----------------------------------------------------------


async def test_window_filters_by_arrival_on_both_paths(app_client, db_session):
    feed = await make_source(db_session, "window_feed", category="tech")
    old = await _entry(db_session, feed, "three days ago", hours_ago=72, score=90.0)
    new = await _entry(db_session, feed, "an hour ago", hours_ago=1, score=10.0)
    # Published a week ago, arrived now: inside a 2-day window.
    late = await make_entry(
        db_session, feed, "late arrival", published_at=_ago(24 * 7), fetched_at=_ago(2)
    )

    for params in ({"max_age_hours": 48}, {"max_age_hours": 48, "per_category_limit": 100}):
        assert set(_ids(await app_client.get("/api/entries", params=params))) == {new.id, late.id}

    # 8760 (the window "All") keeps a year.
    for params in ({"max_age_hours": 8760}, {"max_age_hours": 8760, "per_category_limit": 100}):
        assert set(_ids(await app_client.get("/api/entries", params=params))) == {
            old.id,
            new.id,
            late.id,
        }


async def test_flat_window_cuts_by_arrival_so_a_new_low_score_story_is_kept(app_client, db_session):
    feed = await make_source(db_session, "flat_cut_feed", category="tech")
    high_old = await _entry(db_session, feed, "high, older", hours_ago=30, score=90.0)
    mid = await _entry(db_session, feed, "mid", hours_ago=20, score=50.0)
    low_new = await _entry(db_session, feed, "low, newest", hours_ago=1, score=1.0)

    # Without a window the flat cut is by score: the new story is cut.
    assert _ids(await app_client.get("/api/entries", params={"category": "tech", "limit": 2})) == [
        high_old.id,
        mid.id,
    ]
    # With one it is by arrival, newest first.
    resp = await app_client.get(
        "/api/entries", params={"category": "tech", "limit": 2, "max_age_hours": 48}
    )
    assert _ids(resp) == [low_new.id, mid.id]


async def test_per_category_window_cuts_by_arrival_so_a_new_low_score_story_is_kept(
    app_client, db_session
):
    tech = await make_source(db_session, "pc_cut_tech", category="tech")
    news = await make_source(db_session, "pc_cut_news", category="news")
    high_old = await _entry(db_session, tech, "tech high, older", hours_ago=30, score=90.0)
    mid = await _entry(db_session, tech, "tech mid", hours_ago=20, score=50.0)
    low_new = await _entry(db_session, tech, "tech low, newest", hours_ago=1, score=1.0)
    news_one = await _entry(db_session, news, "news", hours_ago=5, score=5.0)

    resp = await app_client.get("/api/entries", params={"per_category_limit": 2})
    assert set(_ids(resp)) == {high_old.id, mid.id, news_one.id}

    resp = await app_client.get(
        "/api/entries", params={"per_category_limit": 2, "max_age_hours": 48}
    )
    # Windowed, the response is newest arrival first, as on the flat path.
    assert _ids(resp) == [low_new.id, news_one.id, mid.id]


async def test_window_bounds_are_validated(app_client):
    for bad in (0, 8761):
        resp = await app_client.get("/api/entries", params={"max_age_hours": bad})
        assert resp.status_code == 422, resp.text


# --- per_source_cap -----------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"per_category_limit": 100}, {"category": "tech"}])
async def test_cap_without_a_window_is_a_422(app_client, extra):
    resp = await app_client.get("/api/entries", params={"per_source_cap": 25, **extra})
    assert resp.status_code == 422, resp.text


async def test_cap_bounds_are_validated(app_client):
    for bad in (0, 201):
        resp = await app_client.get(
            "/api/entries", params={"per_source_cap": bad, "max_age_hours": 48}
        )
        assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("path", ["flat", "per_category"])
async def test_cap_keeps_a_feeds_best_scoring_stories_not_its_newest(app_client, db_session, path):
    busy = await make_source(db_session, "busy_feed", category="tech")
    # Newest first, worst score first: the newest 3 are the worst.
    rows = [
        await _entry(db_session, busy, f"busy {i}", hours_ago=1 + i, score=float(i))
        for i in range(6)
    ]
    params = {"max_age_hours": 48, "per_source_cap": 3}
    params.update({"limit": 50} if path == "flat" else {"per_category_limit": 100})

    assert set(_ids(await app_client.get("/api/entries", params=params))) == {
        r.id for r in rows[3:]
    }


@pytest.mark.parametrize("path", ["flat", "per_category"])
async def test_cap_on_one_polls_rows_is_deterministic(app_client, db_session, path):
    feed = await make_source(db_session, "batch_feed", category="tech")
    # 30 rows, one fetched_at, many tied scores.
    rows = await _one_transaction(db_session, feed, [float(i % 4) for i in range(30)])
    expected = sorted(rows, key=lambda r: (r.composite_score, r.id), reverse=True)[:25]

    params = {"max_age_hours": 48, "per_source_cap": 25}
    params.update({"limit": 200} if path == "flat" else {"per_category_limit": 100})
    first = _ids(await app_client.get("/api/entries", params=params))
    second = _ids(await app_client.get("/api/entries", params=params))

    assert set(first) == {r.id for r in expected}
    assert first == second


async def test_per_category_cap_runs_before_the_category_cut(app_client, db_session):
    # The cap is cut first, so the category still gets its full
    # per_category_limit from what the cap leaves: 25 + 25 = 50 after
    # the cap, then the newest 40. Cut the other way round, the newest
    # 40 would be 30 + 10 and the cap would leave 35.
    newer = await make_source(db_session, "cap_order_newer", category="tech")
    older = await make_source(db_session, "cap_order_older", category="tech")
    for i in range(30):
        await _entry(db_session, newer, f"newer {i}", hours_ago=1 + i * 0.1, score=float(i))
        await _entry(db_session, older, f"older {i}", hours_ago=10 + i * 0.1, score=float(i))

    resp = await app_client.get(
        "/api/entries",
        params={"per_category_limit": 40, "max_age_hours": 48, "per_source_cap": 25},
    )
    rows = resp.json()
    assert resp.status_code == 200, resp.text
    assert len(rows) == 40
    by_feed = {newer.id: 0, older.id: 0}
    for row in rows:
        by_feed[row["source_id"]] += 1
    assert by_feed == {newer.id: 25, older.id: 15}


async def test_per_category_cap_leaves_other_categories_their_slice(app_client, db_session):
    busy = await make_source(db_session, "cap_busy_tech", category="tech")
    quiet = await make_source(db_session, "cap_quiet_news", category="news")
    for i in range(10):
        await _entry(db_session, busy, f"tech {i}", hours_ago=1 + i, score=50.0 + i)
    news = [await _entry(db_session, quiet, f"news {i}", hours_ago=2 + i, score=1.0) for i in range(3)]

    resp = await app_client.get(
        "/api/entries",
        params={"per_category_limit": 100, "max_age_hours": 48, "per_source_cap": 4},
    )
    ids = _ids(resp)
    assert len(ids) == 7
    assert {r.id for r in news} <= set(ids)


async def test_flat_cap_with_a_category_filter(app_client, db_session):
    a = await make_source(db_session, "flat_cat_a", category="tech")
    b = await make_source(db_session, "flat_cat_b", category="tech")
    other = await make_source(db_session, "flat_cat_other", category="news")
    for i in range(5):
        await _entry(db_session, a, f"a {i}", hours_ago=1 + i, score=float(i))
        await _entry(db_session, b, f"b {i}", hours_ago=1 + i, score=float(i))
        await _entry(db_session, other, f"other {i}", hours_ago=1 + i, score=99.0)

    resp = await app_client.get(
        "/api/entries",
        params={"category": "tech", "limit": 200, "max_age_hours": 48, "per_source_cap": 2},
    )
    rows = resp.json()
    assert resp.status_code == 200, resp.text
    assert sorted(r["title"] for r in rows) == ["a 3", "a 4", "b 3", "b 4"]
    # Newest arrival first.
    fetched = [r["fetched_at"] for r in rows]
    assert fetched == sorted(fetched, reverse=True)


async def test_flat_cap_with_a_source_list(app_client, db_session):
    a = await make_source(db_session, "flat_src_a", category="tech")
    b = await make_source(db_session, "flat_src_b", category="news")
    c = await make_source(db_session, "flat_src_c", category="tech")
    for i in range(4):
        await _entry(db_session, a, f"a {i}", hours_ago=1 + i, score=float(i))
        await _entry(db_session, b, f"b {i}", hours_ago=1 + i, score=float(i))
        await _entry(db_session, c, f"c {i}", hours_ago=1 + i, score=float(i))

    resp = await app_client.get(
        "/api/entries",
        params={
            "source": ["flat_src_a", "flat_src_b"],
            "limit": 200,
            "max_age_hours": 48,
            "per_source_cap": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    assert sorted(r["title"] for r in resp.json()) == ["a 3", "b 3"]


async def test_flat_cap_with_search(app_client, db_session):
    feed = await make_source(db_session, "flat_q_feed", category="tech")
    other = await make_source(db_session, "flat_q_other", category="tech")
    for i in range(4):
        await _entry(db_session, feed, f"needle {i}", hours_ago=1 + i, score=float(i))
        await _entry(db_session, feed, f"hay {i}", hours_ago=1 + i, score=50.0)
        await _entry(db_session, other, f"needle other {i}", hours_ago=1 + i, score=float(i))

    resp = await app_client.get(
        "/api/entries", params={"q": "needle", "max_age_hours": 48, "per_source_cap": 2}
    )
    assert resp.status_code == 200, resp.text
    # The cap ranks within the search's matches, not the feed's best.
    assert sorted(r["title"] for r in resp.json()) == [
        "needle 2",
        "needle 3",
        "needle other 2",
        "needle other 3",
    ]


async def test_8760_with_the_cap_returns_200(app_client, db_session):
    feed = await make_source(db_session, "all_feed", category="tech")
    year_old = await _entry(db_session, feed, "eleven months ago", hours_ago=24 * 330)
    for params in (
        {"max_age_hours": 8760, "per_source_cap": 25},
        {"max_age_hours": 8760, "per_source_cap": 25, "per_category_limit": 100},
        {"max_age_hours": 8760, "per_source_cap": 25, "category": "tech", "limit": 200},
    ):
        assert _ids(await app_client.get("/api/entries", params=params)) == [year_old.id]


# --- active_only --------------------------------------------------------------


async def test_active_only_leaves_out_paused_feeds_on_both_paths(app_client, db_session):
    live = await make_source(db_session, "live_feed", category="tech")
    paused = await make_source(db_session, "paused_feed", category="tech", active=False)
    kept = await _entry(db_session, live, "live", hours_ago=1)
    gone = await _entry(db_session, paused, "paused", hours_ago=1, score=99.0)

    capped = {"max_age_hours": 48, "per_source_cap": 25}
    for params in (
        {},
        {"per_category_limit": 100},
        {"category": "tech"},
        {"source": ["live_feed", "paused_feed"]},
        capped,
        {**capped, "per_category_limit": 100},
        {**capped, "category": "tech"},
    ):
        both = set(_ids(await app_client.get("/api/entries", params=params)))
        assert both == {kept.id, gone.id}
        only = _ids(await app_client.get("/api/entries", params={**params, "active_only": True}))
        assert only == [kept.id]


# --- hidden stories ------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"per_category_limit": 100},
        {"category": "tech"},
        {"source": ["hide_feed"]},
        {"max_age_hours": 48, "per_source_cap": 25},
        {"max_age_hours": 48, "per_source_cap": 25, "per_category_limit": 100},
    ],
)
async def test_hidden_stories_are_left_out(app_client, db_session, params):
    feed = await make_source(db_session, "hide_feed", category="tech")
    hidden = await _entry(db_session, feed, "hidden", hours_ago=1, score=99.0)
    kept = await _entry(db_session, feed, "kept", hours_ago=2)
    await _hide(db_session, [hidden.id])

    assert _ids(await app_client.get("/api/entries", params=params)) == [kept.id]


async def test_hidden_stories_dont_use_up_a_categorys_slots(app_client, db_session):
    feed = await make_source(db_session, "slot_feed", category="tech")
    hidden = [await _entry(db_session, feed, f"hidden {i}", hours_ago=1, score=99.0) for i in range(2)]
    kept = [await _entry(db_session, feed, f"kept {i}", hours_ago=2, score=float(i)) for i in range(2)]
    await _hide(db_session, [h.id for h in hidden])

    resp = await app_client.get("/api/entries", params={"per_category_limit": 2})
    assert set(_ids(resp)) == {k.id for k in kept}
    resp = await app_client.get("/api/entries", params={"category": "tech", "limit": 2})
    assert set(_ids(resp)) == {k.id for k in kept}


async def test_search_still_finds_a_hidden_story(app_client, db_session):
    feed = await make_source(db_session, "search_feed", category="tech")
    hidden = await _entry(db_session, feed, "needle in a haystack", hours_ago=1)
    await _hide(db_session, [hidden.id])

    assert _ids(await app_client.get("/api/entries", params={"q": "needle"})) == [hidden.id]


async def test_another_users_hidden_stories_dont_apply(app_client, db_session):
    feed = await make_source(db_session, "other_user_feed", category="tech")
    entry = await _entry(db_session, feed, "theirs", hours_ago=1)
    await _hide(db_session, [entry.id], user_id="someone-else")

    assert _ids(await app_client.get("/api/entries")) == [entry.id]


async def test_hidden_stories_are_keyed_by_the_resolved_user(app_client, db_session, monkeypatch):
    from app.config import settings

    # With the LAN bypass on, the in-process client (127.0.0.1) is the
    # bypass user, so the anonymous row no longer applies and theirs does.
    monkeypatch.setattr(settings, "local_auth_bypass", True)
    feed = await make_source(db_session, "bypass_feed", category="tech")
    mine = await _entry(db_session, feed, "mine", hours_ago=1)
    anon = await _entry(db_session, feed, "anonymous", hours_ago=2)
    await _hide(db_session, [mine.id], user_id="local-bypass")
    await _hide(db_session, [anon.id])

    assert _ids(await app_client.get("/api/entries")) == [anon.id]


@pytest.mark.parametrize(
    "value",
    [
        {"ids": [1]},
        "1",
        None,
        [None, "1", 1.0, True, -1, 0, 2**63, [1], {"id": 1}],
    ],
)
async def test_a_malformed_hidden_row_returns_200(app_client, db_session, value):
    feed = await make_source(db_session, "malformed_feed", category="tech")
    # The first entry after the per-test truncate has id 1, which every
    # value above names in some wrong shape: none of them hides it.
    first = await _entry(db_session, feed, "first", hours_ago=1)
    assert first.id == 1
    await _hide(db_session, value)

    for params in ({}, {"per_category_limit": 100}, {"category": "tech"}):
        assert _ids(await app_client.get("/api/entries", params=params)) == [first.id]


async def test_a_mixed_hidden_row_keeps_its_real_ids(app_client, db_session):
    feed = await make_source(db_session, "mixed_feed", category="tech")
    first = await _entry(db_session, feed, "first", hours_ago=1)
    second = await _entry(db_session, feed, "second", hours_ago=2)
    await _hide(db_session, ["x", True, second.id, None, second.id])

    assert _ids(await app_client.get("/api/entries")) == [first.id]


async def test_only_the_newest_1000_hidden_ids_are_read(app_client, db_session):
    feed = await make_source(db_session, "cap_hidden_feed", category="tech")
    oldest = await _entry(db_session, feed, "oldest hidden", hours_ago=1)
    # 1000 larger (newer) ids push the real one out of the read.
    await _hide(db_session, [oldest.id, *range(10**6, 10**6 + 1000)])

    assert _ids(await app_client.get("/api/entries")) == [oldest.id]
