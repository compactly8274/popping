"""PR10b: the sitemap crawl reads only article sitemaps and keeps only
URLs that could be articles (``article_detect.order_sitemaps`` and
``url_ok``), for website feeds and for ``/auto`` alike.

``fetch_html_checked`` is replaced with a dict of documents, so nothing
touches the network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path

import pytest

from app import feed_autodiscovery
from app.feed_autodiscovery import (
    _crawl_sitemaps,
    _parse_sitemap_entries,
    discover_sitemap_urls,
)

pytestmark = pytest.mark.no_db

FIXTURES = Path(__file__).parent / "fixtures" / "scrape"
SITE = "https://www.example.com"
SHOP = "https://shop.example.com"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'


def _serve(monkeypatch, docs: dict[str, str]) -> list[str]:
    asked: list[str] = []

    async def fake_fetch_html_checked(url, custom_headers=None, content_types=None, **kwargs):
        asked.append(url)
        return docs.get(url)

    monkeypatch.setattr(feed_autodiscovery, "fetch_html_checked", fake_fetch_html_checked)
    # discover_sitemap_* checks the input URL first; these hosts are
    # stand-ins with no DNS.
    monkeypatch.setattr(feed_autodiscovery, "check_url_safe", lambda url: (True, None))
    return asked


def test_entries_carry_a_news_sitemaps_publication_date_and_title():
    pages, nested = _parse_sitemap_entries(_fixture("news-sitemap.xml"))
    assert nested == []
    url, published, _lastmod, title = pages[0]
    assert url == f"{SITE}/news/2026/10/19/bridge-reopens-after-repairs"
    assert published == "2026-10-19T16:20:00-07:00"
    assert title == "Bridge reopens after repairs"


def test_lastmod_is_kept_apart_from_the_publication_date():
    """A ``<lastmod>`` says when a page last changed, not when it was
    published (static pages get edited), so it never stands in for the
    publication date."""
    pages, _ = _parse_sitemap_entries(_fixture("yoast-post-sitemap2.xml"))
    assert pages[0] == (
        f"{SITE}/news/council-approves-river-trail/", None, "2026-10-18T09:30:11+00:00", None
    )
    pages, nested = _parse_sitemap_entries(f"{SITE}/a\n# comment\n{SITE}/b\n")
    assert pages == [(f"{SITE}/a", None, None, None), (f"{SITE}/b", None, None, None)]
    assert nested == []


def test_an_index_gives_its_nested_sitemaps_with_lastmod():
    _, nested = _parse_sitemap_entries(_fixture("yoast-sitemap_index.xml"))
    assert (f"{SITE}/post-sitemap2.xml", "2026-10-18T09:30:11+00:00") in nested


@pytest.mark.asyncio
async def test_a_site_whose_sitemap_lists_only_static_pages_gives_auto_nothing(monkeypatch):
    """Auto-add used to make a website feed whenever a sitemap listed any
    URL. Home, About and Contact are not articles, so ``/auto`` now finds
    nothing to read here."""
    _serve(monkeypatch, {
        f"{SITE}/robots.txt": f"Sitemap: {SITE}/page-sitemap.xml\n",
        f"{SITE}/page-sitemap.xml": _urlset(f"{SITE}/", f"{SITE}/about-us/", f"{SITE}/contact/"),
    })
    assert await discover_sitemap_urls(f"{SITE}/", limit=1) == []


@pytest.mark.asyncio
async def test_robots_txt_sitemaps_skip_the_page_sitemap_beside_a_post_sitemap(monkeypatch):
    """robots.txt lists the page sitemap first. It is never fetched,
    because a post sitemap exists: page sitemaps are where About and
    Contact come from."""
    asked = _serve(monkeypatch, {
        f"{SITE}/robots.txt": f"Sitemap: {SITE}/page-sitemap.xml\nSitemap: {SITE}/post-sitemap.xml\n",
        f"{SITE}/page-sitemap.xml": _fixture("yoast-page-sitemap.xml"),
        f"{SITE}/post-sitemap.xml": _fixture("yoast-post-sitemap2.xml"),
    })
    entries = await _crawl_sitemaps(f"{SITE}/", limit=10)
    assert [e.url for e in entries] == [
        f"{SITE}/news/council-approves-river-trail/",
        f"{SITE}/news/harvest-market-returns/",
    ]
    assert f"{SITE}/page-sitemap.xml" not in asked


@pytest.mark.asyncio
async def test_a_shopify_store_reads_only_its_blogs(monkeypatch):
    asked = _serve(monkeypatch, {
        f"{SHOP}/sitemap.xml": _fixture("shopify-sitemap.xml"),
        f"{SHOP}/sitemap_blogs_1.xml": _urlset(f"{SHOP}/blogs/news/autumn-restock"),
    })
    entries = await _crawl_sitemaps(f"{SHOP}/sitemap.xml", limit=10)
    assert [e.url for e in entries] == [f"{SHOP}/blogs/news/autumn-restock"]
    assert not any("products" in u or "collections" in u or "pages" in u for u in asked)


@pytest.mark.asyncio
async def test_non_article_urls_are_dropped_from_a_post_sitemap(monkeypatch):
    _serve(monkeypatch, {
        f"{SITE}/sitemap.xml": _urlset(
            f"{SITE}/news/council-approves-river-trail/",
            f"{SITE}/tag/council/",
            f"{SITE}/author/dana/",
            f"{SITE}/blog/page/2/",
            "https://elsewhere.example.org/news/copied-story/",
        ),
    })
    entries = await _crawl_sitemaps(f"{SITE}/sitemap.xml", limit=10)
    assert [e.url for e in entries] == [f"{SITE}/news/council-approves-river-trail/"]


@pytest.mark.asyncio
async def test_candidates_are_dated_newest_first(monkeypatch):
    _serve(monkeypatch, {f"{SITE}/news-sitemap.xml": _fixture("news-sitemap.xml")})
    entries = await _crawl_sitemaps(f"{SITE}/news-sitemap.xml", limit=10)
    assert [e.title for e in entries] == ["Bridge reopens after repairs", "School board votes on budget"]
    assert entries[0].published == dt.datetime(2026, 10, 19, 23, 20, tzinfo=dt.timezone.utc)


@pytest.mark.asyncio
async def test_a_post_sitemaps_lastmod_is_carried_but_never_dates(monkeypatch):
    _serve(monkeypatch, {f"{SITE}/post-sitemap.xml": _fixture("yoast-post-sitemap2.xml")})
    entries = await _crawl_sitemaps(f"{SITE}/post-sitemap.xml", limit=10)
    assert [e.url for e in entries] == [
        f"{SITE}/news/council-approves-river-trail/",
        f"{SITE}/news/harvest-market-returns/",
    ]
    assert all(e.published is None for e in entries)
    assert entries[0].modified == dt.datetime(2026, 10, 18, 9, 30, 11, tzinfo=dt.timezone.utc)
    assert entries[0].modified > entries[1].modified


@pytest.mark.asyncio
async def test_an_oldest_first_post_sitemap_is_read_newest_first_by_lastmod(monkeypatch):
    """WordPress core and Yoast list a post sitemap oldest first. Its
    ``<lastmod>`` puts the newest post first, so a crawl capped below the
    sitemap's size keeps the newest posts, not the oldest."""
    entries = "".join(
        f"<url><loc>{SITE}/news/{slug}/</loc><lastmod>{lastmod}</lastmod></url>"
        for slug, lastmod in [
            ("harvest-market-returns", "2026-10-12T14:02:45+00:00"),
            ("council-approves-river-trail", "2026-10-18T09:30:11+00:00"),
        ]
    )
    _serve(monkeypatch, {
        f"{SITE}/post-sitemap.xml":
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>',
    })
    entries = await _crawl_sitemaps(f"{SITE}/post-sitemap.xml", limit=1)
    assert [e.url for e in entries] == [f"{SITE}/news/council-approves-river-trail/"]


@pytest.mark.asyncio
async def test_a_large_sitemap_is_parsed_off_the_event_loop(monkeypatch):
    """Parsing a sitemap and testing each URL is CPU-bound (a 5000-entry
    sitemap takes about half a second in ``url_ok``), so it runs in a
    thread and the loop keeps serving requests meanwhile."""
    _serve(monkeypatch, {f"{SITE}/sitemap.xml": _urlset(f"{SITE}/news/a")})
    real = feed_autodiscovery._parse_sitemap_entries

    def slow_parse(text):
        time.sleep(0.3)  # a CPU-bound parse, as far as the loop can tell
        return real(text)

    monkeypatch.setattr(feed_autodiscovery, "_parse_sitemap_entries", slow_parse)
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        entries = await _crawl_sitemaps(f"{SITE}/sitemap.xml", limit=10)
    finally:
        done.set()
        await task
    assert [e.url for e in entries] == [f"{SITE}/news/a"]
    assert ticks >= 5, f"event loop was blocked during the sitemap parse (ticks={ticks})"
