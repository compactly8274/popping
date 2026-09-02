"""Tests for app.sources.generic_scrape — the periodic-scrape fallback
for sites with no native RSS/Atom feed.

Same convention as the other extraction/discovery test modules: the
real "fetch a live URL and extract it" happy path needs network
access this sandbox doesn't have. Covered directly instead: the pure
extraction step against fixture HTML (reusing the same trafilatura
call the live path makes, just fed a string instead of a network
response), and the plugin's per-poll backlog/cap bookkeeping using a
monkeypatched candidate list so it's deterministic and network-free.
"""

from __future__ import annotations

import pytest

from app.sources import generic_scrape
from app.sources.generic_scrape import GenericScrapePlugin, _extract_one, probe
from factories import make_source

_ARTICLE_HTML = """
<html>
<head><title>Site Name</title>
<meta property="og:image" content="https://example.com/photo.jpg">
</head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<article>
<h1>A Real Headline For The Extraction Test</h1>
<p>This is the first paragraph of a real article, with enough
substantive content that trafilatura's extractor should recognize it
as the main body rather than boilerplate chrome around it.</p>
<p>A second paragraph continues the story with more detail and
context, giving the extraction enough total text to work with.</p>
</article>
<footer>Copyright 2026.</footer>
</body>
</html>
"""

_JUNK_HTML = "<html><body><nav>Home About Contact</nav></body></html>"


# --- app.sources.generic_scrape._extract_one (pure-ish; only network is fetch_html) ---


@pytest.mark.asyncio
async def test_extract_one_returns_none_for_unreachable_url():
    # Loopback — check_url_safe (inside fetch_html) rejects it before
    # any network attempt, deterministic regardless of environment.
    assert await _extract_one("http://127.0.0.1:1/nope") is None


# --- app.sources.generic_scrape.probe (row-free "Test" preview path) ------


@pytest.mark.asyncio
async def test_probe_returns_empty_list_when_no_sitemap_found(monkeypatch):
    async def fake_discover_sitemap_urls(url, limit=50):
        return []

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    assert await probe("http://127.0.0.1:1/nope") == []


@pytest.mark.asyncio
async def test_probe_stops_at_limit(monkeypatch):
    # Five candidates all "extract" successfully via a stubbed
    # _extract_one; probe(limit=2) should only take the first 2.
    async def fake_discover_sitemap_urls(url, limit=50):
        return [f"https://example.com/{i}" for i in range(5)]

    async def fake_extract_one(url, custom_headers=None):
        return {"title": f"Article at {url}", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    result = await probe("https://example.com/", limit=2)
    assert len(result) == 2


# --- app.sources.generic_scrape.GenericScrapePlugin.fetch ------------------


@pytest.mark.asyncio
async def test_plugin_fetch_skips_already_extracted_urls(db_session, monkeypatch):
    source = await make_source(db_session, "scraped_site", type="generic_scrape")
    plugin = GenericScrapePlugin(source)
    # Slice 27: ``_extracted_urls`` is now an ``OrderedDict`` with FIFO
    # eviction. Use ``_mark_extracted`` (the bounded helper) to seed
    # the seen-set, and compare against the dict's keys for the
    # membership check below.
    plugin._mark_extracted("https://example.com/already-seen")

    async def fake_discover_sitemap_urls(url, limit=200):
        return ["https://example.com/already-seen", "https://example.com/new-one"]

    calls: list[str] = []

    async def fake_extract_one(url, custom_headers=None):
        calls.append(url)
        return {"title": "New Article", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    items = await plugin.fetch()

    assert calls == ["https://example.com/new-one"]
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/new-one"
    # Both the pre-seeded and the newly-extracted URL are now marked
    # extracted, so a second fetch() with the same candidates finds
    # nothing new to do. Compare against the dict's keys — the
    # OrderedDict is bounded but membership is by URL.
    assert set(plugin._extracted_urls.keys()) == {
        "https://example.com/already-seen",
        "https://example.com/new-one",
    }


@pytest.mark.asyncio
async def test_plugin_fetch_respects_per_poll_cap(db_session, monkeypatch):
    source = await make_source(db_session, "big_site", type="generic_scrape")
    plugin = GenericScrapePlugin(source)

    async def fake_discover_sitemap_urls(url, limit=200):
        return [f"https://example.com/{i}" for i in range(generic_scrape._MAX_NEW_PER_POLL + 5)]

    async def fake_extract_one(url, custom_headers=None):
        return {"title": f"Article {url}", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    items = await plugin.fetch()

    assert len(items) == generic_scrape._MAX_NEW_PER_POLL
    # The URLs past the cap were left unmarked — a second poll can
    # still pick them up rather than skipping them forever.
    assert len(plugin._extracted_urls) == generic_scrape._MAX_NEW_PER_POLL


@pytest.mark.asyncio
async def test_plugin_fetch_runs_extractions_concurrently(db_session, monkeypatch):
    """Regression test for a bug found in a repo-wide audit: fetch()
    used to await _extract_one strictly one at a time, so a source
    whose pages each take several seconds would serialize a full
    _MAX_NEW_PER_POLL batch into one long scheduler tick. Prove the
    fix with wall-clock timing: _MAX_NEW_PER_POLL slow (0.2s each)
    fake extractions must complete in well under
    _MAX_NEW_PER_POLL * 0.2s (fully sequential), bounded instead by
    ceil(_MAX_NEW_PER_POLL / _FETCH_CONCURRENCY) * 0.2s."""
    import asyncio
    import time

    source = await make_source(db_session, "slow_site", type="generic_scrape")
    plugin = GenericScrapePlugin(source)

    async def fake_discover_sitemap_urls(url, limit=200):
        return [f"https://example.com/{i}" for i in range(generic_scrape._MAX_NEW_PER_POLL)]

    async def fake_extract_one(url, custom_headers=None):
        await asyncio.sleep(0.2)
        return {"title": f"Article {url}", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    start = time.monotonic()
    items = await plugin.fetch()
    elapsed = time.monotonic() - start

    assert len(items) == generic_scrape._MAX_NEW_PER_POLL
    sequential_worst_case = generic_scrape._MAX_NEW_PER_POLL * 0.2
    assert elapsed < sequential_worst_case * 0.7, (
        f"fetch() took {elapsed:.2f}s for {generic_scrape._MAX_NEW_PER_POLL} "
        f"x 0.2s extractions — looks sequential, not concurrent "
        f"(fully sequential would be ~{sequential_worst_case:.2f}s)"
    )


@pytest.mark.asyncio
async def test_plugin_fetch_marks_extracted_even_on_extraction_error(db_session, monkeypatch):
    """A single failing extraction (raised exception, not just a
    None return) must not crash the whole batch or leave that URL
    un-marked — it should be skipped and still marked extracted so
    a permanently-broken URL doesn't get retried forever."""
    source = await make_source(db_session, "flaky_site", type="generic_scrape")
    plugin = GenericScrapePlugin(source)
    urls = [f"https://example.com/{i}" for i in range(3)]

    async def fake_discover_sitemap_urls(url, limit=200):
        return urls

    async def fake_extract_one(url, custom_headers=None):
        if url == urls[1]:
            raise RuntimeError("boom")
        return {"title": f"Article {url}", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    items = await plugin.fetch()

    assert len(items) == 2
    assert set(plugin._extracted_urls) == set(urls)