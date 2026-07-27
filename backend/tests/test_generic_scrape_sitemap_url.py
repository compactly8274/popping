"""Slice-17 wire tests: Source.sitemap_url + plugin uses it directly.

When ``sources.sitemap_url`` is set on a ``generic_scrape`` row,
the plugin should pass *that* URL to
``discover_sitemap_urls`` (which feeds it to
``trafilatura.sitemaps.sitemap_search``) instead of the page URL.
``trafilatura.sitemap_search`` parses a URL directly when it's a
sitemap, but tries to *find* the sitemap when given a page URL —
the home-vs-sitemap distinction matters, and the wrong one was
returning 0 candidates on real sites like theprogress.com.

These tests are at the plugin level; they don't touch Postgres
or do real network I/O. The wire tests assert the plugin
behavior:

  1. ``sitemap_url=None`` (the default) → ``discover_sitemap_urls``
     is called with ``self.url`` (the page URL). Backwards
     compatible with every existing row.
  2. ``sitemap_url='https://example.com/sitemap.xml'`` →
     ``discover_sitemap_urls`` is called with that URL instead
     of ``self.url``. The user's manual override wins.
  3. The candidate dedup (``_extracted_urls``) and the
     ``_MAX_NEW_PER_POLL`` cap still apply after the override.
  4. ``probe`` (the row-free version used by the Test endpoint)
     is unchanged: it always uses the page URL. ``probe``
     doesn't need the override because the user is asking "does
     this site work?" not "please track this specific sitemap".
  5. Backwards compat at model-load time: a row constructed
     without ``sitemap_url`` attribute defaults to ``None`` so
     pre-migration code paths still work.
"""

from __future__ import annotations

import asyncio
import inspect
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(url: str = "https://example.com", sitemap_url: object = None, **overrides) -> SimpleNamespace:
    """Build a SimpleNamespace that quacks like the slice-17
    Source row — has ``sitemap_url`` regardless of whether it's
    ``None`` (default) or a real URL. The slice-17 plugin uses
    ``getattr(row, 'sitemap_url', None)`` to stay backwards-
    compatible with rows loaded from a pre-migration DB that
    won't have the attribute at all.
    """
    base = dict(
        id=1,
        name="test_source",
        type="generic_scrape",
        category="news",
        url=url,
        refresh_interval_seconds=3600,
        sitemap_url=sitemap_url,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_plugin(row: SimpleNamespace):
    from app.sources.generic_scrape import GenericScrapePlugin
    return GenericScrapePlugin(row)


# ---------------------------------------------------------------------------
# 1. No sitemap_url -> discover_sitemap_urls called with self.url
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_plugin_falls_back_to_self_url_when_sitemap_url_is_none():
    """When ``row.sitemap_url`` is ``None`` (the default for
    every row that hasn't been PATCHed to set one), the plugin
    passes ``self.url`` to ``discover_sitemap_urls``. This is
    the unchanged pre-slice behavior so existing rows aren't
    affected.
    """
    plugin = _make_plugin(_make_row(url="https://example.com", sitemap_url=None))

    with patch("app.sources.generic_scrape.discover_sitemap_urls", new=AsyncMock(return_value=[])) as mock:
        result = await plugin.fetch()

    mock.assert_awaited_once()
    args, kwargs = mock.call_args
    # First positional arg = the sitemap URL the plugin passes
    assert args[0] == "https://example.com"
    assert result == []


# ---------------------------------------------------------------------------
# 2. sitemap_url set -> discover_sitemap_urls called with sitemap_url
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_plugin_uses_sitemap_url_when_set():
    """When the user has PATCHed the row to set
    ``sitemap_url = 'https://theprogress.com/sitemap-news.xml'``,
    the plugin passes that URL to ``discover_sitemap_urls``
    instead of ``self.url``. Trafilatura parses a direct
    sitemap URL (returning the article URLs immediately)
    whereas giving it a page URL triggers a fragile sitemap
    discovery heuristic.
    """
    direct = "https://theprogress.com/sitemap-news.xml"
    plugin = _make_plugin(
        _make_row(url="https://theprogress.com", sitemap_url=direct)
    )

    with patch("app.sources.generic_scrape.discover_sitemap_urls", new=AsyncMock(return_value=[])) as mock:
        result = await plugin.fetch()

    mock.assert_awaited_once()
    args, kwargs = mock.call_args
    assert args[0] == direct
    assert args[0] != "https://theprogress.com"


# ---------------------------------------------------------------------------
# 3. Empty-string sitemap_url -> falls back to self.url
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_plugin_falls_back_on_empty_string_sitemap_url():
    """An empty string for ``sitemap_url`` is treated like
    ``None`` — falsy. Defensive: if a route or migration
    accidentally stores '' instead of NULL, the plugin
    shouldn't pass an empty URL to trafilatura (that would
    raise). Falls back to ``self.url`` instead.
    """
    plugin = _make_plugin(
        _make_row(url="https://example.com", sitemap_url="")
    )

    with patch("app.sources.generic_scrape.discover_sitemap_urls", new=AsyncMock(return_value=[])) as mock:
        await plugin.fetch()

    mock.assert_awaited_once()
    args, kwargs = mock.call_args
    assert args[0] == "https://example.com"


# ---------------------------------------------------------------------------
# 4. _extracted_urls dedup still works with the override URL
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_plugin_dedup_works_with_sitemap_url_override():
    """The instance's ``_extracted_urls`` dedup set is shared
    across polls regardless of which sitemap URL was used to
    populate it. A second poll with the same row should skip
    URLs already attempted.
    """
    direct = "https://example.com/sitemap.xml"
    plugin = _make_plugin(_make_row(url="https://example.com", sitemap_url=direct))

    fake_candidates = [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]

    # Mock _extract_one to return a dummy item for any URL.
    async def fake_extract(url: str):
        return {"title": url.split("/")[-1], "url": url, "text": "x", "published_at": None, "summary": "x", "image_url": None}

    with patch(
        "app.sources.generic_scrape.discover_sitemap_urls",
        new=AsyncMock(return_value=fake_candidates),
    ), patch("app.sources.generic_scrape._extract_one", side_effect=fake_extract):
        # First poll: all three candidates get extracted.
        items_1 = await plugin.fetch()
        # Second poll: all three are in _extracted_urls, so
        # the loop sees them and skips them. Result: empty
        # list (the candidates are exhausted from the dedup).
        items_2 = await plugin.fetch()

    assert len(items_1) == 3
    assert items_2 == []
    assert len(plugin._extracted_urls) == 3


# ---------------------------------------------------------------------------
# 5. Plugin reads sitemap_url from a row lacking the attribute
#    (pre-migration backward compatibility)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_plugin_handles_missing_sitemap_url_attribute():
    """A Source row loaded from a pre-migration DB doesn't
    have a ``sitemap_url`` attribute at all. The plugin uses
    ``getattr(row, 'sitemap_url', None)`` so the constructor
    doesn't raise AttributeError on those rows — they just
    see ``sitemap_url = None`` and use the original
    self.url-based behavior.
    """
    row = SimpleNamespace(
        id=1,
        name="legacy_row",
        type="generic_scrape",
        category="news",
        url="https://example.com",
        refresh_interval_seconds=3600,
        # No sitemap_url — simulates a pre-migration row.
    )

    plugin = _make_plugin(row)
    assert plugin.sitemap_url is None


# ---------------------------------------------------------------------------
# 6. probe() still uses the page URL (not affected by override)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_probe_ignores_sitemap_url_override():
    """The ``probe`` row-free helper used by the Test endpoint
    always uses the page URL the user typed in — even if
    that page happens to be a direct sitemap URL on some
    other row. The override only applies to the live plugin
    instance, not to one-shot probes.
    """
    from app.sources.generic_scrape import probe

    with patch("app.sources.generic_scrape.discover_sitemap_urls", new=AsyncMock(return_value=[])) as mock:
        await probe("https://example.com/sitemap.xml", limit=3)

    mock.assert_awaited_once()
    args, kwargs = mock.call_args
    # probe passes the URL the user typed, not anything
    # sourced from a row's sitemap_url override.
    assert args[0] == "https://example.com/sitemap.xml"
    # And the second arg is limit=max(limit*3, 10) = 10 (not 3).
    # ``discover_sitemap_urls(url, limit=...)`` is a Python
    # call where ``limit`` is a keyword argument — it shows
    # up in mock.call_args.kwargs, not args.
    assert kwargs.get("limit") == 10


# ---------------------------------------------------------------------------
# 7. update_source propagates sitemap_url to the row
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_update_source_writes_sitemap_url_to_row():
    """``scheduler.update_source`` reads its ``sitemap_url`` kwarg
    and writes it to the row when present, leaves it alone when
    ``_UNSET``. The sentinel pattern keeps PATCH semantics correct:
    missing from the body = no-op.
    """
    from app import scheduler as sched_mod
    from app.models import Source
    from app.db import SessionLocal

    fake_row = SimpleNamespace(
        id=42,
        name="chilliwackprogress",
        sitemap_url=None,
        refresh_interval_seconds=3600,
        active=True,
        category="news",
        url="https://theprogress.com",
        favicon_url=None,
        favicon_path=None,
        custom_headers=None,
        error_count=0,
    )

    fake_session = MagicMock()  # noqa: F821
    fake_session.get = AsyncMock(return_value=fake_row)
    fake_session.commit = AsyncMock()
    fake_session.refresh = AsyncMock()

    # Patch the scheduler + assets so we don't actually do work.
    with patch.object(sched_mod, "_scheduler", None):
        new_sitemap = "https://theprogress.com/sitemap-news.xml"
        result = await sched_mod.update_source(
            fake_session,
            source_id=42,
            sitemap_url=new_sitemap,
        )

    # The row's sitemap_url was updated.
    assert fake_row.sitemap_url == new_sitemap
    # And the function returned the row.
    assert result is fake_row


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_update_source_ignores_sitemap_url_when_unset():
    """When the PATCH body doesn't carry ``sitemap_url`` at
    all, ``update_source`` must leave the row's existing
    ``sitemap_url`` untouched. Uses the ``_UNSET`` sentinel
    so ``None`` (explicit clear) and missing (no-op) are
    distinguishable.
    """
    from app import scheduler as sched_mod

    fake_row = SimpleNamespace(
        id=42,
        name="chilliwackprogress",
        sitemap_url="https://theprogress.com/sitemap-news.xml",  # pre-existing
        refresh_interval_seconds=3600,
        active=True,
        category="regional",
        url="https://theprogress.com",
        favicon_url=None,
        favicon_path=None,
        custom_headers=None,
        error_count=0,
    )

    fake_session = MagicMock()  # noqa: F821
    fake_session.get = AsyncMock(return_value=fake_row)
    fake_session.commit = AsyncMock()
    fake_session.refresh = AsyncMock()

    with patch.object(sched_mod, "_scheduler", None):
        # PATCH body doesn't include sitemap_url — should
        # leave the row untouched.
        result = await sched_mod.update_source(
            fake_session,
            source_id=42,
            category="news",  # only change this (matches existing)
        )

    # Pre-existing sitemap_url is preserved.
    assert fake_row.sitemap_url == "https://theprogress.com/sitemap-news.xml"


from unittest.mock import MagicMock  # noqa: E402
