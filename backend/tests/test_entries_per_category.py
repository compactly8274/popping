"""Regression test for the "one high-volume category starves the rest
out of the unfiltered dashboard fetch" bug fixed in PR #11.

Before that fix, ``GET /api/entries`` (no category/source/q filter)
used one global ``ORDER BY composite_score DESC LIMIT N`` across every
source combined, so a high-scoring category could fill the entire
response and leave zero rows for anything else — this reproduces that
exact scenario with skewed synthetic scores and checks
``per_category_limit`` guarantees each category a fair slice.
"""

from __future__ import annotations

import pytest

from factories import make_entry, make_source


async def _seed_skewed_categories(db_session):
    tech = await make_source(db_session, "tech_feed", category="tech")
    news = await make_source(db_session, "news_feed", category="news")
    vulns = await make_source(db_session, "vulns_feed", category="vulns")

    # tech scores high and is high-volume; news and vulns score lower
    # and are lower-volume — exactly the shape that starves them out
    # of a flat global ORDER BY ... LIMIT.
    for i in range(30):
        await make_entry(db_session, tech, f"tech story {i}", composite_score=70.0 + (i % 30))
    for i in range(10):
        await make_entry(db_session, news, f"news story {i}", composite_score=40.0 + (i % 20))
    for i in range(8):
        await make_entry(db_session, vulns, f"vulns story {i}", composite_score=10.0 + (i % 20))


@pytest.mark.asyncio
async def test_flat_limit_can_starve_low_scoring_categories(app_client, db_session):
    await _seed_skewed_categories(db_session)

    resp = await app_client.get("/api/entries", params={"limit": 20})
    assert resp.status_code == 200
    categories_seen = {_category_of(item) for item in resp.json()}
    # This is the bug being guarded against, documented as a fact about
    # the flat query rather than an assertion we want to keep true —
    # it demonstrates why ``per_category_limit`` exists.
    assert categories_seen == {"tech"}


@pytest.mark.asyncio
async def test_per_category_limit_guarantees_a_slice_per_category(app_client, db_session):
    await _seed_skewed_categories(db_session)

    resp = await app_client.get("/api/entries", params={"per_category_limit": 5})
    assert resp.status_code == 200
    rows = resp.json()

    by_category: dict[str, int] = {}
    for item in rows:
        by_category[_category_of(item)] = by_category.get(_category_of(item), 0) + 1

    assert by_category == {"tech": 5, "news": 5, "vulns": 5}


@pytest.mark.asyncio
async def test_per_category_limit_ignored_when_category_filter_set(app_client, db_session):
    await _seed_skewed_categories(db_session)

    # Once ``category`` narrows the query, per_category_limit is a
    # no-op (falls back to the flat ``limit``) — confirms the two
    # params don't fight each other on a filtered view.
    resp = await app_client.get(
        "/api/entries",
        params={"category": "news", "per_category_limit": 2, "limit": 50},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 10  # all news entries, not capped at 2
    assert {_category_of(item) for item in rows} == {"news"}


def _category_of(item: dict) -> str:
    # EntryListOut doesn't ship category directly (see the endpoint's
    # slim projection) — categories are inferred from the source name
    # prefix these tests control (``tech_feed`` -> "tech", etc.).
    for name in ("tech", "news", "vulns"):
        if item["title"].startswith(name):
            return name
    raise AssertionError(f"unexpected entry in response: {item}")
