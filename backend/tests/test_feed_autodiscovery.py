"""Tests for app.feed_autodiscovery (feed + sitemap discovery) and the
POST /api/sources/auto route.

Same convention as test_article_summary.py / test_feed_discovery.py:
the real "find a working feed/sitemap on a live site" happy path
needs real network access and isn't something a unit test should
depend on. Covered directly instead: SSRF rejection (deterministic,
no network), and the route's name-generation / de-duplication logic
and its two "nothing found" / "no provider" style outcomes.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import feed_autodiscovery as feed_autodiscovery_mod
from app.feed_autodiscovery import discover_feed_url, discover_sitemap_urls
from app.routes import sources as sources_route
from app.routes.sources import _free_source_name, _slugify_hostname
from app.sources import rss as rss_mod
from factories import make_source


# --- app.feed_autodiscovery: SSRF guard (no network needed) ---------------


@pytest.mark.asyncio
async def test_discover_feed_url_rejects_unsafe_url():
    assert await discover_feed_url("http://127.0.0.1:6379/") is None


@pytest.mark.asyncio
async def test_discover_feed_url_rejects_non_http_scheme():
    assert await discover_feed_url("ftp://example.com/") is None


@pytest.mark.asyncio
async def test_discover_sitemap_urls_rejects_unsafe_url():
    assert await discover_sitemap_urls("http://169.254.169.254/latest/meta-data/") == []


@pytest.mark.asyncio
async def test_discover_feed_url_unreachable_host_returns_none():
    # A syntactically-safe URL (passes check_url_safe) but genuinely
    # unreachable in this sandbox — exercises the "fetch_rss raised,
    # find_feed_urls raised too" path without needing real internet
    # access, the same way test_article_summary.py's loopback tests
    # force a deterministic failure.
    assert await discover_feed_url("http://127.0.0.1:1/nope") is None


# --- app.feed_autodiscovery: trafilatura's per-request download timeout ----
#
# Follow-up regression: even after the find_feed_urls -> determine_feed
# fix above, Auto Feed still failed on real multi-sitemap sites.
# trafilatura's OWN per-request download timeout defaults to 30s —
# the exact same value as our outer _TRAFILATURA_BUDGET. sitemap_search
# doesn't make just one request; it crawls robots.txt, several guessed
# paths, and any nested sitemap-index references, fetching each in
# turn. So a single slow/hanging host among those candidates could
# consume the ENTIRE outer 30s budget by itself, starving every other
# candidate in the same crawl of a chance to run — observed directly
# via a real site's sitemap discovery going silent for ~28s before our
# wait_for cut it off. _tighten_trafilatura_download_timeout() mutates
# the shared ConfigParser trafilatura's fetch_url binds as its default
# so every internal request this module triggers is capped tightly
# instead of up to 30s.


def test_tighten_trafilatura_download_timeout_mutates_the_shared_default():
    import inspect

    from trafilatura.downloads import fetch_url

    def _current_timeout() -> int:
        return inspect.signature(fetch_url).parameters["config"].default.getint(
            "DEFAULT", "DOWNLOAD_TIMEOUT",
        )

    original = _current_timeout()
    try:
        feed_autodiscovery_mod._tighten_trafilatura_download_timeout()
        assert _current_timeout() == feed_autodiscovery_mod._TRAFILATURA_DOWNLOAD_TIMEOUT_S
    finally:
        # Restore — this module-level ConfigParser is shared process-
        # wide, so a leaked mutation would affect unrelated trafilatura
        # callers (e.g. app.article_extract) for the rest of the test run.
        inspect.signature(fetch_url).parameters["config"].default.set(
            "DEFAULT", "DOWNLOAD_TIMEOUT", str(original),
        )


def test_sitemap_search_internal_fetch_uses_the_same_shared_config():
    """Pins the mechanism the fix relies on: SitemapObject.fetch calls
    fetch_url with no explicit config, so it resolves to the same
    bound default our tighten function mutates. If a trafilatura
    upgrade ever changes this call to pass its own config, the timeout
    fix silently stops applying to sitemap crawls — this test would
    catch that."""
    import inspect

    from trafilatura.sitemaps import SitemapObject

    src = inspect.getsource(SitemapObject.fetch)
    assert "fetch_url(self.current_url)" in src


# --- app.feed_autodiscovery: real trafilatura discovery, no monkeypatch ----
#
# Everything above (and the rest of this file) either rejects a URL
# before trafilatura is ever called, or monkeypatches discovery
# entirely — by design (see the module docstring: "the real happy
# path needs real network access"). But that meant NOTHING exercised
# trafilatura's actual return contract, and it turned out our
# assumption about it was wrong: trafilatura.feeds.find_feed_urls()
# does not return feed URLs found on a page — it fetches whatever
# feed it finds and returns the ARTICLE links extracted from inside
# it. Every real "paste a homepage URL" Auto Feed attempt failed
# because of this (verified against theprogress.com and other real
# sites reported by the user) even though the underlying site had a
# perfectly normal <link rel=alternate> tag. Guard against this
# regressing (or a future trafilatura upgrade changing behavior
# again) with a real HTTP round-trip over loopback — deterministic,
# no external network, and the only way this class of bug is
# actually observable.


class _MockNewsSiteHandler(BaseHTTPRequestHandler):
    """Serves a homepage with a <link rel=alternate> feed tag, and the
    feed itself, on two paths. Deliberately minimal — just enough
    markup for trafilatura's real parser to find and validate."""

    def log_message(self, *args):  # noqa: D401 - silence test output
        pass

    def do_GET(self):
        if self.path == "/feed.xml":
            body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Mock News</title>
<link>http://{self.headers.get('Host')}/</link>
<item><title>Test article one</title>
<link>http://{self.headers.get('Host')}/article-1</link>
<pubDate>Mon, 27 Jul 2026 10:00:00 GMT</pubDate></item>
<item><title>Test article two</title>
<link>http://{self.headers.get('Host')}/article-2</link>
<pubDate>Sun, 26 Jul 2026 10:00:00 GMT</pubDate></item>
</channel></rss>""".encode()
            content_type = "application/rss+xml"
        else:
            host = self.headers.get("Host")
            body = f"""<!doctype html><html><head><title>Mock News</title>
<link rel="alternate" type="application/rss+xml" title="Mock News Feed"
      href="http://{host}/feed.xml"></head>
<body><h1>Mock News</h1><article><a href="/article-1">A headline</a></article>
</body></html>""".encode()
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mock_news_site(monkeypatch):
    """Starts a real local HTTP server serving realistic homepage +
    feed markup, and bypasses the SSRF guard for the duration of the
    test (a loopback test server is, by design, exactly what that
    guard exists to block on a real request — it's not something to
    weaken outside a test). Yields the site's base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockNewsSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Both modules import check_url_safe directly into their own
    # namespace (``from app.url_safety import check_url_safe``), so
    # each needs its own patch target — patching the source module
    # (app.url_safety.check_url_safe) wouldn't affect either binding.
    safe = lambda url: (True, None)  # noqa: E731
    monkeypatch.setattr(feed_autodiscovery_mod, "check_url_safe", safe)
    monkeypatch.setattr(rss_mod, "check_url_safe", safe)
    # courlan's get_hostinfo (which trafilatura.feeds.determine_feed's
    # caller relies on) treats a bare "127.0.0.1:PORT" as having no
    # extractable domain (it wants a real TLD) and bails before ever
    # reaching determine_feed's actual <link rel=alternate> parsing —
    # a courlan quirk unrelated to the bug under test. Substitute a
    # synthetic domain only when the real extractor comes back empty,
    # so determine_feed's real parsing/validation logic still runs
    # against our real HTTP responses.
    import trafilatura.feeds as trafilatura_feeds_mod
    real_get_hostinfo = trafilatura_feeds_mod.get_hostinfo

    def _patched_get_hostinfo(url: str):
        domain, base = real_get_hostinfo(url)
        return (domain or "mock-news-site.test"), base

    monkeypatch.setattr(trafilatura_feeds_mod, "get_hostinfo", _patched_get_hostinfo)
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_trafilatura_find_feed_urls_does_not_return_the_feed_url(mock_news_site):
    """Documents the exact surprising behavior that caused the bug:
    find_feed_urls() dereferences the feed it finds and returns the
    article links from inside it, not the feed URL. If this ever
    changes upstream, discover_feed_url should be revisited — but it
    must NOT go back to calling find_feed_urls() directly under the
    assumption that its output is a list of candidate feed URLs."""
    from trafilatura.feeds import find_feed_urls

    result = find_feed_urls(mock_news_site)
    assert f"{mock_news_site}feed.xml" not in result


@pytest.mark.asyncio
async def test_discover_feed_url_finds_feed_linked_from_homepage(mock_news_site):
    """The actual regression: pasting a homepage URL (not the feed URL
    directly) must resolve to the real feed, via the <link
    rel=alternate> tag — this is the entire point of Auto Feed."""
    result = await discover_feed_url(mock_news_site)
    assert result == f"{mock_news_site}feed.xml"


# --- app.routes.sources._slugify_hostname ----------------------------------


def test_slugify_hostname_strips_www_and_punctuation():
    assert _slugify_hostname("https://www.example.com/some/path") == "example_com"


def test_slugify_hostname_bare_host():
    assert _slugify_hostname("https://blog.example.co.uk/") == "blog_example_co_uk"


def test_slugify_hostname_no_host_falls_back():
    assert _slugify_hostname("not-a-url") == "site"


# --- app.routes.sources._free_source_name ----------------------------------


@pytest.mark.asyncio
async def test_free_source_name_returns_base_when_unused(db_session):
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com"


@pytest.mark.asyncio
async def test_free_source_name_appends_suffix_on_collision(db_session):
    await make_source(db_session, "example_com")
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com_2"


@pytest.mark.asyncio
async def test_free_source_name_skips_multiple_taken_suffixes(db_session):
    await make_source(db_session, "example_com")
    await make_source(db_session, "example_com_2")
    name = await _free_source_name(db_session, "example_com")
    assert name == "example_com_3"


# --- POST /api/sources/auto -------------------------------------------------


@pytest.mark.asyncio
async def test_source_auto_route_rejects_invalid_url(app_client):
    resp = await app_client.post("/api/sources/auto", json={"url": "not-a-url"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_source_auto_route_rejects_unsafe_url(app_client):
    # _validate_url (the same SSRF guard every other source-creation
    # route uses) rejects a loopback address before discovery is ever
    # attempted — the row never lands in the DB, and the response is
    # a 422 rather than a 200 "nothing found".
    resp = await app_client.post(
        "/api/sources/auto", json={"url": "http://127.0.0.1:1/nope", "category": "news"}
    )
    assert resp.status_code == 422


# The success paths below monkeypatch the two discovery functions
# (imported into app.routes.sources at module scope) so the route's
# OWN composition logic — which branch wins, name generation +
# de-duplication, the scheduler.add_source call, the response shape —
# gets real coverage without depending on network access or a live
# target site, the same reasoning test_generic_scrape.py's plugin
# tests use.


# check_url_safe does a real DNS lookup as part of validating the
# user-supplied URL (see _validate_url), which happens BEFORE these
# tests' monkeypatched discovery functions are ever reached — so the
# outer URL has to be something that genuinely resolves. example.com
# is the one hostname this whole test suite can rely on resolving
# (see test_feed_discovery.py's equivalent reasoning); what the
# (mocked) discovery step "finds" downstream is unvalidated by this
# route, so it can be any fake URL.
_RESOLVABLE_URL = "https://example.com/"


@pytest.mark.asyncio
async def test_source_auto_route_creates_rss_source_when_feed_found(app_client, db_session, monkeypatch):
    async def fake_discover_feed_url(url):
        return "https://blog.example.com/feed.xml"

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "tech"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["kind"] == "rss"
    assert body["source"]["type"] == "rss"
    assert body["source"]["url"] == "https://blog.example.com/feed.xml"
    # Slugified from the DISCOVERED feed's own host, not the
    # original page URL the user pasted in — that's the host whose
    # feed this source is actually subscribed to.
    assert body["source"]["name"] == "blog_example_com"


@pytest.mark.asyncio
async def test_source_auto_route_creates_generic_scrape_when_only_sitemap_found(
    app_client, db_session, monkeypatch,
):
    async def fake_discover_feed_url(url):
        return None

    async def fake_discover_sitemap_urls(url, limit=1):
        return ["https://example.com/article-1"]

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)
    monkeypatch.setattr(sources_route, "discover_sitemap_urls", fake_discover_sitemap_urls)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "news"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["kind"] == "generic_scrape"
    assert body["source"]["type"] == "generic_scrape"
    assert body["source"]["url"] == _RESOLVABLE_URL


@pytest.mark.asyncio
async def test_source_auto_route_reports_not_found_when_neither_path_works(
    app_client, db_session, monkeypatch,
):
    async def fake_discover_feed_url(url):
        return None

    async def fake_discover_sitemap_urls(url, limit=1):
        return []

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)
    monkeypatch.setattr(sources_route, "discover_sitemap_urls", fake_discover_sitemap_urls)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "news"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"found": False, "kind": None, "source": None}


@pytest.mark.asyncio
async def test_source_auto_route_dedupes_name_on_collision(app_client, db_session, monkeypatch):
    await make_source(db_session, "blog_example_com")

    async def fake_discover_feed_url(url):
        return "https://blog.example.com/feed.xml"

    monkeypatch.setattr(sources_route, "discover_feed_url", fake_discover_feed_url)

    resp = await app_client.post(
        "/api/sources/auto", json={"url": _RESOLVABLE_URL, "category": "tech"}
    )
    assert resp.status_code == 200
    assert resp.json()["source"]["name"] == "blog_example_com_2"
