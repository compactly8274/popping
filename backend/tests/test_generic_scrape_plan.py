"""PR10b: ``plan_candidates``, the one plan behind a website feed's
fetch and the Test preview.

- A section URL reads its listing page first, then the section of the
  sitemap; a site root or an explicit ``sitemap_url`` reads the sitemap
  first; a URL that is itself a sitemap reads only that.
- The sitemap read is scoped to the link pattern, else to the URL's
  section, with no fallback to the whole site.
- When one read finds articles, the other's failure doesn't matter.
- The feed's headers go to every read.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.article_detect import Candidate
from app.article_extract import FetchRefusedError
from app.sources import generic_scrape
from app.sources.base import NoArticleListError
from app.sources.generic_scrape import GenericScrapePlugin, Plan, plan_candidates

pytestmark = [pytest.mark.no_db, pytest.mark.asyncio]

SITE = "https://site.test"
HEADERS = {"User-Agent": "Safari"}


def _refused(status: int) -> FetchRefusedError:
    request = httpx.Request("GET", f"{SITE}/x")
    return FetchRefusedError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


class _Reads:
    """Stand-ins for the two reads. Each returns its list of URLs as
    candidates, or raises the exception it was given, and records how
    it was called."""

    def __init__(self, monkeypatch, *, sitemap=(), page=()) -> None:
        self.sitemap, self.page = sitemap, page
        self.calls: list[tuple[str, tuple, dict]] = []

        async def fake_sitemap(url, *args, **kwargs):
            self.calls.append(("sitemap", (url, *args), kwargs))
            return self._answer(self.sitemap)

        async def fake_page(url, pattern, *args, **kwargs):
            self.calls.append(("page", (url, pattern, *args), kwargs))
            return self._answer(self.page)

        monkeypatch.setattr(generic_scrape, "discover_sitemap_entries", fake_sitemap)
        monkeypatch.setattr(generic_scrape, "_discover_page_links", fake_page)

    @staticmethod
    def _answer(value):
        if isinstance(value, BaseException):
            raise value
        return [Candidate(u, None, None) for u in value]

    @property
    def order(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def kwargs(self, name: str) -> dict:
        return next(kw for n, _, kw in self.calls if n == name)

    def args(self, name: str) -> tuple:
        return next(a for n, a, _ in self.calls if n == name)


async def test_a_section_url_reads_its_page_first(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/news/from-sitemap"], page=[f"{SITE}/news/from-page"])
    plan = await plan_candidates(f"{SITE}/news")
    assert plan == Plan("page", [Candidate(f"{SITE}/news/from-page", None, None)],
                        [Candidate(f"{SITE}/news/from-page", None, None)])
    assert reads.order == ["page"]


async def test_a_section_url_with_an_empty_page_reads_its_section_of_the_sitemap(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/news/a"], page=[])
    plan = await plan_candidates(f"{SITE}/news")
    assert plan.mode == "sitemap"
    assert plan.fallback
    assert reads.order == ["page", "sitemap"]
    assert reads.args("sitemap")[0] == f"{SITE}/news"
    assert reads.kwargs("sitemap")["path_prefix"] == "/news/"


async def test_a_site_root_reads_its_sitemap_first_then_its_home_page(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[], page=[f"{SITE}/post"])
    plan = await plan_candidates(f"{SITE}/")
    assert plan.mode == "page"
    assert plan.fallback
    assert reads.order == ["sitemap", "page"]
    assert reads.kwargs("sitemap")["path_prefix"] is None


async def test_an_explicit_sitemap_url_is_read_first(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/news/a"], page=[f"{SITE}/news/b"])
    plan = await plan_candidates(f"{SITE}/news", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert plan.mode == "sitemap"
    assert not plan.fallback
    assert reads.order == ["sitemap"]
    assert reads.args("sitemap")[0] == f"{SITE}/news-sitemap.xml"


async def test_a_sitemap_address_reads_the_sitemap_first(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/a"], page=[f"{SITE}/b"])
    plan = await plan_candidates(f"{SITE}/sitemap.xml")
    assert plan.mode == "sitemap"
    assert reads.order == ["sitemap"]


async def test_a_sitemap_address_with_nothing_in_it_is_read_as_a_page(monkeypatch):
    """``looks_like_sitemap`` reads the path only, so an HTML sitemap
    page (``/sitemap/``) looks like one too. It is still the feed's
    listing page: read second, it finds the links."""
    reads = _Reads(monkeypatch, sitemap=[], page=[f"{SITE}/news/b"])
    plan = await plan_candidates(f"{SITE}/sitemap/", link_pattern="/news/")
    assert plan.mode == "page"
    assert reads.order == ["sitemap", "page"]
    assert reads.args("page")[:2] == (f"{SITE}/sitemap/", "/news/")


async def test_an_explicit_sitemap_url_is_read_as_the_sitemap_whatever_it_looks_like(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/a"], page=[])
    await plan_candidates(f"{SITE}/", sitemap_url=f"{SITE}/?sitemap=index")
    assert reads.kwargs("sitemap")["input_is_sitemap"] is True
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/a"], page=[])
    await plan_candidates(f"{SITE}/")
    assert reads.kwargs("sitemap")["input_is_sitemap"] is False


async def test_the_owners_link_pattern_is_trusted_in_the_sitemap_read(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/library/a"], page=[])
    await plan_candidates(f"{SITE}/", link_pattern="/library/")
    assert reads.kwargs("sitemap")["owner_pattern"] is True
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/news/a"], page=[])
    await plan_candidates(f"{SITE}/news/")
    assert reads.kwargs("sitemap")["owner_pattern"] is False


async def test_the_link_pattern_scopes_the_sitemap_instead_of_the_section(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/library/a"], page=[])
    await plan_candidates(f"{SITE}/search?o=newest", link_pattern="/library/")
    assert reads.order == ["page", "sitemap"]
    assert reads.args("page")[1] == "/library/"
    assert reads.kwargs("sitemap")["path_prefix"] == "/library/"


async def test_a_failed_read_does_not_matter_when_the_other_finds_articles(monkeypatch):
    _Reads(monkeypatch, sitemap=[f"{SITE}/news/a"], page=_refused(403))
    plan = await plan_candidates(f"{SITE}/news", raise_errors=True)
    assert [c.url for c in plan.candidates] == [f"{SITE}/news/a"]


async def test_the_most_telling_failure_is_raised(monkeypatch):
    # A refusal of the page beats the sitemap's network error, though
    # the site root reads (and fails) the sitemap first.
    timeout = httpx.ReadTimeout("slow")
    reads = _Reads(monkeypatch, sitemap=timeout, page=_refused(403))
    with pytest.raises(FetchRefusedError):
        await plan_candidates(f"{SITE}/", raise_errors=True)
    assert reads.order == ["sitemap", "page"]
    # A 403 beats a 429, and a 429 beats a missing page.
    missing = httpx.HTTPStatusError(
        "HTTP 404", request=httpx.Request("GET", SITE), response=httpx.Response(404)
    )
    _Reads(monkeypatch, sitemap=_refused(429), page=_refused(403))
    with pytest.raises(FetchRefusedError) as caught:
        await plan_candidates(f"{SITE}/", raise_errors=True)
    assert caught.value.response.status_code == 403
    _Reads(monkeypatch, sitemap=missing, page=_refused(429))
    with pytest.raises(FetchRefusedError) as caught:
        await plan_candidates(f"{SITE}/", raise_errors=True)
    assert caught.value.response.status_code == 429
    _Reads(monkeypatch, sitemap=timeout, page=missing)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await plan_candidates(f"{SITE}/", raise_errors=True)
    assert caught.value is missing
    # Only a network error: it is raised as it is.
    _Reads(monkeypatch, sitemap=timeout, page=[])
    with pytest.raises(httpx.ReadTimeout):
        await plan_candidates(f"{SITE}/news", raise_errors=True)
    # Nothing failed and nothing was found: no list.
    _Reads(monkeypatch, sitemap=[], page=[])
    with pytest.raises(NoArticleListError):
        await plan_candidates(f"{SITE}/news", raise_errors=True)


async def test_without_raise_errors_a_plan_never_raises(monkeypatch):
    _Reads(monkeypatch, sitemap=RuntimeError("bug"), page=_refused(403))
    assert await plan_candidates(f"{SITE}/news") == Plan("", [], [])


async def test_the_reads_pass_raise_errors_on(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[], page=[f"{SITE}/news/a"])
    await plan_candidates(f"{SITE}/news", raise_errors=True)
    assert reads.kwargs("page")["raise_errors"] is True
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/a"], page=[])
    await plan_candidates(f"{SITE}/")
    assert reads.kwargs("sitemap")["raise_errors"] is False


async def test_the_feeds_headers_reach_every_request(monkeypatch):
    reads = _Reads(monkeypatch, sitemap=[f"{SITE}/news/a"], page=[])
    extracted: list[dict] = []

    async def fake_extract(url, custom_headers=None, **kwargs):
        extracted.append(custom_headers)
        return None

    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract)
    row = SimpleNamespace(
        id=1, name="n", type="generic_scrape", category="news", url=f"{SITE}/news",
        refresh_interval_seconds=7200, sitemap_url=None, link_pattern=None, custom_headers=HEADERS,
    )
    await GenericScrapePlugin(row).fetch()
    assert reads.kwargs("page")["headers"] == HEADERS
    assert reads.kwargs("sitemap")["headers"] == HEADERS
    assert extracted == [HEADERS]


async def test_a_listing_page_is_parsed_off_the_event_loop(monkeypatch):
    """``listing_links`` parses the page and tests every link, which is
    CPU-bound; it runs in a thread, so the loop keeps serving requests."""
    import asyncio
    import time

    page = f"{SITE}/news/"

    async def fake_fetch(url, custom_headers=None, **kwargs):
        return "<html><body></body></html>", url

    def slow_listing(html, page_url, **kwargs):
        time.sleep(0.3)  # a CPU-bound parse, as far as the loop can tell
        return [Candidate(f"{SITE}/news/a", None, None)]

    monkeypatch.setattr(generic_scrape, "fetch_html_checked", fake_fetch)
    monkeypatch.setattr(generic_scrape, "listing_links", slow_listing)
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        links = await generic_scrape._discover_page_links(page, None)
    finally:
        done.set()
        await task
    assert [c.url for c in links] == [f"{SITE}/news/a"]
    assert ticks >= 5, f"event loop was blocked during the listing parse (ticks={ticks})"


async def test_owner_configured_follows_the_link_pattern_or_sitemap_url(monkeypatch):
    _Reads(monkeypatch, sitemap=[f"{SITE}/library/a"], page=[f"{SITE}/library/a"])
    seen: list[bool] = []

    async def fake_extract(url, custom_headers=None, *, owner_configured=False, **kwargs):
        seen.append(owner_configured)
        return None

    monkeypatch.setattr(generic_scrape, "_extract_one", fake_extract)

    def plugin(**overrides):
        row = dict(
            id=1, name="n", type="generic_scrape", category="news", url=f"{SITE}/",
            refresh_interval_seconds=7200, sitemap_url=None, link_pattern=None, custom_headers=None,
        )
        row.update(overrides)
        return GenericScrapePlugin(SimpleNamespace(**row))

    await plugin().fetch()
    await plugin(link_pattern="/library/").fetch()
    await plugin(sitemap_url=f"{SITE}/sitemap.xml").fetch()
    assert seen == [False, True, True]

    # A sitemap_url that lists nothing leaves the home page's links,
    # which the owner never chose: they are scored as any page is.
    seen.clear()
    _Reads(monkeypatch, sitemap=[], page=[f"{SITE}/library/a"])
    await plugin(sitemap_url=f"{SITE}/sitemap.xml").fetch()
    await plugin(sitemap_url=f"{SITE}/sitemap.xml", link_pattern="/library/").fetch()
    assert seen == [False, True]
