"""PR10b: a website feed's failures say what they are.

Every case runs the real fetch path: ``fetch_html_checked``, the URL
safety check and SSRF hook (DNS answered by ``fake_dns``), the sitemap
crawl, ``listing_links`` and ``score_page``. Only the transport is fake:
each request goes to the test's routes through an httpx
``MockTransport``, and an unrouted path answers 404. Nothing touches the
network.

The feed's error kind is read the way the scheduler records it
(``scheduler._classify_error``):

- a site that refuses us (401-403) is ``[blocked]``, also when it
  serves its sitemap but refuses the article pages;
- a 404 on robots.txt or a guessed sitemap path is routine, and the
  crawl moves on; a 404 counts as ``[not found]`` only on the feed's
  own listing page or ``sitemap_url``;
- a refused robots.txt counts only when every sitemap is refused too,
  and a sitemap index that loads counts neither way;
- a timeout, a refused connection or a host that doesn't resolve is
  ``[network]``, not ``[no list]``;
- ``[no list]`` only when nothing at all lists articles;
- an article page that fails in a way that passes (a timeout, a 5xx) is
  asked for again on the next poll;
- ``probe`` and ``/auto`` never raise.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import article_extract, feed_autodiscovery
from app.article_detect import Candidate
from app.article_extract import FetchRefusedError
from app.scheduler import _classify_error
from app.sources import generic_scrape
from app.sources.generic_scrape import GenericScrapePlugin, plan_candidates, probe

pytestmark = [pytest.mark.no_db, pytest.mark.asyncio]

FIXTURES = Path(__file__).parent / "fixtures" / "scrape"
SITE = "https://www.example.com"
SAFARI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _urlset(*urls: str) -> str:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )


def _index(*urls: str) -> str:
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</sitemapindex>'
    )


def _xml(body: str) -> tuple[int, str, str]:
    return 200, body, "application/xml"


def _html(body: str, status: int = 200) -> tuple[int, str, str]:
    return status, body, "text/html; charset=utf-8"


_NO_LIST_PAGE = "<html><head><title>Example</title></head><body><p>Welcome.</p></body></html>"


class _Site:
    """Routes by full URL. A value is ``(status, body, content type)``,
    an int status, an ``httpx.Response``, an httpx exception class
    (raised for the request), or an async callable taking the request
    and returning one of those."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.requests: list[str] = []
        # (url, User-Agent) of every request, in order.
        self.agents: list[tuple[str, str]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        self.agents.append((url, request.headers.get("user-agent", "")))
        route = self.routes.get(url, 404)
        if isinstance(route, type) and issubclass(route, Exception):
            raise route("simulated", request=request)
        if callable(route):
            route = await route(request)
        if isinstance(route, httpx.Response):
            return route
        if isinstance(route, int):
            return httpx.Response(route, text="", headers={"content-type": "text/html"})
        status, body, content_type = route
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    def asked(self, path: str) -> int:
        return self.requests.count(SITE + path)


@pytest.fixture
def site(monkeypatch, fake_dns):
    fake_dns["www.example.com"] = "93.184.215.14"
    site = _Site()
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(site.handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(article_extract.httpx, "AsyncClient", client)
    # The fixture pages are dated October 2026; keep them in range
    # whenever this runs.
    monkeypatch.setattr(generic_scrape, "_MAX_AGE_DAYS", 100_000)
    return site


def _plugin(url: str, **overrides) -> GenericScrapePlugin:
    row = dict(
        id=1,
        name="example_site",
        type="generic_scrape",
        category="news",
        url=url,
        refresh_interval_seconds=7200,
        sitemap_url=None,
        link_pattern=None,
        custom_headers=None,
    )
    row.update(overrides)
    return GenericScrapePlugin(SimpleNamespace(**row))


async def _kind_of(coro) -> str:
    """Run a fetch that must fail, and return the kind the scheduler
    records for it."""
    with pytest.raises(Exception) as caught:
        await coro
    return _classify_error(caught.value)


# --- the sitemap crawl --------------------------------------------------------


async def test_robots_and_sitemap_404s_still_find_the_wordpress_sitemap(site):
    # robots.txt, /sitemap.xml and /sitemap_index.xml all 404: routine.
    site.routes[f"{SITE}/wp-sitemap.xml"] = _xml(_fixture("wp-sitemap.xml"))
    site.routes[f"{SITE}/wp-sitemap-posts-post-1.xml"] = _xml(
        _urlset(f"{SITE}/2026/10/18/council-approves-river-trail/", f"{SITE}/2026/10/12/harvest-market-returns/")
    )
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert plan.mode == "sitemap"
    assert [c.url for c in plan.candidates] == [
        f"{SITE}/2026/10/18/council-approves-river-trail/",
        f"{SITE}/2026/10/12/harvest-market-returns/",
    ]
    # The page, taxonomy and user sitemaps beside the posts are never read.
    assert site.asked("/wp-sitemap-posts-page-1.xml") == 0
    assert site.asked("/wp-sitemap-taxonomies-category-1.xml") == 0
    assert site.asked("/wp-sitemap-users-1.xml") == 0


async def test_a_refused_robots_txt_with_a_working_sitemap_still_finds_articles(site):
    site.routes[f"{SITE}/robots.txt"] = 403
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(f"{SITE}/news/council-approves-river-trail/"))
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert [c.url for c in plan.candidates] == [f"{SITE}/news/council-approves-river-trail/"]


async def test_robots_txt_listed_sitemaps_are_read_in_class_order(site):
    # The Yoast index lists its page sitemap with the newest <lastmod>;
    # only the post sitemaps are read, newest first.
    site.routes[f"{SITE}/robots.txt"] = _html("User-agent: *\nSitemap: " + SITE + "/sitemap_index.xml\n")
    site.routes[f"{SITE}/sitemap_index.xml"] = _xml(_fixture("yoast-sitemap_index.xml"))
    site.routes[f"{SITE}/post-sitemap2.xml"] = _xml(_fixture("yoast-post-sitemap2.xml"))
    site.routes[f"{SITE}/page-sitemap.xml"] = _xml(_fixture("yoast-page-sitemap.xml"))
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert [c.url for c in plan.candidates] == [
        f"{SITE}/news/council-approves-river-trail/",
        f"{SITE}/news/harvest-market-returns/",
    ]
    assert site.requests.index(f"{SITE}/post-sitemap2.xml") < site.requests.index(f"{SITE}/post-sitemap.xml")
    for skipped in ("page", "category", "post_tag", "author", "attachment", "product", "product_cat"):
        assert site.asked(f"/{skipped}-sitemap.xml") == 0, skipped

    # The feed then brings the post. The second post is gone (404) and
    # the About page in the page sitemap is never fetched at all.
    site.routes[f"{SITE}/news/council-approves-river-trail/"] = _html(_fixture("yoast-post.html"))
    items = await _plugin(f"{SITE}/").fetch()
    assert [i["url"] for i in items] == [f"{SITE}/news/council-approves-river-trail/"]
    assert items[0]["title"].startswith("Council approves the river trail extension")
    assert site.asked("/about-us/") == 0


async def test_all_404s_and_no_listing_is_no_list(site):
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    kind = await _kind_of(_plugin(f"{SITE}/").fetch())
    assert kind == "no list"


async def test_every_sitemap_refused_is_blocked(site):
    # The home page answers but lists nothing; every sitemap is refused.
    for path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"):
        site.routes[SITE + path] = 403
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "blocked"


async def test_one_refused_sitemap_beside_missing_ones_is_not_blocked(site):
    site.routes[f"{SITE}/sitemap.xml"] = 403
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "no list"


async def test_a_refused_robots_txt_alone_is_not_blocked(site):
    # The guessed sitemaps 404 and the home page has no list: the site
    # answers us, it just has nothing to read.
    site.routes[f"{SITE}/robots.txt"] = 403
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "no list"


async def test_a_sitemap_read_timeout_is_network(site):
    site.routes[f"{SITE}/robots.txt"] = _html(f"Sitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = httpx.ReadTimeout
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "network"


async def test_the_crawl_budget_running_out_is_network(site, monkeypatch):
    async def slow(request):
        await asyncio.sleep(5)
        return _xml(_urlset(f"{SITE}/news/late/"))

    monkeypatch.setattr(feed_autodiscovery, "_TRAFILATURA_BUDGET", 0.2)
    site.routes[f"{SITE}/robots.txt"] = _html(f"Sitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = slow
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "network"


async def test_a_host_that_refuses_connections_is_network(site):
    for path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/"):
        site.routes[SITE + path] = httpx.ConnectError
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "network"


async def test_a_missing_explicit_sitemap_url_is_not_found(site):
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert await _kind_of(plugin.fetch()) == "not found"


async def test_an_explicit_sitemap_url_is_read_whatever_its_address_looks_like(site):
    """WordPress's Plain-permalink sitemap (``/?sitemap=index``) has the
    path ``/``. It is still the owner's sitemap: read directly, and a 404
    there is ``[not found]``, not the robots.txt crawl's ``[no list]``."""
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/?sitemap=index")
    assert await _kind_of(plugin.fetch()) == "not found"
    assert site.asked("/robots.txt") == 0

    site.routes[f"{SITE}/?sitemap=index"] = _xml(_urlset(f"{SITE}/news/council-approves-river-trail/"))
    plan = await plan_candidates(f"{SITE}/", sitemap_url=f"{SITE}/?sitemap=index", raise_errors=True)
    assert plan.mode == "sitemap"
    assert [c.url for c in plan.candidates] == [f"{SITE}/news/council-approves-river-trail/"]


# A formatted page whose ``srcset`` puts one image address per line
# (Prettier's HTML output, Ghost's Casper theme).
_SRCSET_PAGE = """<!doctype html>
<html lang="en">
  <head><title>Example</title></head>
  <body>
    <img
      srcset="
        https://images.example-cdn.net/hero-400.jpg 400w,
        https://www.example.com/wp-content/uploads/hero-800.jpg 800w
      "
      alt=""
    />
    <p>Welcome.</p>
  </body>
</html>
"""


@pytest.mark.parametrize("page", [_NO_LIST_PAGE, _SRCSET_PAGE], ids=["plain", "srcset"])
@pytest.mark.parametrize("address", ["/", "/site-map/"])
async def test_a_sitemap_url_that_is_a_page_reads_the_sites_sitemaps(site, address, page):
    """The Sitemap address field holds the site's own address or an HTML
    site-map page. It names no page or sitemap, and doesn't look like a
    sitemap, so the site's sitemaps are read through robots.txt, as for
    a site. When those refuse us, the feed is blocked. A web page's
    lines are no plain-text list of URLs, even when some start with one
    (a formatted ``srcset``)."""
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/post-sitemap.xml\n")
    site.routes[f"{SITE}/post-sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = _html(_fixture("yoast-post.html"))
    site.routes[f"{SITE}/"] = _html(page)
    site.routes[f"{SITE}/site-map/"] = _html(page)
    plugin = _plugin(f"{SITE}/", sitemap_url=SITE + address)
    assert [i["url"] for i in await plugin.fetch()] == [post]

    site.routes[f"{SITE}/post-sitemap.xml"] = 403
    plugin = _plugin(f"{SITE}/", sitemap_url=SITE + address)
    assert await _kind_of(plugin.fetch()) == "blocked"


def _site_sitemap_with_a_post(site: _Site) -> None:
    """robots.txt names a post sitemap whose one post is an article."""
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/post-sitemap.xml\n")
    site.routes[f"{SITE}/post-sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = _html(_fixture("yoast-post.html"))
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)


async def test_an_empty_sitemap_url_is_not_replaced_by_the_sites_other_sitemaps(site):
    """The owner's news sitemap is an empty urlset for a poll (it lists
    only the last two days). Its address looks like a sitemap, so it is
    the owner's sitemap, not a page: the site's other sitemaps are not
    read in its place as the owner's choice. The home page lists nothing
    and answers, so the poll is a quiet one with nothing new, as on main
    (round 7; it read ``[no list]`` before)."""
    _site_sitemap_with_a_post(site)
    site.routes[f"{SITE}/news-sitemap.xml"] = _xml(_urlset())
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert await plugin.fetch() == []
    assert site.asked("/robots.txt") == 0
    assert site.asked("/post-sitemap.xml") == 0


async def test_a_sitemap_url_whose_entries_are_all_left_out_is_not_replaced(site):
    """A ``sitemap_url`` at a page-like address that lists only static
    pages names pages, so it is a sitemap whatever its address: its
    entries are left out (``url_ok``), and the site's other sitemaps are
    not read in its place."""
    _site_sitemap_with_a_post(site)
    site.routes[f"{SITE}/?sitemap=index"] = _xml(_urlset(f"{SITE}/about/", f"{SITE}/contact-us"))
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/?sitemap=index")
    assert await _kind_of(plugin.fetch()) == "no list"
    assert site.asked("/robots.txt") == 0


# A Yoast-style index in robots.txt whose post sitemaps the site refuses
# (the index itself served from a cache, say).
_POST_SITEMAPS = (f"{SITE}/post-sitemap.xml", f"{SITE}/post-sitemap2.xml")


def _refused_index(site: _Site, status: int, *, robots: bool = True) -> None:
    if robots:
        site.routes[f"{SITE}/robots.txt"] = _html(f"Sitemap: {SITE}/sitemap_index.xml\n")
    site.routes[f"{SITE}/sitemap_index.xml"] = _xml(_index(*_POST_SITEMAPS))
    for child in _POST_SITEMAPS:
        site.routes[child] = status
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)


async def test_a_served_index_whose_post_sitemaps_are_all_refused_is_blocked(site):
    _refused_index(site, 403)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "blocked"


async def test_a_served_index_whose_post_sitemaps_are_all_rate_limited_is_an_http_error(site):
    _refused_index(site, 429)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "http error"


async def test_an_explicit_sitemap_index_whose_sitemaps_are_all_refused_is_blocked(site):
    _refused_index(site, 403, robots=False)
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/sitemap_index.xml")
    assert await _kind_of(plugin.fetch()) == "blocked"


async def test_an_index_with_one_refused_and_one_missing_sitemap_is_not_blocked(site):
    _refused_index(site, 403)
    site.routes[_POST_SITEMAPS[1]] = 404
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "no list"


async def test_a_refused_sitemap_beside_one_that_loads_is_not_blocked(site):
    """A sitemap that loads with no articles in it (only an About page
    here) is not an index: the site serves us its sitemaps, so the one
    refused beside it doesn't make the feed blocked."""
    site.routes[f"{SITE}/robots.txt"] = _html(
        f"Sitemap: {SITE}/post-sitemap.xml\nSitemap: {SITE}/news-sitemap.xml\n"
    )
    site.routes[f"{SITE}/post-sitemap.xml"] = 403
    site.routes[f"{SITE}/news-sitemap.xml"] = _xml(_urlset(f"{SITE}/about/"))
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "no list"


async def test_a_host_that_does_not_resolve_is_network(site, monkeypatch):
    """A lapsed domain or a resolver that is down: the URL safety check
    refuses the host, but the feed reads ``[network]`` (it backs off and
    can stop), not the setup state ``[no list]``."""
    import socket

    from app import url_safety

    answer = url_safety.socket.getaddrinfo

    def no_answer(host, port, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name == "www.example.com":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return answer(host, port, *args, **kwargs)

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", no_answer)
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "network"
    assert await _kind_of(_plugin(f"{SITE}/news/").fetch()) == "network"
    assert await _kind_of(_plugin(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml").fetch()) == "network"
    assert site.requests == []
    # The sitemap read says so itself, not only the page read after it,
    # and so does a single page fetch (an article page whose host stops
    # resolving mid-poll is asked for again, not passed over).
    with pytest.raises(httpx.ConnectError):
        await feed_autodiscovery.discover_sitemap_entries(f"{SITE}/", raise_errors=True)
    with pytest.raises(httpx.ConnectError):
        await article_extract.fetch_html_checked(f"{SITE}/news/story/")
    assert await article_extract.fetch_html(f"{SITE}/news/story/") is None
    # The callers that never raise still don't.
    assert await probe(f"{SITE}/") == []
    assert await feed_autodiscovery.discover_sitemap_urls(f"{SITE}/") == []


async def test_a_host_in_a_denied_range_is_still_no_list(site, fake_dns):
    fake_dns["www.example.com"] = "10.0.0.5"
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "no list"
    assert await _kind_of(_plugin(f"{SITE}/news/").fetch()) == "no list"
    assert site.requests == []


# --- the listing page ---------------------------------------------------------


async def test_a_refused_listing_page_is_blocked(site):
    site.routes[f"{SITE}/news/"] = 403
    assert await _kind_of(_plugin(f"{SITE}/news/").fetch()) == "blocked"


async def test_a_missing_listing_page_is_not_found(site):
    assert await _kind_of(_plugin(f"{SITE}/news/").fetch()) == "not found"


async def test_a_rate_limited_listing_page_is_an_http_error(site):
    site.routes[f"{SITE}/news/"] = 429
    assert await _kind_of(_plugin(f"{SITE}/news/").fetch()) == "http error"


async def test_an_html_sitemap_page_is_read_as_a_listing(site):
    """``/sitemap/`` looks like a sitemap by its path, but it is an HTML
    page of links: with a link pattern it is the feed's listing, as it
    was before PR10b."""
    page = f"{SITE}/sitemap/"
    posts = [f"{SITE}/news/story-{n}/" for n in range(5)]
    links = "".join(f'<li><a href="{p}">Story {n}</a></li>' for n, p in enumerate(posts))
    site.routes[page] = _html(f"<html><body><h1>Site map</h1><ul>{links}</ul></body></html>")
    for post in posts:
        site.routes[post] = _html(_fixture("yoast-post.html"))
    plan = await plan_candidates(page, link_pattern="/news/", raise_errors=True)
    assert plan.mode == "page"
    items = await _plugin(page, link_pattern="/news/").fetch()
    assert [i["url"] for i in items] == posts


async def test_a_missing_listing_page_with_a_working_section_sitemap_still_reads(site):
    # The page 404s, but the section has sitemap entries: the feed works.
    site.routes[f"{SITE}/sitemap.xml"] = _xml(
        _urlset(f"{SITE}/news/council-approves-river-trail/", f"{SITE}/about/")
    )
    site.routes[f"{SITE}/news/council-approves-river-trail/"] = _html(_fixture("yoast-post.html"))
    items = await _plugin(f"{SITE}/news/").fetch()
    assert [i["url"] for i in items] == [f"{SITE}/news/council-approves-river-trail/"]


# --- article pages ------------------------------------------------------------


async def test_refused_article_pages_are_blocked_and_retried_later(site):
    """The sitemap is served, every article page is refused: the feed is
    blocked, and the pages stay unmarked, so the poll after Read as
    Safari reads them."""
    posts = [f"{SITE}/news/council-approves-river-trail/", f"{SITE}/news/harvest-market-returns/"]
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*posts))

    async def challenge(request):
        if "Safari" in request.headers.get("user-agent", ""):
            return _html(_fixture("yoast-post.html"))
        return 403

    for post in posts:
        site.routes[post] = challenge

    plugin = _plugin(f"{SITE}/")
    assert await _kind_of(plugin.fetch()) == "blocked"
    assert len(plugin._extracted_urls) == 0

    plugin.custom_headers = {"User-Agent": SAFARI}
    items = await plugin.fetch()
    assert {i["url"] for i in items} == set(posts)
    assert set(plugin._extracted_urls) == set(posts)


async def test_a_page_that_refuses_everyone_does_not_block_the_feed(site):
    """A members-only page alone in a poll: the stored story beside it
    loads, so the site serves us. The page is skipped (marked), and the
    poll succeeds with nothing new."""
    stored = f"{SITE}/news/council-approves-river-trail/"
    members = f"{SITE}/news/members-only-briefing/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(members, stored))
    site.routes[stored] = _html(_fixture("yoast-post.html"))
    site.routes[members] = 403

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    assert members in plugin._extracted_urls
    assert site.requests.count(stored) == 1  # the control fetch
    # The next poll doesn't ask for the page again.
    assert await plugin.fetch(known_urls=known) == []
    assert site.requests.count(members) == 1


async def test_a_site_that_refuses_every_page_is_blocked_even_with_stored_stories(site):
    stored = f"{SITE}/news/council-approves-river-trail/"
    new = f"{SITE}/news/harvest-market-returns/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(new, stored))
    site.routes[stored] = 403
    site.routes[new] = 403

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await _kind_of(plugin.fetch(known_urls=known)) == "blocked"
    assert new not in plugin._extracted_urls


async def test_a_control_page_that_answers_503_does_not_count_as_served(site):
    """A challenge in front of the whole site often answers 503 to the
    stored story the control fetch asks for. That is not the site
    serving us: blocked, and the refused pages stay unmarked."""
    stored = f"{SITE}/news/council-approves-river-trail/"
    new = [f"{SITE}/news/new-post-{n}/" for n in range(3)]
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*new, stored))
    for post in new:
        site.routes[post] = 403
    site.routes[stored] = 503

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await _kind_of(plugin.fetch(known_urls=known)) == "blocked"
    assert not set(new) & set(plugin._extracted_urls)
    # The next poll asks for them again.
    assert await _kind_of(plugin.fetch(known_urls=known)) == "blocked"
    assert all(site.requests.count(post) == 2 for post in new)


async def test_a_control_page_that_is_not_a_page_does_not_count_as_served(site):
    stored = f"{SITE}/news/council-approves-river-trail/"
    new = f"{SITE}/news/new-post/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(new, stored))
    site.routes[new] = 403
    site.routes[stored] = (200, '{"challenge": true}', "application/json")

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await _kind_of(plugin.fetch(known_urls=known)) == "blocked"
    assert new not in plugin._extracted_urls


async def test_a_gone_control_page_is_passed_for_the_next_one(site):
    """A deleted stored story (404) says nothing about whether the site
    serves us, so the next stored story is the control. It loads: the
    lone refused page is members only, skipped, and the poll succeeds."""
    members = f"{SITE}/news/members-only-briefing/"
    deleted = f"{SITE}/news/deleted-story/"
    stored = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(members, deleted, stored))
    site.routes[members] = 403
    site.routes[stored] = _html(_fixture("yoast-post.html"))

    async def known(urls):
        return {deleted, stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    assert members in plugin._extracted_urls
    assert site.requests.count(deleted) == 1
    assert site.requests.count(stored) == 1


async def test_when_every_control_page_is_gone_the_refusal_stands(site):
    members = f"{SITE}/news/members-only-briefing/"
    gone = [f"{SITE}/news/deleted-{n}/" for n in range(2)]
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(members, *gone))
    site.routes[members] = 403

    async def known(urls):
        return set(gone) & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await _kind_of(plugin.fetch(known_urls=known)) == "blocked"
    assert members not in plugin._extracted_urls


async def test_a_second_members_only_page_does_not_block_the_feed(site):
    """The first members-only page was skipped on an earlier poll. When a
    second one is published, the first is not the control (it refuses
    everyone, so it can't say whether the site serves us): the stored
    story is, it loads, and the second page is skipped too."""
    stored = f"{SITE}/news/council-approves-river-trail/"
    first = f"{SITE}/news/members-only-briefing-1/"
    second = f"{SITE}/news/members-only-briefing-2/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(first, stored))
    site.routes[stored] = _html(_fixture("yoast-post.html"))
    site.routes[first] = 403
    site.routes[second] = 403

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    assert first in plugin._extracted_urls
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(second, first, stored))
    for _ in range(3):
        assert await plugin.fetch(known_urls=known) == []
    assert second in plugin._extracted_urls
    assert site.requests.count(first) == 1
    assert site.requests.count(second) == 1


@pytest.mark.parametrize("newer", ["not a page", "gone"])
async def test_pages_read_as_not_a_page_or_gone_are_never_the_control(site, newer):
    """Pages newer than every stored story that the feed already read and
    didn't keep (a PDF at a page-like address, or three deleted stories
    still listed) come first in the candidates. They can't say whether
    the site serves us, so the stored stories are the controls, and a
    lone members-only page is skipped on every poll after the first
    instead of blocking the feed."""
    members = f"{SITE}/news/members-only-briefing/"
    stored = [f"{SITE}/news/council-approves-river-trail/", f"{SITE}/news/harvest-market-returns/"]
    if newer == "not a page":
        top = [f"{SITE}/news/annual-report/"]
        site.routes[top[0]] = (200, "%PDF-1.7 ...", "application/pdf")
    else:
        top = [f"{SITE}/news/deleted-{n}/" for n in range(3)]  # unrouted: 404
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*top, members, *stored))
    site.routes[members] = 403
    for story in stored:
        site.routes[story] = _html(_fixture("yoast-post.html"))

    async def known(urls):
        return set(stored) & set(urls)

    plugin = _plugin(f"{SITE}/")
    for _ in range(4):
        assert await plugin.fetch(known_urls=known) == []
    assert members in plugin._extracted_urls
    assert all(site.requests.count(page) == 1 for page in top)


async def test_a_stored_story_is_the_control_before_a_page_not_tried_yet(site):
    """A full batch of refused pages, with more new pages behind it: the
    stored story is the first control (it loaded as an article before),
    not the next new page, which may be anything (here a PDF)."""
    refused = [f"{SITE}/news/members-only-{n}/" for n in range(generic_scrape._MAX_NEW_PER_POLL)]
    untried = f"{SITE}/news/annual-report/"
    stored = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*refused, untried, stored))
    for page in refused:
        site.routes[page] = 403
    site.routes[untried] = (200, "%PDF-1.7 ...", "application/pdf")
    site.routes[stored] = _html(_fixture("yoast-post.html"))

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    assert set(refused) <= set(plugin._extracted_urls)
    assert site.asked("/news/annual-report/") == 0


@pytest.mark.parametrize("passed_over", ["too old", "not an article"])
async def test_a_page_read_and_passed_over_that_served_a_page_is_a_control(
    site, monkeypatch, passed_over
):
    """A site that posts rarely: every page its sitemap lists was read and
    passed over (too old, or not an article), so the feed has stored
    nothing. Then a members-only page goes up. The pages passed over
    answered with a page when they were read, so the first is the
    control: it loads, the members-only page is skipped, and the feed
    stays healthy instead of reading ``[blocked]`` on every poll (the
    sixth would stop it)."""
    public = [f"{SITE}/news/autumn-update-{n}/" for n in range(3)]
    members = f"{SITE}/news/members-only-briefing/"
    if passed_over == "too old":
        monkeypatch.setattr(generic_scrape, "_MAX_AGE_DAYS", 0)
        page = _html(_fixture("yoast-post.html"))
    else:
        page = _html(_NO_LIST_PAGE)
    for url in public:
        site.routes[url] = page
    site.routes[members] = 403
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*public))

    async def known(urls):
        return set()

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(members, *public))
    for _ in range(6):
        assert await plugin.fetch(known_urls=known) == []
    assert members in plugin._extracted_urls
    assert site.requests.count(members) == 1
    assert site.requests.count(public[0]) == 2  # read, then the control


@pytest.mark.parametrize("earlier", ["not a page", "gone", "members only", "failing"])
async def test_a_page_that_did_not_serve_a_page_is_never_the_control(site, earlier):
    """No stored story can be the control (the only one has been deleted
    since), and pages read on an earlier poll that didn't answer with a
    page (a PDF at a page-like address, deleted stories, a members-only
    page skipped then, a page that answered 503 and is still being
    retried) are listed before a page not tried yet. None of them can
    say whether the site serves us, so they are passed over and the page
    not tried yet is the control: it loads, the poll succeeds, and they
    are not asked for again."""
    stored = f"{SITE}/news/council-approves-river-trail/"
    site.routes[stored] = _html(_fixture("yoast-post.html"))
    if earlier == "not a page":
        read = [f"{SITE}/news/annual-report/"]
        site.routes[read[0]] = (200, "%PDF-1.7 ...", "application/pdf")
    elif earlier == "gone":
        read = [f"{SITE}/news/deleted-{n}/" for n in range(generic_scrape._MAX_CONTROLS)]  # 404
    elif earlier == "members only":
        read = [f"{SITE}/news/members-only-briefing/"]
        site.routes[read[0]] = 403
    else:
        read = [f"{SITE}/news/budget-hearing-notes/"]
        site.routes[read[0]] = 503
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*read, stored))

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    if earlier == "failing":
        assert set(read) <= set(plugin._page_failures)
    else:
        assert set(read) <= set(plugin._extracted_urls)

    # The stored story is deleted, and a full batch of members-only pages
    # goes up, above those pages and a public page not tried yet.
    del site.routes[stored]
    refused = [f"{SITE}/news/members-only-{n}/" for n in range(generic_scrape._MAX_NEW_PER_POLL)]
    untried = f"{SITE}/news/harvest-market-returns/"
    for page in refused:
        site.routes[page] = 403
    site.routes[untried] = _html(_fixture("yoast-post.html"))
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*refused, *read, untried, stored))
    assert await plugin.fetch(known_urls=known) == []
    assert set(refused) <= set(plugin._extracted_urls)
    assert all(site.requests.count(page) == 1 for page in read)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, 503, 500])
async def test_an_article_that_fails_in_a_way_that_passes_is_fetched_again(site, failure):
    """A timeout or a 5xx on a new article doesn't lose it: the page
    stays unmarked, the poll still succeeds, and the next poll reads
    it."""
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = failure
    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch() == []
    assert post not in plugin._extracted_urls
    site.routes[post] = _html(_fixture("yoast-post.html"))
    items = await plugin.fetch()
    assert [i["url"] for i in items] == [post]
    assert post in plugin._extracted_urls


async def test_a_page_that_keeps_failing_is_passed_over_after_three_polls(site):
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = 503
    plugin = _plugin(f"{SITE}/")
    for _ in range(generic_scrape._MAX_PAGE_FAILURES):
        assert await plugin.fetch() == []
    assert post in plugin._extracted_urls
    assert plugin._page_failures == {}
    assert await plugin.fetch() == []
    assert site.requests.count(post) == generic_scrape._MAX_PAGE_FAILURES


@pytest.mark.parametrize("status", [404, 410])
async def test_a_gone_article_page_stays_marked(site, status):
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = status
    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch() == []
    assert post in plugin._extracted_urls
    assert await plugin.fetch() == []
    assert site.requests.count(post) == 1


async def test_a_rate_limited_page_stays_for_later_even_when_the_site_loads(site):
    """A 429 is about this moment, not the page: when the control loads,
    the poll succeeds, but the page stays unmarked for the next poll."""
    stored = f"{SITE}/news/council-approves-river-trail/"
    limited = f"{SITE}/news/harvest-market-returns/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(limited, stored))
    site.routes[stored] = _html(_fixture("yoast-post.html"))
    site.routes[limited] = 429

    async def known(urls):
        return {stored} & set(urls)

    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch(known_urls=known) == []
    assert limited not in plugin._extracted_urls


async def test_a_page_that_redirects_elsewhere_is_not_an_article(site):
    # The same dated post is accepted at its own address and rejected
    # when its address redirects to the home page (``score_page`` reads
    # the final URL).
    gone = f"{SITE}/news/old-story/"
    kept = f"{SITE}/news/fresh-story/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(kept, gone))

    async def to_home(request):
        return httpx.Response(301, headers={"location": f"{SITE}/"})

    site.routes[gone] = to_home
    site.routes[f"{SITE}/"] = _html(_fixture("dated-website-post.html"))
    site.routes[kept] = _html(_fixture("dated-website-post.html"))
    items = await _plugin(f"{SITE}/").fetch()
    assert [i["url"] for i in items] == [kept]


async def test_a_mixed_poll_keeps_the_refused_page_for_later(site):
    ok = f"{SITE}/news/council-approves-river-trail/"
    refused = f"{SITE}/news/harvest-market-returns/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(ok, refused))
    site.routes[ok] = _html(_fixture("yoast-post.html"))
    site.routes[refused] = 403
    plugin = _plugin(f"{SITE}/")
    items = await plugin.fetch()
    assert [i["url"] for i in items] == [ok]
    assert ok in plugin._extracted_urls
    assert refused not in plugin._extracted_urls


async def test_rate_limited_article_pages_are_an_http_error(site):
    posts = [f"{SITE}/news/council-approves-river-trail/", f"{SITE}/news/harvest-market-returns/"]
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*posts))
    for post in posts:
        site.routes[post] = 429
    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "http error"


# --- what gets in -------------------------------------------------------------


async def test_a_listing_page_brings_its_articles_not_its_other_pages(site):
    base = f"{SITE}/blog/"
    site.routes[base] = _html(_fixture("listing.html"))
    council = base + "2026/10/18/council-approves-river-trail/"
    harvest = base + "2026/10/12/harvest-market-returns/"
    site.routes[council] = _html(_fixture("yoast-post.html"))
    # A dead link that answers 200 with a "Page not found" page is
    # fetched and rejected (``score_page``), and not asked for again.
    site.routes[harvest] = _html(_fixture("soft-404.html"))
    plugin = _plugin(base)
    items = await plugin.fetch()
    assert [i["url"] for i in items] == [council]
    assert harvest in plugin._extracted_urls
    # Read page first: the sitemap was never asked.
    assert site.asked("/robots.txt") == 0


async def test_a_link_pattern_accepts_an_undated_page_with_no_article_type(site):
    """The owner's link pattern is the article signal: an ollama-style
    model page declares no article type and no date, and still comes
    in (``score_page(owner_configured=True)``)."""
    page = f"{SITE}/search?o=newest"
    links = "".join(
        f'<li><a href="/library/{m}">{m}</a></li>' for m in ("riverlm3", "riverlm2")
    )
    site.routes[page] = _html(f"<html><body><ul>{links}</ul></body></html>")
    for model in ("riverlm3", "riverlm2"):
        site.routes[f"{SITE}/library/{model}"] = _html(_fixture("ollama-library-model.html"))
    items = await _plugin(page, link_pattern="/library/").fetch()
    assert [i["url"] for i in items] == [f"{SITE}/library/riverlm3", f"{SITE}/library/riverlm2"]
    assert all(i["published_at"] is None for i in items)


def _spy_owner_configured(monkeypatch) -> list[bool]:
    """Record ``owner_configured`` for every page ``_extract_one`` reads,
    running the real extraction."""
    seen: list[bool] = []
    real = generic_scrape._extract_one

    async def spy(url, custom_headers=None, **kwargs):
        seen.append(kwargs.get("owner_configured", False))
        return await real(url, custom_headers=custom_headers, **kwargs)

    monkeypatch.setattr(generic_scrape, "_extract_one", spy)
    return seen


_MODELS = [f"{SITE}/library/riverlm{n}" for n in range(3)]
_MODEL_LIST = "<html><body><main><ul>{}</ul></main></body></html>".format(
    "".join(f'<li><a href="{m}">River LM {n}</a></li>' for n, m in enumerate(_MODELS))
)


async def test_a_dead_sitemap_url_does_not_vouch_for_the_home_pages_links(site, monkeypatch):
    """The owner's sitemap_url answers 404 and the home page's repeating
    links are read instead. The owner never chose those links, so their
    pages (undated, no article type) are scored as any page is, and
    rejected."""
    seen = _spy_owner_configured(monkeypatch)
    site.routes[f"{SITE}/"] = _html(_MODEL_LIST)
    for model in _MODELS:
        site.routes[model] = _html(_fixture("ollama-library-model.html"))
    plugin = _plugin(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert await plugin.fetch() == []
    assert seen == [False] * len(_MODELS)
    assert set(_MODELS) <= set(plugin._extracted_urls)


@pytest.mark.parametrize(
    "failure",
    [503, 429, httpx.ReadTimeout, _xml(_urlset())],
    ids=["503", "429", "timeout", "empty"],
)
async def test_a_sitemap_url_that_fails_for_one_poll_keeps_its_pages(site, monkeypatch, failure):
    """The owner's sitemap_url fails for a few polls, and the home page's
    links (the same pages) are read without the owner's word and
    rejected, once: nothing is fetched again while the sitemap stays
    down. When the sitemap answers again, it vouches for them and they
    come in: bad polls don't lose the owner's pages."""
    seen = _spy_owner_configured(monkeypatch)
    sitemap = f"{SITE}/news-sitemap.xml"
    site.routes[f"{SITE}/"] = _html(_MODEL_LIST)
    for model in _MODELS:
        site.routes[model] = _html(_fixture("ollama-library-model.html"))
    plugin = _plugin(f"{SITE}/", sitemap_url=sitemap)
    site.routes[sitemap] = failure
    for _ in range(3):
        assert await plugin.fetch() == []
    assert seen == [False] * len(_MODELS)
    assert all(site.requests.count(model) == 1 for model in _MODELS)
    site.routes[sitemap] = _xml(_urlset(*_MODELS))
    items = await plugin.fetch()
    assert [i["url"] for i in items] == _MODELS[: generic_scrape._DEFAULT_MAX_UNDATED]
    assert seen[len(_MODELS):] == [True] * len(_MODELS)
    # Read once more, not on every poll: the third poll asks for nothing.
    assert await plugin.fetch() == []
    assert all(site.requests.count(model) == 2 for model in _MODELS)


async def test_a_working_sitemap_url_vouches_for_its_pages(site, monkeypatch):
    seen = _spy_owner_configured(monkeypatch)
    site.routes[f"{SITE}/news-sitemap.xml"] = _xml(_urlset(*_MODELS))
    for model in _MODELS:
        site.routes[model] = _html(_fixture("ollama-library-model.html"))
    items = await _plugin(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml").fetch()
    assert [i["url"] for i in items] == _MODELS[: generic_scrape._DEFAULT_MAX_UNDATED]
    assert seen == [True] * len(_MODELS)


def _once(first, then, *, times: int = 1):
    """A route that answers ``first`` to the first ``times`` requests,
    then ``then``."""
    asked = 0

    async def route(request):
        nonlocal asked
        asked += 1
        return first if asked <= times else then

    return route


class _Stored:
    """The feed's stored stories, as ``known_urls`` reads them; each poll's
    items are stored, as ``scheduler._ingest`` would."""

    def __init__(self, *urls: str) -> None:
        self.urls = set(urls)

    async def __call__(self, urls):
        return self.urls & set(urls)

    async def poll(self, plugin: GenericScrapePlugin) -> list[str]:
        items = await plugin.fetch(known_urls=self)
        self.urls |= {i["url"] for i in items}
        return [i["url"] for i in items]


@pytest.mark.parametrize("left", ["sitemap 503", "page 503", "page 503 twice", "page 429"])
async def test_a_page_left_for_a_later_poll_keeps_its_place(site, left):
    """The owner's sitemap (no ``<lastmod>``) lists a new dated article
    above a new undated model page. One poll leaves the model page for
    later: the sitemap answers 503, so the home page's links are read
    without the owner's word, or the page itself answers 503 (on one
    poll or two) or 429. The article comes in on that poll. When the
    model page loads, it is listed below a story stored since, but it
    was new when it was first left, and so it still is: it comes in,
    once."""
    sitemap = f"{SITE}/library-sitemap.xml"
    article = f"{SITE}/news/council-approves-river-trail/"
    other = f"{SITE}/news/harvest-market-returns/"
    model, older = f"{SITE}/library/riverlm3", f"{SITE}/library/riverlm1"
    for story in (article, other):
        site.routes[story] = _html(_fixture("yoast-post.html"))
    page = _html(_fixture("ollama-library-model.html"))
    site.routes[model] = page
    listed = _xml(_urlset(article, model, older))
    site.routes[sitemap] = listed
    site.routes[f"{SITE}/"] = _html(
        "<html><body><main><ul>{}</ul></main></body></html>".format(
            "".join(f'<li><a href="{u}">{u}</a></li>' for u in (article, model, other))
        )
    )
    failures = 2 if left.endswith("twice") else 1
    if left == "sitemap 503":
        site.routes[sitemap] = _once(503, listed)
    else:
        site.routes[model] = _once(int(left.split()[1]), page, times=failures)
    stored = _Stored(older)
    plugin = _plugin(f"{SITE}/", sitemap_url=sitemap)
    assert article in await stored.poll(plugin)
    for _ in range(failures - 1):
        assert await stored.poll(plugin) == []
    assert model not in stored.urls
    assert await stored.poll(plugin) == [model]
    assert await stored.poll(plugin) == []
    assert site.requests.count(model) == failures + 1


async def test_a_page_left_for_later_while_it_was_backlog_is_still_backlog(site):
    """After a restart the feed reads again the pages it passed over
    before. One listed below a stored story answers 503 and is left for
    later: it was backlog then, so it is when it loads."""
    sitemap = f"{SITE}/library-sitemap.xml"
    stored_model, old_model = f"{SITE}/library/riverlm3", f"{SITE}/library/riverlm1"
    site.routes[sitemap] = _xml(_urlset(stored_model, old_model))
    site.routes[old_model] = _once(503, _html(_fixture("ollama-library-model.html")))
    stored = _Stored(stored_model)
    plugin = _plugin(f"{SITE}/", sitemap_url=sitemap)
    assert await stored.poll(plugin) == []
    assert await stored.poll(plugin) == []
    assert site.requests.count(old_model) == 2
    assert old_model in plugin._extracted_urls


_BODY = "<p>" + "The council met on Tuesday and approved the plan for the trail. " * 14 + "</p>"


def _undated_page(title: str, og_type: str) -> tuple[int, str, str]:
    """A story page with no date of its own, tagged ``og_type``."""
    return _html(
        f'<html><head><meta property="og:type" content="{og_type}"><title>{title}</title></head>'
        f"<body><main><article><h1>{title}</h1>{_BODY}</article></main></body></html>"
    )


def _list_of(*urls: str) -> tuple[int, str, str]:
    """A listing page whose posts are ``urls``, in that order."""
    links = "".join(f'<li><a href="{u}">The story at {u}</a></li>' for u in urls)
    return _html(f"<html><body><main><ul>{links}</ul></main></body></html>")


def _news_sitemap(*entries: tuple[str, str]) -> tuple[int, str, str]:
    """A news sitemap: each ``(url, publication date)``, no ``<lastmod>``."""
    urls = "".join(
        f"<url><loc>{u}</loc><news:news><news:publication><news:name>Example</news:name>"
        "<news:language>en</news:language></news:publication>"
        f"<news:publication_date>{d}</news:publication_date>"
        "<news:title>A story</news:title></news:news></url>"
        for u, d in entries
    )
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        f'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{urls}</urlset>'
    )


async def test_a_root_feed_whose_news_sitemap_fails_for_one_poll_keeps_its_new_story(site):
    """Only the news sitemap dates the stories: their pages are tagged
    og:type=website and carry no date. It answers 503 for one poll while
    a new story is up, so the home page's links are read, where the new
    story has no date and is rejected. That poll read it without its
    usual read's evidence, so when the sitemap is back the story is read
    once more and comes in. main stored it then too."""
    older, old, new = (
        f"{SITE}/news/{slug}/"
        for slug in (
            "council-approves-river-trail", "harvest-market-returns", "library-extends-hours"
        )
    )
    for n, story in enumerate((older, old, new)):
        site.routes[story] = _undated_page(f"Story {n} from the council", "website")
    sitemap = f"{SITE}/news-sitemap.xml"
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {sitemap}\n")
    listed = _news_sitemap((new, "2026-10-16"), (old, "2026-10-14"), (older, "2026-10-12"))
    site.routes[sitemap] = _once(503, listed)
    site.routes[f"{SITE}/"] = _list_of(new, old, older)
    stored = _Stored(older, old)
    plugin = _plugin(f"{SITE}/")
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 1
    assert await stored.poll(plugin) == [new]
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 2


@pytest.mark.parametrize("robots", ["lists the sitemap", "refused"])
@pytest.mark.parametrize("bad_polls", [1, 2])
@pytest.mark.parametrize("link_pattern", [None, "/blog/"])
async def test_a_root_feed_whose_sitemap_fails_for_one_poll_does_not_drop_its_new_post(
    site, link_pattern, bad_polls, robots
):
    """Undated posts, a sitemap with no ``<lastmod>`` (newest first), and
    a home page that lists its posts by when they were last updated, so
    a stored post edited this morning is listed above the new one. The
    sitemap answers 503 for a poll or two while a new post is up. On the
    home page the new post is listed below the edited one, which is
    stored, so those polls drop it as backlog. It was judged by the home
    page's order, not the sitemap's, so when the sitemap is back the
    post is read once more (and not on each bad poll), listed above
    every stored post, and comes in. main stored it then too, with a
    link pattern or without. A site that refuses its robots.txt (the
    guessed ``/sitemap.xml`` is read) is no different: that refusal is
    read as no robots.txt, not as a sitemap that failed, so the poll
    whose sitemap is back is complete."""
    edited, new, older = (
        f"{SITE}/blog/{slug}/"
        for slug in (
            "second-post-on-the-rivers", "new-post-on-the-markets", "first-post-on-the-trails"
        )
    )
    for n, post in enumerate((edited, new, older)):
        site.routes[post] = _undated_page(f"Post {n} on the trails", "article")
    site.routes[f"{SITE}/robots.txt"] = (
        403 if robots == "refused" else _html(f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    )
    site.routes[f"{SITE}/sitemap.xml"] = _once(503, _xml(_urlset(new, edited, older)), times=bad_polls)
    site.routes[f"{SITE}/"] = _list_of(edited, new, older)
    stored = _Stored(edited)
    plugin = _plugin(f"{SITE}/", link_pattern=link_pattern)
    for _ in range(bad_polls):
        assert await stored.poll(plugin) == []
    assert new in plugin._extracted_urls
    assert site.requests.count(new) == 1
    assert await stored.poll(plugin) == [new]
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 2


@pytest.mark.parametrize("bad_polls", [1, 2])
@pytest.mark.parametrize("link_pattern", [None, "/blog/"])
async def test_a_section_feed_whose_listing_fails_for_one_poll_does_not_drop_its_new_post(
    site, link_pattern, bad_polls
):
    """The mirror case: a section feed's listing page answers 503 for a
    poll or two while a new post is up, so its section of the sitemap is
    read. That sitemap lists oldest first with no ``<lastmod>``, so the
    new (undated) post is listed below the stored ones: backlog. When the
    listing page is back, it lists the post first, and the post comes in.
    main, which read the sitemap, stored it, with a link pattern or
    without."""
    first, second, third, new = (f"{SITE}/blog/post-on-the-trails-{n}/" for n in range(4))
    for n, post in enumerate((first, second, third, new)):
        site.routes[post] = _undated_page(f"Post {n} on the trails", "article")
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(first, second, third, new))
    site.routes[f"{SITE}/blog"] = _once(503, _list_of(new, third, second, first), times=bad_polls)
    stored = _Stored(first, second, third)
    plugin = _plugin(f"{SITE}/blog", link_pattern=link_pattern)
    for _ in range(bad_polls):
        assert await stored.poll(plugin) == []
    assert new in plugin._extracted_urls
    assert site.requests.count(new) == 1
    assert await stored.poll(plugin) == [new]
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 2


@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout])
@pytest.mark.parametrize("layout", ["robots.txt", "sitemap index"])
async def test_a_news_sitemap_that_fails_beside_a_post_sitemap_keeps_its_new_story(
    site, layout, failure
):
    """The Yoast and News SEO layout: a news sitemap beside a post
    sitemap, listed in robots.txt or in a sitemap index. Only the news
    sitemap dates the stories (og:type=website pages with no date). It
    fails for one poll while a new story is up, and the post sitemap
    loads, so the poll's read is the usual one but lacks the news
    sitemap's dates: the new story is rejected. That read had a document
    fail, so the next complete read reads the story once more, and it
    comes in. main stored it on the first poll."""
    older, old, new = (
        f"{SITE}/news/{slug}/"
        for slug in (
            "council-approves-river-trail", "harvest-market-returns", "library-extends-hours"
        )
    )
    for n, story in enumerate((older, old, new)):
        site.routes[story] = _undated_page(f"Story {n} from the council", "website")
    news, posts = f"{SITE}/news-sitemap.xml", f"{SITE}/post-sitemap.xml"
    if layout == "robots.txt":
        site.routes[f"{SITE}/robots.txt"] = _html(
            f"User-agent: *\nSitemap: {news}\nSitemap: {posts}\n"
        )
    else:
        site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/sitemap_index.xml\n")
        site.routes[f"{SITE}/sitemap_index.xml"] = _xml(_index(posts, news))
    site.routes[news] = failure
    site.routes[posts] = _xml(_urlset(new, old, older))
    site.routes[f"{SITE}/"] = _list_of(new, old, older)
    stored = _Stored(older, old)
    plugin = _plugin(f"{SITE}/")
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 1
    site.routes[news] = _news_sitemap((new, "2026-10-16"), (old, "2026-10-14"), (older, "2026-10-12"))
    assert await stored.poll(plugin) == [new]
    assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 2
    # The home page was never read: the sitemaps found the stories.
    assert site.asked("/") == 0


async def test_a_news_sitemap_that_keeps_failing_does_not_reread_its_stories_each_poll(site):
    """While the news sitemap keeps failing, each poll's read lacks its
    dates, so a story such a poll read is not read again until a poll
    whose sitemaps all load."""
    old, new = (f"{SITE}/news/{slug}/" for slug in ("harvest-market-returns", "library-extends-hours"))
    for n, story in enumerate((old, new)):
        site.routes[story] = _undated_page(f"Story {n} from the council", "website")
    news, posts = f"{SITE}/news-sitemap.xml", f"{SITE}/post-sitemap.xml"
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {news}\nSitemap: {posts}\n")
    site.routes[news] = _once(503, _news_sitemap((new, "2026-10-16"), (old, "2026-10-14")), times=3)
    site.routes[posts] = _xml(_urlset(new, old))
    stored = _Stored(old)
    plugin = _plugin(f"{SITE}/")
    for _ in range(3):
        assert await stored.poll(plugin) == []
    assert site.requests.count(new) == 1
    assert await stored.poll(plugin) == [new]
    assert site.requests.count(new) == 2


async def test_the_sitemap_crawl_reports_the_documents_that_failed(site):
    """``discover_sitemap_entries`` collects every document that failed,
    robots.txt included: a refusal, a 5xx, a timeout. A 404 is a document
    that is gone, not one that failed, and a refused robots.txt is read
    as none. With every document loading, nothing is collected, and the
    plan reads as complete."""
    story = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/robots.txt"] = _html(
        "User-agent: *\n"
        + "".join(f"Sitemap: {SITE}/{name}-sitemap.xml\n" for name in ("news", "post", "blog", "story"))
    )
    site.routes[f"{SITE}/news-sitemap.xml"] = 503
    site.routes[f"{SITE}/post-sitemap.xml"] = _xml(_urlset(story))
    site.routes[f"{SITE}/blog-sitemap.xml"] = httpx.ReadTimeout
    site.routes[f"{SITE}/story-sitemap.xml"] = 403
    failures: list[BaseException] = []
    entries = await feed_autodiscovery.discover_sitemap_entries(f"{SITE}/", failures=failures)
    assert [e.url for e in entries] == [story]
    assert sorted(type(f).__name__ for f in failures) == [
        "FetchRefusedError", "HTTPStatusError", "ReadTimeout",
    ]
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert plan.mode == "sitemap" and plan.degraded and not plan.fallback

    # A refused robots.txt is read as none (the guessed paths are read),
    # not as a document that failed.
    site.routes[f"{SITE}/robots.txt"] = 403
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(story))
    failures = []
    entries = await feed_autodiscovery.discover_sitemap_entries(f"{SITE}/", failures=failures)
    assert [e.url for e in entries] == [story]
    assert failures == []
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert plan.mode == "sitemap" and not plan.degraded

    for name in ("news", "blog", "story"):
        site.routes[f"{SITE}/{name}-sitemap.xml"] = 404
    site.routes[f"{SITE}/robots.txt"] = 503
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(story))
    failures = []
    entries = await feed_autodiscovery.discover_sitemap_entries(f"{SITE}/", failures=failures)
    assert [e.url for e in entries] == [story]
    assert [f.response.status_code for f in failures] == [503]

    site.routes[f"{SITE}/robots.txt"] = 404
    failures = []
    await feed_autodiscovery.discover_sitemap_entries(f"{SITE}/", failures=failures)
    assert failures == []
    plan = await plan_candidates(f"{SITE}/", raise_errors=True)
    assert plan.mode == "sitemap" and not plan.degraded


@pytest.mark.parametrize(
    "pinned",
    [["welcome-to-the-trail-blog"], ["welcome-to-the-trail-blog", "how-to-read-our-trail-maps"]],
    ids=["one sticky post", "two sticky posts"],
)
async def test_a_pinned_post_above_new_posts_does_not_make_them_backlog(site, pinned):
    """A section feed's listing keeps its sticky posts at the top, in the
    same list as the rest, and its posts carry no date. Each poll a new
    post appears below the sticky ones and above the stored ones. The
    stored sticky posts are pinned, not the line between new and old:
    each new post comes in on its poll. main read the section's sitemap
    and stored each one."""
    sticky = [f"{SITE}/blog/{slug}/" for slug in pinned]
    olds = [f"{SITE}/blog/old-post-on-the-rivers-{n}/" for n in range(3)]
    news = [f"{SITE}/blog/new-post-on-the-markets-{n}/" for n in range(3)]
    for n, post in enumerate(sticky + olds + news):
        site.routes[post] = _undated_page(f"Post {n} on the trails", "article")
    stored = _Stored(*sticky, *olds)
    plugin = _plugin(f"{SITE}/blog")
    for k in range(3):
        site.routes[f"{SITE}/blog"] = _list_of(*sticky, *reversed(news[: k + 1]), *olds)
        assert await stored.poll(plugin) == [news[k]]
    assert all(site.requests.count(post) == 1 for post in news)


@pytest.mark.parametrize("leading", [1, 2])
async def test_a_listing_led_by_stored_posts_with_none_below_keeps_them_as_the_line(
    site, leading
):
    """Stored posts at the top of the listing and none further down: they
    are the line, as on any listing, so the undated pages below them are
    old backlog (after a restart, the feed reads again the pages it
    passed over). A lone stored post is the line too."""
    stored_posts = [f"{SITE}/blog/stored-post-on-the-rivers-{n}/" for n in range(leading)]
    olds = [f"{SITE}/blog/old-post-on-the-markets-{n}/" for n in range(3)]
    for n, post in enumerate(stored_posts + olds):
        site.routes[post] = _undated_page(f"Post {n} on the trails", "article")
    site.routes[f"{SITE}/blog"] = _list_of(*stored_posts, *olds)
    stored = _Stored(*stored_posts)
    plugin = _plugin(f"{SITE}/blog")
    assert await stored.poll(plugin) == []
    assert set(olds) <= set(plugin._extracted_urls)


@pytest.mark.parametrize(("posts", "children"), [(10, 12), (4, 6)])
async def test_a_category_pages_subcategory_links_do_not_outvote_its_posts(site, posts, children):
    """A section feed at a WordPress category (``/category/news/``) whose
    page links to more child categories than it lists posts, outside nav
    and aside. The child categories are under the feed's own section,
    but the listing is read with every segment judged first, so they are
    no candidates and the posts are the list: each is stored on the first
    poll, and no child category is fetched."""
    stories = [f"{SITE}/council-story-number-{n}-with-details/" for n in range(posts)]
    subs = [f"{SITE}/category/news/region-{n}/" for n in range(children)]
    chips = "".join(f'<li><a href="{u}">Region {n}</a></li>' for n, u in enumerate(subs))
    cards = "".join(
        f'<article><h2><a href="{u}">Council story number {n} with a long headline</a></h2></article>'
        for n, u in enumerate(stories)
    )
    site.routes[f"{SITE}/category/news/"] = _html(
        "<html><body><header><nav><a href='/'>Home</a></nav></header>"
        f"<div class='cat-header'><h1>News</h1><ul class='subcats'>{chips}</ul></div>"
        f"<main>{cards}</main></body></html>"
    )
    for story in stories:
        site.routes[story] = _html(_fixture("yoast-post.html"))
    plugin = _plugin(f"{SITE}/category/news/")
    items = await plugin.fetch()
    assert [i["url"] for i in items] == stories
    assert not any(site.asked(u.removeprefix(SITE)) for u in subs)


@pytest.mark.parametrize(
    ("name", "feed"),
    [
        ("media_release-sitemap.xml", "root"),
        ("media_release-sitemap.xml", "root and sitemap_url"),
        ("media_release-sitemap.xml", "section"),
        ("media_release-sitemap.xml", "section and sitemap_url"),
        ("product_update-sitemap.xml", "section"),
        ("wp-sitemap-posts-product_update-1.xml", "section"),
        ("wp-sitemap-posts-media_release-1.xml", "section"),
        ("video-sitemap.xml", "section"),
        ("video-sitemap.xml", "root"),
        # A post type's name may hold a "-" (round 7).
        ("wp-sitemap-posts-media-release-1.xml", "root"),
        ("wp-sitemap-posts-media-release-1.xml", "section"),
        ("wp-sitemap-posts-product-update-1.xml", "root"),
        ("sitemap-media-releases.xml", "root"),
    ],
)
async def test_a_custom_post_types_sitemap_is_read(site, name, feed):
    """A WordPress site whose stories are a custom post type named with a
    media or product word (``media_release``, ``product_update``, or
    ``video``, whose Yoast sitemap is ``video-sitemap.xml``), beside a
    page sitemap, and whose pages list nothing in their HTML (rendered by
    script). The type's sitemap lists the stories: it is read, and the
    stories come in, as main stored them. The page sitemap is still read
    last and gives nothing."""
    section = "/media-releases/" if "media" in name else "/updates/"
    stories = [f"{SITE}{section}council-approves-river-trail-plan-{n}/" for n in range(3)]
    index = f"{SITE}/sitemap_index.xml"
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {index}\n")
    site.routes[index] = _xml(_index(f"{SITE}/page-sitemap.xml", f"{SITE}/{name}"))
    site.routes[f"{SITE}/page-sitemap.xml"] = _xml(_urlset(f"{SITE}/about/", f"{SITE}/contact/"))
    site.routes[f"{SITE}/{name}"] = _xml(_urlset(*stories))
    for story in stories:
        site.routes[story] = _html(_fixture("yoast-post.html"))
    empty = _html("<html><body><main><h1>News</h1><div id='app'></div></main></body></html>")
    site.routes[f"{SITE}/"] = empty
    site.routes[f"{SITE}{section.rstrip('/')}"] = empty
    url = f"{SITE}{section.rstrip('/')}" if feed.startswith("section") else f"{SITE}/"
    sitemap_url = index if feed.endswith("sitemap_url") else None
    items = await _plugin(url, sitemap_url=sitemap_url).fetch()
    assert sorted(i["url"] for i in items) == stories
    assert site.asked(f"/{name}") == 1


@pytest.mark.parametrize("ending", ["jhtml", "jspx", "shtm", "dhtml", "nsf", "lasso"])
@pytest.mark.parametrize("feed", ["root", "section"])
async def test_a_server_page_ending_is_an_article_page(site, ending, feed):
    """Article pages whose URLs end in a server page's ending that
    neither courlan nor ``article_detect`` knows (ATG's ``.jhtml``,
    ``.jspx``, ``.shtm``, ``.dhtml``, Domino's ``.nsf``, ``.lasso``),
    listed by the sitemap and the listing page, with no link pattern.
    main stored them from the sitemap, which lists pages: its entries
    are read as pages. The listing page's links are judged as courlan
    judged them on main, so a section feed's listing gives no list, and
    its section of the sitemap brings the stories in."""
    stories = [f"{SITE}/news/story-{n}-council-approves-plan.{ending}" for n in range(3)]
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*stories))
    site.routes[f"{SITE}/"] = _list_of(*stories)
    site.routes[f"{SITE}/news"] = _list_of(*stories)
    for story in stories:
        site.routes[story] = _html(_fixture("yoast-post.html"))
    url = f"{SITE}/news" if feed == "section" else f"{SITE}/"
    items = await _plugin(url).fetch()
    assert sorted(i["url"] for i in items) == stories
    assert site.asked("/sitemap.xml") == 1


async def test_a_listing_cards_date_dates_a_page_that_carries_none(site):
    """A builder's post page tagged og:type=website with no date of its
    own: the listing card's ``<time datetime>`` is the site's date for
    it, so it is an article, and the story carries that date."""
    base = f"{SITE}/news/"
    dates = ["2026-10-14", "2026-10-12", "2026-10-09"]
    posts = [f"{base}story-{n}/" for n in range(3)]
    cards = "".join(
        f'<article><h2><a href="{p}">Story {n}</a></h2><time datetime="{d}">{d}</time></article>'
        for n, (p, d) in enumerate(zip(posts, dates, strict=True))
    )
    site.routes[base] = _html(f"<html><body><main><h1>News</h1>{cards}</main></body></html>")
    body = "<p>" + "The council met on Tuesday and approved the plan for the trail. " * 14 + "</p>"
    page = (
        '<html><head><meta property="og:type" content="website"><title>{t}</title></head>'
        "<body><main><h1>{t}</h1>" + body + "</main></body></html>"
    )
    for n, post in enumerate(posts):
        site.routes[post] = _html(page.format(t=f"Story {n} from the council"))
    items = await _plugin(base).fetch()
    assert [i["url"] for i in items] == posts
    assert [i["published_at"].date().isoformat() for i in items] == dates


async def test_an_astro_style_post_in_a_sitemap_is_an_article(site):
    post = f"{SITE}/blog/rebuilding-the-river-trail-bridge/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(post))
    site.routes[post] = _html(_fixture("astro-post.html"))
    items = await _plugin(f"{SITE}/").fetch()
    assert [i["url"] for i in items] == [post]


@pytest.mark.parametrize("read", ["sitemap", "sitemap under the link pattern", "listing page"])
async def test_a_slug_that_ends_in_a_software_name_is_an_article(site, read):
    """Hugo and Eleventy keep a slug's dots before the trailing slash,
    and Jekyll keeps them under its ``.html``: ``vue.js`` there is a word
    of the slug, not a file. main stored these pages from a sitemap and
    under a link pattern."""
    posts = [
        f"{SITE}/posts/getting-started-with-three.js/",
        f"{SITE}/posts/understanding-package.json/",
        f"{SITE}/posts/why-i-moved-off-tailwind.css/",
        f"{SITE}/posts/2026/10/18/whats-new-in-vue.js.html",
    ]
    astro = "https://www.example.com/blog/rebuilding-the-river-trail-bridge/"
    for post in posts:
        site.routes[post] = _html(_fixture("astro-post.html").replace(astro, post))
    if read == "listing page":
        links = "".join(f'<li><a href="{p}">Post</a></li>' for p in posts)
        site.routes[f"{SITE}/posts/"] = _html(f"<html><body><main><ul>{links}</ul></main></body></html>")
        plugin = _plugin(f"{SITE}/posts/", link_pattern="/posts/")
    else:
        site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*posts))
        pattern = "/posts/" if read == "sitemap under the link pattern" else None
        plugin = _plugin(f"{SITE}/", link_pattern=pattern)
    items = await plugin.fetch()
    assert sorted(i["url"] for i in items) == sorted(posts)


def _news_under_about(site: _Site, section: str) -> list[str]:
    """A council-style site that files its news under a static section:
    robots.txt names a sitemap that lists three dated stories under
    ``section`` and the About page itself, and both the section page and
    the home page list the stories. Returns the stories, newest first."""
    stories = [
        f"{SITE}{section}{slug}"
        for slug in ("council-approves-river-trail", "harvest-market-returns", "new-library-hours")
    ]
    dates = ["2026-10-18", "2026-10-15", "2026-10-12"]
    astro = "https://www.example.com/blog/rebuilding-the-river-trail-bridge/"
    for story in stories:
        site.routes[story] = _html(_fixture("astro-post.html").replace(astro, story))
    entries = "".join(
        f"<url><loc>{u}</loc><lastmod>{d}</lastmod></url>"
        for u, d in zip(stories, dates, strict=True)
    )
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}<url><loc>{SITE}/about/</loc><lastmod>2026-10-19</lastmod></url></urlset>"
    )
    cards = "".join(
        f'<li><h2><a href="{u}">Story {n}</a></h2><time datetime="{d}">{d}</time></li>'
        for n, (u, d) in enumerate(zip(stories, dates, strict=True))
    )
    listing = _html(
        '<html><body><header><nav><a href="/">Home</a><a href="/about/">About</a></nav></header>'
        f"<main><h1>News</h1><ul>{cards}</ul></main></body></html>"
    )
    site.routes[f"{SITE}{section.rstrip('/')}"] = listing
    site.routes[f"{SITE}/"] = listing
    return stories


@pytest.mark.parametrize(
    ("section", "feed"),
    [
        ("/about/news/", "section"),
        ("/about/news/", "section and link pattern"),
        ("/about/news/", "root and sitemap_url"),
        ("/about/news/", "root"),
        ("/about-us/news/", "root"),
        # A section named like no news section: a page below it is
        # judged like any other (round 7), so a root feed reads it too.
        ("/about/media-centre/", "section"),
        ("/about/media-centre/", "section and link pattern"),
        ("/about/media-centre/", "link pattern"),
        ("/about/media-centre/", "root"),
        ("/about-us/news-and-media/", "root"),
        ("/about-us/news-and-media/", "section"),
    ],
)
async def test_articles_under_an_about_section_are_articles(site, section, feed):
    """Councils, universities and companies file their news under a
    static section (``/about/news/<slug>``). main stored those stories
    in every one of these setups, because it never ran ``url_ok`` on a
    sitemap or under a link pattern. Each feed's usual read finds them
    on its own: a section feed never asks for the sitemap, and a root
    feed never asks for its home page. The About page itself is still
    never fetched."""
    stories = _news_under_about(site, section)
    pattern = section if "link pattern" in feed else None
    url = f"{SITE}{section.rstrip('/')}" if feed.startswith("section") else f"{SITE}/"
    sitemap_url = f"{SITE}/sitemap.xml" if feed == "root and sitemap_url" else None
    plugin = _plugin(url, link_pattern=pattern, sitemap_url=sitemap_url)
    items = await plugin.fetch()
    assert [i["url"] for i in items] == stories
    if feed.startswith("section"):
        assert site.asked("/robots.txt") == site.asked("/sitemap.xml") == 0
    else:
        assert site.asked("/") == 0
    assert site.asked("/about/") == 0
    assert await plugin.fetch() == []


async def test_auto_discovery_finds_a_sitemap_of_news_under_an_about_section(site):
    stories = _news_under_about(site, "/about/news/")
    assert await feed_autodiscovery.discover_sitemap_urls(f"{SITE}/", limit=1) == stories[:1]


async def test_links_under_the_link_pattern_are_the_owners_choice(site):
    """A one-word page name courlan reads as an index page (``home``) is
    still a candidate under the owner's link pattern; without one,
    ``listing_links`` leaves it out."""
    page = f"{SITE}/search?o=newest"
    names = ("home", "riverlm3", "riverlm2")
    links = "".join(f'<li><a href="/library/{m}">{m}</a></li>' for m in names)
    site.routes[page] = _html(f"<html><body><main><ul>{links}</ul></main></body></html>")
    plan = await plan_candidates(page, link_pattern="/library/", raise_errors=True)
    assert [c.url for c in plan.candidates] == [f"{SITE}/library/{m}" for m in names]
    plan = await plan_candidates(page)
    assert f"{SITE}/library/home" not in [c.url for c in plan.candidates]


async def test_sitemap_entries_under_the_link_pattern_are_the_owners_choice(site):
    """The same rule in the sitemap read: a site root with a link
    pattern reads its sitemap first, and a one-word page name courlan
    rejects (``home``) is still a candidate under the owner's pattern."""
    models = [f"{SITE}/library/home", f"{SITE}/library/riverlm3"]
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(*models))
    plan = await plan_candidates(f"{SITE}/", link_pattern="/library/", raise_errors=True)
    assert plan.mode == "sitemap"
    assert [c.url for c in plan.candidates] == models
    plan = await plan_candidates(f"{SITE}/")
    assert [c.url for c in plan.candidates] == [f"{SITE}/library/riverlm3"]


async def test_without_a_link_pattern_the_same_page_is_not_an_article(site):
    # Same site read as a sitemap feed: no owner signal, so the model
    # page (no article type, no date) is rejected.
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(f"{SITE}/library/riverlm3"))
    site.routes[f"{SITE}/library/riverlm3"] = _html(_fixture("ollama-library-model.html"))
    plugin = _plugin(f"{SITE}/")
    assert await plugin.fetch() == []
    assert f"{SITE}/library/riverlm3" in plugin._extracted_urls


# --- the feed's headers -------------------------------------------------------


def _safari_only(route):
    """A route the site serves only to a Safari User-Agent (a WAF that
    refuses the default one): 403 for anyone else."""

    async def handler(request):
        if "Safari" in request.headers.get("user-agent", ""):
            return route
        return 403

    return handler


async def test_the_feeds_headers_reach_robots_txt_every_sitemap_and_each_page(site):
    """Read as Safari has to reach every request a website feed makes:
    robots.txt, the sitemap index (listed only in robots.txt), the post
    sitemap and the article page."""
    post = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/robots.txt"] = _safari_only(_html(f"Sitemap: {SITE}/sitemaps/index.xml\n"))
    site.routes[f"{SITE}/sitemaps/index.xml"] = _safari_only(_xml(_index(f"{SITE}/sitemaps/posts.xml")))
    site.routes[f"{SITE}/sitemaps/posts.xml"] = _safari_only(_xml(_urlset(post)))
    site.routes[post] = _safari_only(_html(_fixture("yoast-post.html")))
    site.routes[f"{SITE}/"] = _safari_only(_html(_NO_LIST_PAGE))

    assert await _kind_of(_plugin(f"{SITE}/").fetch()) == "blocked"

    site.agents.clear()
    items = await _plugin(f"{SITE}/", custom_headers={"User-Agent": SAFARI}).fetch()
    assert [i["url"] for i in items] == [post]
    asked = [url for url, _ in site.agents]
    for url in (f"{SITE}/robots.txt", f"{SITE}/sitemaps/index.xml", f"{SITE}/sitemaps/posts.xml", post):
        assert url in asked
    assert all(agent == SAFARI for _, agent in site.agents), site.agents


async def test_the_feeds_headers_reach_its_listing_page(site):
    base = f"{SITE}/news/"
    posts = [f"{base}story-{n}/" for n in range(3)]
    cards = "".join(f'<article><h2><a href="{p}">Story {n}</a></h2></article>' for n, p in enumerate(posts))
    site.routes[base] = _safari_only(_html(f"<html><body><main>{cards}</main></body></html>"))
    for post in posts:
        site.routes[post] = _safari_only(_html(_fixture("yoast-post.html")))

    assert await _kind_of(_plugin(base).fetch()) == "blocked"

    site.agents.clear()
    items = await _plugin(base, custom_headers={"User-Agent": SAFARI}).fetch()
    assert [i["url"] for i in items] == posts
    assert site.agents[0][0] == base
    assert all(agent == SAFARI for _, agent in site.agents), site.agents


# --- callers that never raise -------------------------------------------------


async def test_probe_and_auto_discovery_never_raise(site):
    for path in ("/robots.txt", "/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/", "/news/"):
        site.routes[SITE + path] = 403
    assert await probe(f"{SITE}/") == []
    assert await probe(f"{SITE}/news/") == []
    assert await feed_autodiscovery.discover_sitemap_urls(f"{SITE}/") == []


async def test_probe_skips_refused_pages(site):
    ok = f"{SITE}/news/council-approves-river-trail/"
    site.routes[f"{SITE}/sitemap.xml"] = _xml(_urlset(f"{SITE}/news/harvest-market-returns/", ok))
    site.routes[f"{SITE}/news/harvest-market-returns/"] = 403
    site.routes[ok] = _html(_fixture("yoast-post.html"))
    items = await probe(f"{SITE}/")
    assert [i["url"] for i in items] == [ok]


async def test_fetch_html_checked_refusal_is_an_http_status_error(site):
    site.routes[f"{SITE}/x"] = 402
    with pytest.raises(FetchRefusedError) as caught:
        await article_extract.fetch_html_checked(f"{SITE}/x")
    assert isinstance(caught.value, httpx.HTTPStatusError)
    assert caught.value.response.status_code == 402
    # fetch_html itself still returns None for the same answer.
    assert await article_extract.fetch_html(f"{SITE}/x") is None
    # Other statuses read as an empty document unless asked for.
    site.routes[f"{SITE}/y"] = 503
    assert await article_extract.fetch_html_checked(f"{SITE}/y") is None
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await article_extract.fetch_html_checked(f"{SITE}/y", raise_for_status=True)
    assert _classify_error(caught.value) == "upstream error"


async def test_a_redirect_reports_the_final_url(site):
    async def moved(request):
        return httpx.Response(301, headers={"location": f"{SITE}/new-home/"})

    site.routes[f"{SITE}/old/"] = moved
    site.routes[f"{SITE}/new-home/"] = _html("<html><body>home</body></html>")
    body, final_url = await article_extract.fetch_html_checked(f"{SITE}/old/", return_final_url=True)
    assert final_url == f"{SITE}/new-home/"
    assert "home" in body


async def test_candidates_carry_news_sitemap_titles_and_dates(site):
    site.routes[f"{SITE}/news-sitemap.xml"] = _xml(_fixture("news-sitemap.xml"))
    plan = await plan_candidates(f"{SITE}/", sitemap_url=f"{SITE}/news-sitemap.xml", raise_errors=True)
    assert plan.candidates and all(isinstance(c, Candidate) for c in plan.candidates)
    assert all(c.title and c.published for c in plan.candidates)


# --- round 7 ------------------------------------------------------------------


def _story_page(title: str) -> tuple[int, str, str]:
    page = _fixture("yoast-post.html")
    return _html(page.replace("Council approves the river trail extension", title))


@pytest.mark.parametrize(
    "folder", ["council-approves-river-trail-plan-{n}", "2026101{n}"], ids=["slug", "date"]
)
@pytest.mark.parametrize("page", ["index.html", "default.aspx"])
@pytest.mark.parametrize("feed", ["root", "section", "sitemap_url"])
async def test_stories_published_as_folder_index_pages_are_articles(site, folder, page, feed):
    """A static or exported site publishes each story as a folder with its
    own index page (``/news/<slug>/index.html``), listed by its sitemap
    and its news page. ``url_ok`` read such a page as a home page, so the
    feed read ``[no list]`` where main stored the stories (round 7)."""
    stories = [f"{SITE}/news/{folder.format(n=n)}/{page}" for n in range(3)]
    entries = "".join(
        f"<url><loc>{u}</loc><lastmod>2026-10-1{8 - n}</lastmod></url>" for n, u in enumerate(stories)
    )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )
    site.routes[f"{SITE}/robots.txt"] = _html(f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n")
    site.routes[f"{SITE}/sitemap.xml"] = _xml(sitemap)
    site.routes[f"{SITE}/news"] = _list_of(*stories)
    site.routes[f"{SITE}/"] = _html(_NO_LIST_PAGE)
    for n, story in enumerate(stories):
        site.routes[story] = _story_page(f"Council story number {n} about the trail")
    url = f"{SITE}/news" if feed == "section" else f"{SITE}/"
    sitemap_url = f"{SITE}/sitemap.xml" if feed == "sitemap_url" else None
    items = await _plugin(url, sitemap_url=sitemap_url).fetch()
    assert sorted(i["url"] for i in items) == sorted(stories)


@pytest.mark.parametrize(
    "section", ["/about-us/news-and-media/", "/about/media-centre/", "/about/press-room/"]
)
@pytest.mark.parametrize("quick_links", [3, 4])
async def test_a_newsroom_under_a_static_section_reads_its_posts_beside_quick_links(
    site, section, quick_links
):
    """A council's newsroom filed under About, whose page also has a block
    of quick links ("Popular services") in its body, outside nav and
    aside. Its releases were read as static pages, so the quick links
    were the only list of 3 or more: their pages were fetched and
    rejected, and the feed stored nothing and said nothing, where main
    stored every release from the sitemap (round 7)."""
    releases = [f"{SITE}{section}council-media-release-number-{n}/" for n in range(10)]
    services = [
        f"{SITE}/services/{name}/"
        for name in ("garbage-and-recycling", "property-taxes", "building-permits", "parks")
    ][:quick_links]
    cards = "".join(
        f'<div class="views-row"><h3><a href="{u}">Council media release number {n}</a></h3></div>'
        for n, u in enumerate(releases)
    )
    quick = "".join(f'<li><a href="{u}">Service</a></li>' for u in services)
    site.routes[f"{SITE}{section.rstrip('/')}"] = _html(
        "<html><body><header><nav><a href='/'>Home</a></nav></header>"
        f"<main><h1>News and media</h1><div class='view-content'>{cards}</div>"
        f"<div class='popular-services'><h2>Popular services</h2><ul>{quick}</ul></div></main>"
        "</body></html>"
    )
    for n, release in enumerate(releases):
        site.routes[release] = _story_page(f"Council media release number {n}")
    items = await _plugin(f"{SITE}{section.rstrip('/')}").fetch()
    assert [i["url"] for i in items] == releases
    assert not any(site.asked(u.removeprefix(SITE)) for u in services)


def _builder_post(title: str, meta: str, og_type: str | None) -> tuple[int, str, str]:
    """A site builder's post: no ``<article>``, no JSON-LD and no date
    metadata, the date (``meta``) right after its ``<h1>``."""
    og = f'<meta property="og:type" content="{og_type}">' if og_type else ""
    return _html(
        f"<html><head><title>{title} | Example</title>{og}</head><body>"
        "<div class='navbar'><a href='/'>Example</a></div><div class='section'><div class='container'>"
        f"<h1 class='heading'>{title}</h1>{meta}<div class='rich-text'>{_BODY}{_BODY}</div>"
        "</div></div></body></html>"
    )


_WEBFLOW_DATE = "<div class='blog-date'>October {day}, 2026</div>"
_BLOCK_THEME_DATE = (
    "<div class='wp-block-post-date'><time datetime='2026-10-{day}T09:00:00+00:00'>"
    "October {day}, 2026</time></div>"
)


@pytest.mark.parametrize(
    ("meta", "og_type"),
    [(_WEBFLOW_DATE, "website"), (_BLOCK_THEME_DATE, None)],
    ids=["webflow", "block theme"],
)
@pytest.mark.parametrize("feed", ["root", "section"])
async def test_a_site_builders_posts_dated_by_their_title_are_articles(site, meta, og_type, feed):
    """Webflow tags every page og:type=website and prints a post's date as
    text under its title; a WordPress block theme prints it in a plain
    ``<time>`` beside the title, with no ``<article>``. Their posts read
    as undated (-3), so the feed stored nothing and said nothing, where
    main stored every post (round 7). The About page, with no date by its
    title, is still left out."""
    posts = [f"{SITE}/blog/council-story-number-{n}" for n in range(3)]
    for n, post in enumerate(posts):
        site.routes[post] = _builder_post(
            f"Council story number {n} about the trail", meta.format(day=18 - n), og_type
        )
    site.routes[f"{SITE}/about"] = _builder_post("About us", "<p>Who we are.</p>", og_type)
    entries = "".join(
        f"<url><loc>{u}</loc><lastmod>2026-10-{d}</lastmod></url>"
        for u, d in [
            (f"{SITE}/", 19), (f"{SITE}/about", 19), *((p, 18 - n) for n, p in enumerate(posts))
        ]
    )
    site.routes[f"{SITE}/sitemap.xml"] = _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    )
    cards = "".join(
        f'<div role="listitem" class="w-dyn-item"><a href="{p}" class="blog-card">Story {n}</a></div>'
        for n, p in enumerate(posts)
    )
    site.routes[f"{SITE}/blog"] = _html(
        "<html><body><main><h1>Blog</h1>"
        f'<div role="list" class="w-dyn-items">{cards}</div></main></body></html>'
    )
    url = f"{SITE}/blog" if feed == "section" else f"{SITE}/"
    items = await _plugin(url).fetch()
    assert [i["url"] for i in items] == posts


@pytest.mark.parametrize("sticky", [2, 3])
async def test_sticky_posts_stored_by_a_first_fetch_do_not_make_new_posts_backlog(site, sticky):
    """A new section feed whose listing pins two or three sticky posts
    above the newest ones, all undated. The first fetch keeps two undated
    pages (the cap): the sticky ones at the top. The posts below them
    were read and passed over, so on later polls they show the sticky
    posts are pinned, and each new post comes in on its poll, once. They
    were the line before, so every new post was dropped for good, where
    main stored each one (round 7)."""
    pinned = [f"{SITE}/blog/welcome-to-the-blog-part-{n}/" for n in range(sticky)]
    olds = [f"{SITE}/blog/old-post-on-the-rivers-{n}/" for n in range(10)]
    news = [f"{SITE}/blog/new-post-on-the-markets-{n}/" for n in range(3)]
    for n, post in enumerate(pinned + olds + news):
        site.routes[post] = _undated_page(f"Post {n} on the trails", "article")
    stored = _Stored()
    plugin = _plugin(f"{SITE}/blog")
    site.routes[f"{SITE}/blog"] = _list_of(*pinned, *olds)
    assert await stored.poll(plugin) == pinned[:2]
    for k in range(3):
        site.routes[f"{SITE}/blog"] = _list_of(*pinned, *reversed(news[: k + 1]), *olds)
        assert await stored.poll(plugin) == [news[k]]
    assert all(site.requests.count(post) == 1 for post in news)


@pytest.mark.parametrize(
    "feed",
    ["the sitemap itself", "sitemap_url beside a rendered section", "sitemap_url beside the home page"],
)
async def test_an_empty_news_sitemap_between_stories_is_a_quiet_poll(site, feed):
    """A news sitemap lists only the last two days, so between stories it
    is an empty urlset. A feed that reads it (its address, or its
    ``sitemap_url`` beside a page that lists nothing in its HTML) had a
    poll with nothing new then, on main; it read ``[no list]`` (Pick a
    section, the wrong fix) until round 7."""
    sitemap = f"{SITE}/news-sitemap.xml"
    first, second = f"{SITE}/news/council-approves-river-trail/", f"{SITE}/news/harvest-market-returns/"
    for story in (first, second):
        site.routes[story] = _html(_fixture("yoast-post.html"))
    rendered = _html("<html><body><main><h1>News</h1><div id='app'></div></main></body></html>")
    site.routes[f"{SITE}/news"] = rendered
    site.routes[f"{SITE}/"] = rendered
    if feed == "the sitemap itself":
        plugin = _plugin(sitemap)
    else:
        url = f"{SITE}/news" if "section" in feed else f"{SITE}/"
        plugin = _plugin(url, sitemap_url=sitemap)
    stored = _Stored()
    site.routes[sitemap] = _news_sitemap((first, "2026-10-18T09:30:00+00:00"))
    assert await stored.poll(plugin) == [first]
    site.routes[sitemap] = _xml(_urlset())
    assert await stored.poll(plugin) == []
    assert await stored.poll(plugin) == []
    site.routes[sitemap] = _news_sitemap((second, "2026-10-20T09:30:00+00:00"))
    assert await stored.poll(plugin) == [second]


@pytest.mark.parametrize(
    ("listing", "kind"),
    [(403, "blocked"), (404, "not found"), (503, "upstream error"), (httpx.ConnectError, "network")],
)
async def test_an_empty_sitemap_url_still_reports_its_listing_pages_failure(site, listing, kind):
    """The empty sitemap is quiet only when the other read failed in no
    way: a refused, missing, failing or unreachable listing page still
    says what it is."""
    site.routes[f"{SITE}/news-sitemap.xml"] = _xml(_urlset())
    site.routes[f"{SITE}/news"] = listing
    plugin = _plugin(f"{SITE}/news", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert await _kind_of(plugin.fetch()) == kind


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (403, "blocked"),
        (404, "not found"),
        (httpx.ReadTimeout, "network"),
        (_html(_NO_LIST_PAGE), "no list"),
        (_xml(_urlset(f"{SITE}/about/", f"{SITE}/contact-us")), "no list"),
    ],
    ids=["refused", "gone", "timeout", "a page", "only static pages"],
)
async def test_a_sitemap_url_that_is_not_an_empty_sitemap_still_reads_as_before(site, answer, kind):
    """Only a sitemap that loads and names nothing is quiet. One that is
    refused, gone or times out says so, and one that is a web page or
    lists only pages that aren't articles has no list."""
    site.routes[f"{SITE}/news-sitemap.xml"] = answer
    site.routes[f"{SITE}/news"] = _html(_NO_LIST_PAGE)
    plugin = _plugin(f"{SITE}/news", sitemap_url=f"{SITE}/news-sitemap.xml")
    assert await _kind_of(plugin.fetch()) == kind
