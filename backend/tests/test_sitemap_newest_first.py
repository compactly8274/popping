"""Sitemap-based scraping picks recent articles from the right section.

Modelled on bccpa.ca: one site-wide sitemap, entries in no useful order
and without <lastmod>, newsroom articles at
/news-events/cpabc-newsroom/<year>/<month name>/<slug>/.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app import feed_autodiscovery
from app.feed_autodiscovery import _crawl_sitemaps, _entry_date, _newest_first
from app.sources import generic_scrape
from app.sources.generic_scrape import _section_prefix

SITE = "https://www.example-cpa.ca"
NEWSROOM = f"{SITE}/news-events/cpabc-newsroom/"

SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{NEWSROOM}2023/june/5-reasons-to-come-into-the-office/</loc></url>
  <url><loc>{SITE}/chapter-events/office/2023/august/golf-tournament/</loc></url>
  <url><loc>{NEWSROOM}2026/february/8-things-to-know-before-filing/</loc></url>
  <url><loc>{SITE}/kbase/standards/aasb-2015-2016-annual-report/</loc></url>
  <url><loc>{NEWSROOM}2024/october/cra-new-audit-powers/</loc></url>
  <url><loc>{NEWSROOM}</loc></url>
  <url><loc>{NEWSROOM}2026/july/regulatory-pause-esg/</loc></url>
</urlset>"""


def _serve(monkeypatch, docs: dict[str, str]):
    async def fake_fetch_html_checked(url, custom_headers=None, content_types=None, **kwargs):
        return docs.get(url)

    monkeypatch.setattr(feed_autodiscovery, "fetch_html_checked", fake_fetch_html_checked)


def test_entry_date_prefers_lastmod_then_url():
    assert _entry_date(f"{NEWSROOM}2024/october/x/", "2026-07-27") == dt.datetime(
        2026, 7, 27, tzinfo=dt.timezone.utc
    )
    assert _entry_date(f"{NEWSROOM}2024/october/x/", None) == dt.datetime(
        2024, 10, 1, tzinfo=dt.timezone.utc
    )
    assert _entry_date(f"{SITE}/blog/2025/03/14/pi-day/", None) == dt.datetime(
        2025, 3, 14, tzinfo=dt.timezone.utc
    )
    assert _entry_date(f"{SITE}/about-us/", None) is None


def test_newest_first_keeps_undated_last_in_order():
    ordered = _newest_first([
        (f"{SITE}/about/", None),
        (f"{SITE}/news/2023/01/a/", None),
        (f"{SITE}/contact/", None),
        (f"{SITE}/news/2026/05/b/", None),
    ])
    assert ordered == [
        f"{SITE}/news/2026/05/b/",
        f"{SITE}/news/2023/01/a/",
        f"{SITE}/about/",
        f"{SITE}/contact/",
    ]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_crawl_scoped_to_section_newest_first(monkeypatch):
    _serve(monkeypatch, {f"{SITE}/sitemap.xml": SITEMAP})
    entries = await _crawl_sitemaps(f"{SITE}/sitemap.xml", limit=10, path_prefix="/news-events/cpabc-newsroom/")
    assert [e.url for e in entries] == [
        f"{NEWSROOM}2026/july/regulatory-pause-esg/",
        f"{NEWSROOM}2026/february/8-things-to-know-before-filing/",
        f"{NEWSROOM}2024/october/cra-new-audit-powers/",
        f"{NEWSROOM}2023/june/5-reasons-to-come-into-the-office/",
    ]


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_crawl_limit_applies_after_sorting(monkeypatch):
    _serve(monkeypatch, {f"{SITE}/sitemap.xml": SITEMAP})
    entries = await _crawl_sitemaps(f"{SITE}/sitemap.xml", limit=1, path_prefix="/news-events/cpabc-newsroom/")
    assert [e.url for e in entries] == [f"{NEWSROOM}2026/july/regulatory-pause-esg/"]
    # The date in the URL orders the entries, but it is no publication
    # date (only the month) and no <lastmod>: the candidate carries
    # neither.
    assert entries[0].published is None
    assert entries[0].modified is None


def test_section_prefix():
    assert _section_prefix(NEWSROOM) == "/news-events/cpabc-newsroom/"
    assert _section_prefix(f"{SITE}/insights/blog") == "/insights/blog/"
    assert _section_prefix(f"{SITE}/") is None
    assert _section_prefix(SITE) is None
    assert _section_prefix(f"{SITE}/sitemap.xml") is None


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_an_empty_section_does_not_fall_back_to_the_whole_sitemap(monkeypatch):
    """A section feed whose section has no sitemap entries finds
    nothing. It used to fall back to the whole site's sitemap, which is
    how a section feed filled with the rest of the site (PR10b)."""
    from app.article_detect import Candidate

    calls = []

    async def fake_discover(url, limit=200, path_prefix=None, **kwargs):
        calls.append(path_prefix)
        return [] if path_prefix else [Candidate(f"{SITE}/post", None, None)]

    async def no_page_links(*args, **kwargs):
        return []

    monkeypatch.setattr(generic_scrape, "discover_sitemap_entries", fake_discover)
    monkeypatch.setattr(generic_scrape, "_discover_page_links", no_page_links)
    plan = await generic_scrape.plan_candidates(f"{SITE}/blog/")
    assert plan == generic_scrape.Plan("", [], [])
    assert calls == ["/blog/"]


def test_too_old_skips_old_dated_pages_only():
    now = dt.datetime.now(dt.timezone.utc)
    assert generic_scrape._too_old({"published_at": now - dt.timedelta(days=2500)})
    assert not generic_scrape._too_old({"published_at": now - dt.timedelta(days=1)})
    assert not generic_scrape._too_old({"published_at": None})
    assert not generic_scrape._too_old({})
