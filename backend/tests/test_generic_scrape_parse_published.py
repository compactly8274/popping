"""Slice-18 wire tests: _parse_published_str normalizes trafilatura's
date value to a UTC datetime.

Before slice 18, ``_extract_one`` returned ``data.get("date")``
verbatim — a string like ``"2026-07-27"`` (when the page only
exposes the publication date) or ``"2026-07-27T08:59:10-07:00"``
(when the page exposes a full timestamp). The downstream
``recency.score`` then called ``.tzinfo`` on the string, raising
``AttributeError: 'str' object has no attribute 'tzinfo'`` and
crashing the entire ingest before any entries landed.

After slice 18, ``_extract_one`` runs the date through
``_parse_published_str``, which handles every shape trafilatura
emits and returns a tz-aware ``datetime`` (or ``None`` on
unparseable input, so the entry still lands — just without the
recency boost).

These tests assert the parser:

  1. ISO-8601 with offset (``"2026-07-27T08:59:10-07:00"``)
  2. Bare ISO date (``"2026-07-27"``) — naive datetime, normalized
     to UTC midnight
  3. Z-suffix (``"2026-07-27T08:59:10Z"``) — common in HTML
     metadata
  4. RFC-822 (``"Mon, 27 Jul 2026 15:59:10 +0000"``) — rare but
     real
  5. ``None`` → ``None``
  6. Empty string → ``None``
  7. Garbage string → ``None`` (no raise)
  8. Non-string (e.g. int) → ``None``
  9. Whitespace-only string → ``None``
  10. Whitespace-padded valid string → parses successfully
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import os
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "popping_test")
os.environ.setdefault("POSTGRES_PASSWORD", "popping_test")
os.environ.setdefault("POSTGRES_DB", "popping_test")
os.environ.setdefault("EMBEDDING_ENABLED", "false")


@pytest.mark.no_db
def test_parse_iso_with_offset():
    from app.sources.generic_scrape import _parse_published_str

    result = _parse_published_str("2026-07-27T08:59:10-07:00")
    assert result is not None
    assert result.year == 2026
    assert result.month == 7
    assert result.day == 27
    assert result.hour == 8
    assert result.minute == 59
    assert result.second == 10
    # The offset is preserved (we don't normalize to UTC; that's
    # the consumer's job — recency.score() does it via
    # ``replace(tzinfo=...)`` only if tzinfo is None).
    assert result.tzinfo is not None
    assert result.utcoffset() == dt.timedelta(hours=-7)


@pytest.mark.no_db
def test_parse_bare_iso_date():
    from app.sources.generic_scrape import _parse_published_str

    result = _parse_published_str("2026-07-27")
    assert result is not None
    assert result.year == 2026 and result.month == 7 and result.day == 27
    # Bare date → midnight, normalized to UTC (the recency
    # signal doesn't need sub-day precision for a date-only
    # publication metadata).
    assert result.hour == 0 and result.minute == 0 and result.second == 0
    assert result.tzinfo is not None
    assert result.utcoffset() == dt.timedelta(0)


@pytest.mark.no_db
def test_parse_z_suffix():
    from app.sources.generic_scrape import _parse_published_str

    result = _parse_published_str("2026-07-27T08:59:10Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result.utcoffset() == dt.timedelta(0)
    assert result.year == 2026 and result.month == 7 and result.day == 27
    assert result.hour == 8


@pytest.mark.no_db
def test_parse_rfc822():
    from app.sources.generic_scrape import _parse_published_str

    result = _parse_published_str("Mon, 27 Jul 2026 15:59:10 +0000")
    assert result is not None
    assert result.tzinfo is not None
    assert result.year == 2026 and result.month == 7 and result.day == 27
    assert result.hour == 15


@pytest.mark.no_db
def test_parse_none():
    from app.sources.generic_scrape import _parse_published_str

    assert _parse_published_str(None) is None


@pytest.mark.no_db
def test_parse_empty_string():
    from app.sources.generic_scrape import _parse_published_str

    assert _parse_published_str("") is None
    assert _parse_published_str("   ") is None  # whitespace-only


@pytest.mark.no_db
def test_parse_garbage_does_not_raise():
    """An unparseable string must return ``None`` and NOT raise.
    The whole point of the parser is to keep the ingest
    pipeline running even when trafilatura returns weird
    metadata.
    """
    from app.sources.generic_scrape import _parse_published_str

    for bad in (
        "not a date at all",
        "2026-13-99",  # invalid month/day
        "tomorrow",
        "yesterday",
        "abc/def/ghi",
    ):
        result = _parse_published_str(bad)
        assert result is None, f"expected None for {bad!r}, got {result!r}"


@pytest.mark.no_db
def test_parse_non_string_returns_none():
    """Trafilatura shouldn't return non-strings, but be defensive:
    an int / float / object that slips through must not crash
    the parser.
    """
    from app.sources.generic_scrape import _parse_published_str

    for value in (12345, 1.5, True, ["2026-07-27"], {"date": "2026-07-27"}):
        result = _parse_published_str(value)
        assert result is None, f"expected None for {value!r}, got {result!r}"


@pytest.mark.no_db
def test_parse_padded_whitespace_string():
    """A valid date with leading/trailing whitespace should
    still parse. ``feedparser`` and ``trafilatura`` can
    occasionally add a trailing newline; the parser must
    be tolerant.
    """
    from app.sources.generic_scrape import _parse_published_str

    result = _parse_published_str("  2026-07-27  ")
    assert result is not None
    assert result.year == 2026 and result.month == 7 and result.day == 27
    assert result.tzinfo is not None


@pytest.mark.no_db
def test_extract_one_uses_parser_for_published_at(monkeypatch):
    """End-to-end: ``_extract_one`` invokes the parser on
    trafilatura's date field. We mock trafilatura.bare_extraction
    to return a doc with a string date, then assert the
    item's ``published_at`` is a tz-aware datetime, not a
    string. The pre-slice-18 version returned the raw string,
    which crashed ``recency.score()`` downstream.
    """
    import trafilatura

    from app.sources.generic_scrape import _extract_one

    fake_doc = SimpleNamespace(
        as_dict=lambda: {
            "title": "Some article",
            "text": "Some body text that is long enough to pass the summary check.",
            "date": "2026-07-27",
            "image": None,
        }
    )

    class FakeHtmlCtx:
        async def __aenter__(self):
            return "<html>fake</html>"

        async def __aexit__(self, *args):
            return None

    async def fake_fetch_html(url):
        return "<html>fake</html>"

    monkeypatch.setattr("app.sources.generic_scrape.fetch_html", fake_fetch_html)
    monkeypatch.setattr(trafilatura, "bare_extraction", lambda *a, **k: fake_doc)

    item = asyncio_run(_extract_one("https://example.com/article"))
    assert item is not None
    assert "published_at" in item
    assert isinstance(item["published_at"], dt.datetime), (
        f"published_at must be datetime, not {type(item['published_at']).__name__}"
    )
    assert item["published_at"].tzinfo is not None


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.mark.no_db
def test_extract_one_unparseable_date_yields_none(monkeypatch):
    """If the page's date is unparseable, ``_extract_one``
    should return a result with ``published_at=None`` (so the
    entry still lands in the DB — just without the recency
    boost) rather than raising or returning ``None`` for the
    whole item.
    """
    import trafilatura

    from app.sources.generic_scrape import _extract_one

    fake_doc = SimpleNamespace(
        as_dict=lambda: {
            "title": "Article with weird date",
            "text": "Long enough body text to pass the summary truncation check.",
            "date": "the day before yesterday",
            "image": None,
        }
    )

    async def fake_fetch_html(url):
        return "<html>fake</html>"

    monkeypatch.setattr("app.sources.generic_scrape.fetch_html", fake_fetch_html)
    monkeypatch.setattr(trafilatura, "bare_extraction", lambda *a, **k: fake_doc)

    item = asyncio_run(_extract_one("https://example.com/weird-date"))
    assert item is not None  # the item still lands
    assert item["published_at"] is None  # but the date is None
