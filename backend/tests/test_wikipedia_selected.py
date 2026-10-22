"""Wikipedia On This Day posts the day's selected anniversaries only, at
most 5, under URLs that carry the date, so the same anniversary next
year (or the same article tomorrow) is a new story, not a duplicate."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app import scheduler
from app.models import Entry, Source
from app.sources import wikipedia_on_this_day as wiki
from app.sources.wikipedia_on_this_day import WikipediaOnThisDay


def _page(title: str) -> dict:
    return {
        "title": title,
        "content_urls": {"desktop": {"page": f"https://en.wikipedia.org/wiki/{title}"}},
    }


def _payload() -> dict:
    return {
        "events": [{"text": f"Event {n}", "year": 1900 + n, "pages": [_page(f"Event_{n}")]} for n in range(3)],
        "holidays": [{"text": "A holiday", "pages": [_page("Holiday")]}],
        "selected": [
            {"text": "Selected 0", "year": 1801, "pages": [_page("Article_A")]},
            # Links the same article as the first: not a sixth slot.
            {"text": "Selected 0 again", "year": 1802, "pages": [_page("Article_A")]},
            {"text": "", "year": 1803, "pages": [_page("Empty")]},
            {"text": "Selected 1, no linked page", "year": 1804, "pages": []},
            {"text": "Selected 2", "year": 1805, "pages": [_page("Article_B")]},
            {"text": "Selected 3", "year": 1806, "pages": [_page("Article_C")]},
            {"text": "Selected 4", "year": 1807, "pages": [_page("Article_D")]},
            {"text": "Selected 5", "year": 1808, "pages": [_page("Article_E")]},
            {"text": "Selected 6", "year": 1809, "pages": [_page("Article_F")]},
        ],
    }


@pytest.fixture
def wiki_feed(monkeypatch):
    """Serve ``_payload`` for any date and let the test pick "today"."""
    real_client = httpx.AsyncClient
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=json.dumps(_payload()).encode())

    def client(**kwargs):
        # No DNS in tests: drop the SSRF hook (pinned elsewhere) and
        # answer from the handler.
        kwargs.pop("event_hooks", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(wiki.httpx, "AsyncClient", client)

    def set_today(when: dt.datetime) -> None:
        class _Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return when

        monkeypatch.setattr(
            wiki, "dt", SimpleNamespace(datetime=_Clock, timezone=dt.timezone, date=dt.date)
        )

    return SimpleNamespace(set_today=set_today, requested=requested)


@pytest.mark.no_db
async def test_selected_only_at_most_5_with_dated_urls(wiki_feed):
    today = dt.datetime(2026, 10, 19, 6, 0, tzinfo=dt.timezone.utc)
    wiki_feed.set_today(today)
    items = await WikipediaOnThisDay().fetch()
    assert wiki_feed.requested == [f"{wiki._BASE}/10/19"]
    assert len(items) == 5
    assert [i["meta"]["kind"] for i in items] == ["selected"] * 5
    assert [i["title"] for i in items] == [
        "1801: Selected 0",
        "1804: Selected 1, no linked page",
        "1805: Selected 2",
        "1806: Selected 3",
        "1807: Selected 4",
    ]
    assert items[0]["url"] == "https://en.wikipedia.org/wiki/Article_A#otd-2026-10-19"
    # The fallback URL already has a fragment; the date joins it.
    assert items[1]["url"] == (
        "https://en.wikipedia.org/wiki/Wikipedia:Selected_anniversaries"
        "#selected-1--no-linked-page-otd-2026-10-19"
    )
    assert all(i["published_at"] == today for i in items)


@pytest.mark.no_db
def test_class_category_is_on_this_day():
    assert WikipediaOnThisDay.category == "on_this_day"


async def test_same_article_on_two_dates_inserts_twice(db_session, wiki_feed, offline_ingest):
    now = dt.datetime.now(dt.timezone.utc)
    wiki_feed.set_today(now - dt.timedelta(days=1))
    first = await scheduler._ingest(WikipediaOnThisDay)
    assert first["error"] is None
    assert first["inserted"] == 5

    # Same day again: all duplicates.
    again = await scheduler._ingest(WikipediaOnThisDay)
    assert again["inserted"] == 0 and again["duplicates"] == 5

    wiki_feed.set_today(now)
    second = await scheduler._ingest(WikipediaOnThisDay)
    assert second["inserted"] == 5
    assert second["skipped"] == 0

    urls = set(
        (await db_session.scalars(select(Entry.url).where(Entry.url.like("%Article_A%")))).all()
    )
    assert len(urls) == 2

    row = await db_session.scalar(select(Source).where(Source.name == WikipediaOnThisDay.name))
    assert row.category == "on_this_day"
