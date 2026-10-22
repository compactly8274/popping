"""The brief leaves out a new feed's backfill: stories published long
before they reached us (``published_at < fetched_at - backfill_hours``).

The guard is measured against each story's own ``fetched_at``, not the
brief's ``since``, so fresh stories with a date-only or slightly early
``published_at`` still make it, at any window size. Both selection
paths carry it: the daily digest (``_select_entries``) and the
convergence alert (``_select_entries_by_slug``)."""

from __future__ import annotations

import datetime as dt

import pytest

from app import runtime_settings
from app.brief import BriefGenerator
from app.models import Entry
from factories import make_source


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _entry(session, source, title, *, published_at, fetched_at, score=50.0):
    # Built directly: ``factories.make_entry`` turns a None
    # ``published_at`` into now, and these tests need undated rows.
    entry = Entry(
        source_id=source.id,
        title=title,
        url=f"https://brief.test/{title.replace(' ', '-')}",
        published_at=published_at,
        composite_score=score,
    )
    session.add(entry)
    await session.commit()
    entry.fetched_at = fetched_at
    await session.commit()
    return entry


def _titles(rows) -> set[str]:
    return {e.title for e, _s in rows}


@pytest.fixture
def window(monkeypatch):
    def _set(hours: int):
        async def _fixed(cls=None):
            return hours

        monkeypatch.setattr(BriefGenerator, "_resolve_window_hours", staticmethod(_fixed))

    _set(24)
    return _set


async def test_daily_digest_keeps_news_and_drops_backfill(db_session, window):
    src = await make_source(db_session, "brief_guard_feed", category="news")
    now = _now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await _entry(
        db_session, src, "Date only story stamped midnight",
        published_at=midnight, fetched_at=now - dt.timedelta(minutes=30),
    )
    await _entry(
        db_session, src, "Published just before the window",
        published_at=now - dt.timedelta(hours=25), fetched_at=now - dt.timedelta(hours=2),
    )
    await _entry(
        db_session, src, "Thirty day old backfill story",
        published_at=now - dt.timedelta(days=30), fetched_at=now - dt.timedelta(hours=1),
    )
    await _entry(
        db_session, src, "Undated story from a scrape",
        published_at=None, fetched_at=now - dt.timedelta(hours=1),
    )

    rows = await BriefGenerator._select_entries(db_session, limit=25)
    assert _titles(rows) == {
        "Date only story stamped midnight",
        "Published just before the window",
        "Undated story from a scrape",
    }


async def test_one_hour_window_still_keeps_fresh_stories(db_session, window):
    window(1)
    src = await make_source(db_session, "brief_guard_1h", category="news")
    now = _now()
    await _entry(
        db_session, src, "Fresh story",
        published_at=now - dt.timedelta(minutes=10), fetched_at=now - dt.timedelta(minutes=5),
    )
    await _entry(
        db_session, src, "Published three hours ago fetched now",
        published_at=now - dt.timedelta(hours=3), fetched_at=now - dt.timedelta(minutes=20),
    )
    await _entry(
        db_session, src, "Old backfill fetched now",
        published_at=now - dt.timedelta(days=9), fetched_at=now - dt.timedelta(minutes=20),
    )
    rows = await BriefGenerator._select_entries(db_session, limit=25)
    assert _titles(rows) == {"Fresh story", "Published three hours ago fetched now"}


async def test_guard_boundary_follows_backfill_hours(db_session, window):
    src = await make_source(db_session, "brief_guard_knob", category="news")
    now = _now()
    fetched = now - dt.timedelta(hours=1)
    await _entry(
        db_session, src, "Published 40 hours before fetch",
        published_at=fetched - dt.timedelta(hours=40), fetched_at=fetched,
    )
    await _entry(
        db_session, src, "Published 50 hours before fetch",
        published_at=fetched - dt.timedelta(hours=50), fetched_at=fetched,
    )
    rows = await BriefGenerator._select_entries(db_session, limit=25)
    assert _titles(rows) == {"Published 40 hours before fetch"}

    try:
        await runtime_settings.set("ingest.backfill_hours", "24")
        rows = await BriefGenerator._select_entries(db_session, limit=25)
        assert _titles(rows) == set()
    finally:
        runtime_settings.invalidate_all()


async def test_convergence_path_has_the_same_guard(db_session, window):
    a = await make_source(db_session, "brief_guard_conv_a", category="news")
    b = await make_source(db_session, "brief_guard_conv_b", category="news")
    now = _now()
    title = "Bridge closes after storm"
    fresh = await _entry(
        db_session, a, title,
        published_at=now - dt.timedelta(hours=2), fetched_at=now - dt.timedelta(hours=1),
    )
    backfill = Entry(
        source_id=b.id,
        title=title,
        url="https://brief.test/old-bridge",
        published_at=now - dt.timedelta(days=20),
        composite_score=90.0,
    )
    db_session.add(backfill)
    await db_session.commit()

    from app.scoring import composite as composite_scorer

    slug = composite_scorer.title_slug(title)
    rows = await BriefGenerator._select_entries_by_slug(db_session, slug, limit=5)
    assert [e.id for e, _s in rows] == [fresh.id]
