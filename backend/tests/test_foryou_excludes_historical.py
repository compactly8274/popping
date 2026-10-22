"""On this day stories stay in their own tab and never reach For You,
the same way the brief already leaves them out."""

from __future__ import annotations

import datetime as dt

from factories import make_entry, make_source


async def test_on_this_day_never_reaches_for_you(app_client, db_session):
    now = dt.datetime.now(dt.timezone.utc)
    otd = await make_source(db_session, "wikipedia_on_this_day", category="on_this_day", type="api")
    news = await make_source(db_session, "foryou_news_feed", category="news")
    # Scored far above the news story, so only the filter keeps it out.
    await make_entry(db_session, otd, "1805: Battle of Trafalgar", published_at=now, composite_score=99.0)
    kept = await make_entry(db_session, news, "Council approves budget", published_at=now, composite_score=10.0)

    resp = await app_client.get("/api/foryou")
    assert resp.status_code == 200, resp.text
    assert [e["id"] for e in resp.json()] == [kept.id]

    # Not even when asked for its category.
    resp = await app_client.get("/api/foryou", params={"category": "on_this_day"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_same_slug_is_excluded_whatever_its_category(app_client, db_session):
    # Excluded by feed (slug), not by category: an install that kept it
    # in news, or moved it elsewhere, still keeps it out of For You.
    now = dt.datetime.now(dt.timezone.utc)
    otd = await make_source(db_session, "wikipedia_on_this_day", category="news", type="api")
    await make_entry(db_session, otd, "1066: Battle of Hastings", published_at=now, composite_score=99.0)
    resp = await app_client.get("/api/foryou")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
