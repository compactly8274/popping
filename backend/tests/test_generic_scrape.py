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

    async def fake_extract_one(url):
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
    plugin._extracted_urls.add("https://example.com/already-seen")

    async def fake_discover_sitemap_urls(url, limit=200):
        return ["https://example.com/already-seen", "https://example.com/new-one"]

    calls: list[str] = []

    async def fake_extract_one(url):
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
    # nothing new to do.
    assert plugin._extracted_urls == {
        "https://example.com/already-seen",
        "https://example.com/new-one",
    }


@pytest.mark.asyncio
async def test_plugin_fetch_respects_per_poll_cap(db_session, monkeypatch):
    source = await make_source(db_session, "big_site", type="generic_scrape")
    plugin = GenericScrapePlugin(source)

    async def fake_discover_sitemap_urls(url, limit=200):
        return [f"https://example.com/{i}" for i in range(generic_scrape._MAX_NEW_PER_POLL + 5)]

    async def fake_extract_one(url):
        return {"title": f"Article {url}", "url": url}

    monkeypatch.setattr(generic_scrape, "discover_sitemap_urls", fake_discover_sitemap_urls)
    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract_one)

    items = await plugin.fetch()

    assert len(items) == generic_scrape._MAX_NEW_PER_POLL
    # The URLs past the cap were left unmarked — a second poll can
    # still pick them up rather than skipping them forever.
    assert len(plugin._extracted_urls) == generic_scrape._MAX_NEW_PER_POLL
