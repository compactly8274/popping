"""Ingest must not embed items whose URL is already stored, and must not
hold a DB connection while the plugin fetches.

Item dates are relative to now: the ingest rules skip stories dated
before the feed's first fetch window, so a fixed date would fall
behind it as the calendar moves on."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app import scheduler
from app.models import Entry
from app.sources.base import SourcePlugin

_NOW = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)


class _FakePlugin(SourcePlugin):
    name = "test_skip_known"
    type = "rss"
    category = "news"
    url = "https://example.test/feed"

    items: list[dict] = []

    async def fetch(self) -> list[dict]:
        return list(self.items)


@pytest.fixture
def embed_calls(monkeypatch):
    calls: list[str] = []

    async def fake_embed(norm):
        calls.append(norm["url"])
        return None

    monkeypatch.setattr(scheduler, "_embed_text", fake_embed)
    monkeypatch.setattr(scheduler, "_reschedule_ingest_job", lambda *a, **k: None)
    return calls


def _item(n: int) -> dict:
    return {"title": f"Item {n}", "url": f"https://example.test/{n}", "published_at": _NOW}


@pytest.mark.asyncio
async def test_second_poll_does_not_re_embed_known_items(db_session, embed_calls):
    _FakePlugin.items = [_item(1), _item(2)]
    first = await scheduler._ingest(_FakePlugin)
    assert first["inserted"] == 2 and first["skipped"] == 0
    assert embed_calls == [
        "https://example.test/1",
        "https://example.test/2",
    ]

    embed_calls.clear()
    _FakePlugin.items = [_item(1), _item(2), _item(3), _item(3)]
    second = await scheduler._ingest(_FakePlugin)
    assert embed_calls == ["https://example.test/3"]
    assert second["inserted"] == 1
    assert second["duplicates"] == 3
    assert second["skipped"] == 0

    total = await db_session.scalar(
        select(func.count()).select_from(Entry).where(Entry.url.like("https://example.test/%"))
    )
    assert total == 3


@pytest.mark.asyncio
async def test_old_backlog_is_skipped_not_embedded(db_session, embed_calls):
    """A story dated well before the feed's first fetch is skipped by the
    ingest rules before the embed step, and counted as skipped."""
    _FakePlugin.items = [_item(1)]
    await scheduler._ingest(_FakePlugin)
    embed_calls.clear()

    old = {
        "title": "Old",
        "url": "https://example.test/old",
        "published_at": _NOW - dt.timedelta(days=40),
    }
    _FakePlugin.items = [_item(1), old]
    summary = await scheduler._ingest(_FakePlugin)
    assert embed_calls == []
    assert summary["inserted"] == 0
    assert summary["duplicates"] == 1
    assert summary["skipped"] == 1


@pytest.mark.asyncio
async def test_fetch_runs_with_no_session_open(db_session, embed_calls, monkeypatch):
    open_sessions = 0
    real_factory = scheduler.SessionLocal

    class _CountingSession:
        def __init__(self):
            self._cm = real_factory()

        async def __aenter__(self):
            nonlocal open_sessions
            open_sessions += 1
            return await self._cm.__aenter__()

        async def __aexit__(self, *exc):
            nonlocal open_sessions
            open_sessions -= 1
            return await self._cm.__aexit__(*exc)

    seen_during_fetch: list[int] = []

    class _Probe(_FakePlugin):
        name = "test_fetch_no_session"

        async def fetch(self):
            seen_during_fetch.append(open_sessions)
            return [_item(10)]

    monkeypatch.setattr(scheduler, "SessionLocal", _CountingSession)
    await scheduler._ingest(_Probe)
    assert seen_during_fetch == [0]
