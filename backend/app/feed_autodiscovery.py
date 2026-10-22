"""Auto-discover a feed for a URL the user pastes into "Add custom" —
either an existing RSS/Atom feed, or (when none exists) a sitemap-
derived list of article URLs suitable for the generic-scrape fallback
source type (``app.sources.generic_scrape``).

Uses trafilatura's feed detection (``trafilatura.feeds``) rather than
reimplementing "search a page for <link rel=alternate>" ourselves — well-tested, actively maintained, and already a project
dependency (added in ``app.article_extract`` for full-article LLM
summaries).

Which sitemaps are worth reading and which of their URLs could be
articles is decided by ``app.article_detect`` (``order_sitemaps``,
``url_ok``).

Network access: every request goes through
``app.article_extract.fetch_html`` (or ``fetch_html_checked``, its
raising form for the sitemap crawl), which applies the SSRF guard to
the URL and to every redirect hop. trafilatura is used only to parse
what we fetched (``determine_feed``); it never fetches anything itself,
because its own downloader has no hook for that guard.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from urllib.parse import urlparse

import httpx
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as DefusedET

from app.article_detect import Candidate, order_sitemaps, parse_date, same_site, url_ok
from app.article_extract import (
    NETWORK_ERRORS,
    FetchRefusedError,
    fetch_html,
    fetch_html_checked,
    most_telling_error,
    unresolved_host_error,
)
from app.sources.rss import fetch_rss
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.feed_autodiscovery")

_MAX_FEED_CANDIDATES = 5
_MAX_SITEMAP_URLS = 50

# Per-stage timeouts for the autodiscovery flow.
#
# The /api/sources/auto endpoint is synchronous from the user's
# perspective ("click Add Custom, wait for a toast"). The discovery
# chain — initial fetch_rss + trafilatura.find_feed_urls + a per-
# candidate fetch_rss loop — runs WITHOUT an outer endpoint
# timeout, so the worst case is unbounded (``fetch_rss`` uses
# ``_READ_TIMEOUT=60.0`` × 2 attempts = 120s per candidate).
# Observed: a slow CDN makes the endpoint hang 5-10 minutes, which
# the user reads as "the autofeed button is broken".
#
# Bounded here per stage so every link in the chain has a sane
# deadline. The numbers are chosen to be larger than the happy
# path (most feeds discover in <2s) but tight enough that a hung
# host gets returned-as-not-found within a small multiple of the
# happy-path wall time, not minutes:
#
#   _INITIAL_PROBE_BUDGET    10s  the user-supplied URL is a direct feed
#                              (fast probe; if the URL itself isn't a
#                              feed we move on quickly)
#   _TRAFILATURA_BUDGET      30s  fetching + scanning the page for feed
#                              links, or the sitemap crawl (robots.txt,
#                              guessed paths, nested sitemap indexes)
#   _CANDIDATE_PROBE_BUDGET  20s  per-candidate fetch_rss; shorter than
#                              60s because we have many candidates and
#                              the diminishing-return on slow feeds is
#                              steep — if the first candidate takes
#                              >20s, the rest will too, and the user
#                              would rather see a fast "not a feed"
#                              than a 5-min wait
#   _DISCOVERY_TOTAL_BUDGET  60s  outer cap on the whole flow inside
#                              discover_feed_url itself; protects
#                              against the worst-case scenario where
#                              every candidate narrowly misses each
#                              per-stage cap and adds up
_INITIAL_PROBE_BUDGET = 10.0
_TRAFILATURA_BUDGET = 30.0
_CANDIDATE_PROBE_BUDGET = 20.0
_DISCOVERY_TOTAL_BUDGET = 60.0


async def discover_feed_url(page_url: str) -> str | None:
    """Return a working RSS/Atom feed URL for ``page_url``, or None
    if none could be found within the discovery time budget.

    Tries ``page_url`` itself first — the user may have pasted a feed
    URL directly, which is the common case and needs no discovery at
    all — then asks trafilatura for candidate feed URLs on the page
    and tries each in turn until one actually parses with at least
    one item. A candidate merely existing in the page's markup isn't
    enough to trust; feed-detection heuristics false-positive on
    stale/broken `<link>` tags often enough that "does it actually
    parse" is the only real test.

    Every external hop is wrapped in ``asyncio.wait_for`` so a
    host that hangs on connect, read, or any internal trafilatura
    call can't hold the request open for minutes. Timeouts are
    treated the same as a network failure — fall through to the
    next stage. The user-visible behavior is "took too long,
    not a feed after all"; see the per-stage budget module
    docstring for the rationale.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        return None

    async def _probe_initial() -> str | None:
        """Phase 1: a quick probe of the user-supplied URL itself."""
        items = await fetch_rss(page_url)
        return page_url if items else None

    try:
        result = await asyncio.wait_for(_probe_initial(), _INITIAL_PROBE_BUDGET)
        if result:
            return result
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: initial probe of %s hit the %ss budget; "
            "falling through to trafilatura",
            page_url, _INITIAL_PROBE_BUDGET,
        )
    except Exception:  # noqa: BLE001 - not itself a feed; fall through to discovery
        pass

    async def _trafilatura_find() -> list[str]:
        # Lazy import: trafilatura loads lxml at import time.
        from trafilatura.feeds import FeedParameters, determine_feed, get_hostinfo

        # determine_feed scans the page for <link rel=alternate> tags and
        # common feed paths and returns candidate feed URLs. (Not
        # trafilatura.feeds.find_feed_urls: that dereferences the feed
        # and returns the article links inside it.)
        domain, baseurl = get_hostinfo(page_url)
        if domain is None:
            logger.info(
                "feed_autodiscovery: no domain in %s — giving up before fetching",
                page_url,
            )
            return []
        html = await fetch_html(page_url)
        if html is None:
            logger.info(
                "feed_autodiscovery: could not fetch %s — no page to scan for feed links",
                page_url,
            )
            return []
        params = FeedParameters(baseurl, domain, page_url, False, None)
        found = await asyncio.to_thread(determine_feed, html, params)
        logger.info(
            "feed_autodiscovery: determine_feed found %d candidate(s) for %s: %s",
            len(found), page_url, found[:_MAX_FEED_CANDIDATES],
        )
        return found

    try:
        candidates = await asyncio.wait_for(_trafilatura_find(), _TRAFILATURA_BUDGET)
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: determine_feed for %s hit the "
            "%ss budget; giving up",
            page_url, _TRAFILATURA_BUDGET,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - third-party discovery, never let it fail the request
        logger.info("feed_autodiscovery: determine_feed failed for %s: %s", page_url, exc)
        return None

    async def _probe_candidate(candidate: str) -> str | None:
        """Phase 2: probe a single trafilatura-discovered candidate."""
        items = await fetch_rss(candidate)
        return candidate if items else None

    for candidate in candidates[:_MAX_FEED_CANDIDATES]:
        try:
            result = await asyncio.wait_for(_probe_candidate(candidate), _CANDIDATE_PROBE_BUDGET)
            if result:
                return result
            logger.info(
                "feed_autodiscovery: candidate %s for %s fetched but "
                "didn't parse as a feed with any items; trying next candidate",
                candidate, page_url,
            )
        except asyncio.TimeoutError:
            # Log and try the next candidate. A single slow candidate
            # shouldn't take down the whole loop.
            logger.info(
                "feed_autodiscovery: candidate %s for %s hit the %ss budget; "
                "trying next candidate",
                candidate, page_url, _CANDIDATE_PROBE_BUDGET,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "feed_autodiscovery: candidate %s for %s raised %s; "
                "trying next candidate",
                candidate, page_url, exc,
            )
            continue
    return None


async def discover_sitemap_entries(
    page_url: str,
    limit: int = _MAX_SITEMAP_URLS,
    path_prefix: str | None = None,
    *,
    headers: dict | None = None,
    raise_errors: bool = False,
    input_is_sitemap: bool = False,
    owner_pattern: bool = False,
    failures: list[BaseException] | None = None,
    empty: list[str] | None = None,
) -> list[Candidate]:
    """Return up to ``limit`` article candidates from ``page_url``'s
    sitemap, newest first, as ``Candidate(url, published, title,
    modified, place)``.

    They are ordered by their best date: a news sitemap's publication
    date, else ``<lastmod>``, else a ``/YYYY/MM/`` style date in the URL
    itself; undated URLs follow in sitemap order. ``published`` is the
    publication date alone, ``modified`` the ``<lastmod>`` alone, and
    ``title`` a news sitemap's title, each None when absent. ``place``
    is the entry's place in the sitemaps' own order.

    ``input_is_sitemap``: ``page_url`` is the owner's ``sitemap_url``,
    read as the sitemap whatever its address looks like (a WordPress
    ``/?sitemap=index``). Otherwise only an address that looks like a
    sitemap is read directly; any other is a site whose robots.txt and
    usual sitemap paths are tried. ``owner_pattern``: ``path_prefix`` is
    the owner's ``link_pattern`` (``url_ok``'s ``owner_pattern``).

    ``failures``: when given, each document of the crawl that failed is
    added to it (refused, a 5xx, a timeout or another network error;
    not a 404 or another 4xx, which says the document is gone, nor a
    refused robots.txt, which is read as none), whether or not the crawl
    found pages. A crawl that found pages while one of its documents
    failed may have missed what that document says about them (a news
    sitemap's publication dates), so the plugin reads them once more
    when every document loads (``Plan.degraded``).

    ``empty``: when given, the input sitemap's address is added to it
    when it loaded as an XML sitemap (a urlset or a sitemap index) that
    names no page or sitemap at all: a news sitemap between stories,
    which lists only the last two days. The plugin reads that as a
    quiet poll, not as a feed with no list (``plan_candidates``).

    Only entries that could be articles are kept
    (``article_detect.url_ok``), and only the sitemaps worth reading
    are fetched, best first (``article_detect.order_sitemaps``: news,
    then posts; never tag, author, product or attachment sitemaps, and a
    page sitemap only when there is nothing else). ``path_prefix`` (e.g.
    ``/news/``) keeps only URLs under that path, so a section of a large
    site doesn't get buried by the rest of its sitemap. ``headers`` are
    sent with every request (a feed's ``custom_headers``).

    Used by the ``type="generic_scrape"`` plugin (see
    ``app.sources.generic_scrape.plan_candidates``) and, through
    ``discover_sitemap_urls``, by ``/auto``.

    Subject to the same ``_TRAFILATURA_BUDGET`` as the feed discovery
    path. By default nothing raises: a hung sitemap host, a refusal or
    any other failure returns ``[]``, so the Add Custom endpoint can't
    hang or fail on this hop. With ``raise_errors`` (only a website
    feed's own fetch sets it), an empty crawl raises the failure that
    explains it (see ``_crawl_sitemaps``), and running out of budget
    raises ``asyncio.TimeoutError``, which the scheduler records as
    ``[network]``.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        if raise_errors:
            # A host that doesn't resolve is a host that doesn't answer
            # (``[network]``), not a site with no list.
            unresolved = unresolved_host_error(page_url)
            if unresolved is not None:
                raise unresolved
        return []
    try:
        entries = await asyncio.wait_for(
            _crawl_sitemaps(
                page_url,
                limit,
                path_prefix,
                headers=headers,
                raise_errors=raise_errors,
                input_is_sitemap=input_is_sitemap,
                owner_pattern=owner_pattern,
                failures=failures,
                empty=empty,
            ),
            _TRAFILATURA_BUDGET,
        )
    except asyncio.TimeoutError:
        logger.info(
            "feed_autodiscovery: sitemap crawl for %s hit the %ss budget",
            page_url, _TRAFILATURA_BUDGET,
        )
        if raise_errors:
            raise
        return []
    except Exception as exc:  # noqa: BLE001
        if raise_errors:
            raise
        logger.info("feed_autodiscovery: sitemap crawl failed for %s: %s", page_url, exc)
        return []
    return entries[:limit]


async def discover_sitemap_urls(
    page_url: str,
    limit: int = _MAX_SITEMAP_URLS,
    path_prefix: str | None = None,
    *,
    headers: dict | None = None,
) -> list[str]:
    """``discover_sitemap_entries`` as plain URLs. Never raises: an
    empty list means no usable sitemap was found. ``/auto`` uses it to
    decide whether a site with no feed can be added as a
    ``type="generic_scrape"`` source."""
    entries = await discover_sitemap_entries(page_url, limit, path_prefix, headers=headers)
    return [entry.url for entry in entries]


# Sitemap crawl limits: how many sitemap documents one discovery may
# fetch (robots.txt-listed + guessed + nested index entries).
_MAX_SITEMAP_DOCS = 10
# How many matching URLs to collect before sorting by date. Sitemaps
# are often in no useful order, so the newest pages can only be picked
# out after seeing (a bounded slice of) all of them.
_MAX_SITEMAP_COLLECT = 5000
_GUESSED_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")


def looks_like_sitemap(url: str) -> bool:
    """Whether ``url`` names a sitemap document rather than a page."""
    path = urlparse(url).path.lower()
    return path.endswith((".xml", ".xml.gz", ".txt")) or "sitemap" in path


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# One sitemap page entry: ``(url, published, lastmod, title)``.
_Entry = tuple[str, str | None, str | None, str | None]

# A web page. It is never a plain-text list of URLs, even when some of
# its lines start with one (a formatted ``srcset``, an embed's address).
_WEB_PAGE_RE = re.compile(r"<(?:!doctype\s+html|html|head|body)\b", re.IGNORECASE)


def _parse_sitemap_entries(
    text: str,
) -> tuple[list[_Entry], list[tuple[str, str | None]]]:
    """Read a sitemap document: an XML urlset, an XML sitemapindex, or
    a plain-text list. Returns ``([(page, published, lastmod, title)],
    [(nested sitemap, lastmod)])``.

    A page's ``published`` is a news sitemap's
    ``<news:publication_date>``, ``lastmod`` its ``<lastmod>`` and
    ``title`` its ``<news:title>``, each None when absent. The two dates
    stay apart: a publication date dates the page, a ``<lastmod>`` only
    says when it last changed.

    A web page (an HTML document) names nothing: its lines are not a
    plain-text list, whatever they start with."""
    try:
        root = DefusedET.fromstring(text.lstrip())
    except (DefusedET.ParseError, ValueError, DefusedXmlException):
        if _WEB_PAGE_RE.search(text):
            return [], []
        lines = [ln.strip() for ln in text.splitlines()]
        return [
            (ln, None, None, None) for ln in lines if ln.startswith(("http://", "https://"))
        ], []
    pages: list[_Entry] = []
    nested: list[tuple[str, str | None]] = []
    is_index = _local(root.tag) == "sitemapindex"
    for entry in root:
        loc = lastmod = published = title = None
        for child in entry:
            name = _local(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
            elif name == "news":
                for item in child:
                    item_name = _local(item.tag)
                    if item_name == "publication_date" and item.text:
                        published = item.text.strip()
                    elif item_name == "title" and item.text:
                        title = item.text.strip()
        if not loc:
            continue
        if is_index:
            nested.append((loc, lastmod))
        else:
            pages.append((loc, published, lastmod, title))
    return pages, nested


def _is_sitemap_xml(text: str) -> bool:
    """Whether ``text`` is an XML sitemap, a urlset or a sitemap index,
    whatever it lists (an empty urlset is one)."""
    try:
        root = DefusedET.fromstring(text.lstrip())
    except (DefusedET.ParseError, ValueError, DefusedXmlException):
        return False
    return _local(root.tag) in ("urlset", "sitemapindex")


def _read_sitemap(
    text: str, site: str, path_prefix: str | None, room: int, owner_pattern: bool = False,
) -> tuple[list[_Entry], list[tuple[str, str | None]], bool]:
    """Parse one sitemap document and keep the pages that could be
    articles on ``site``: under ``path_prefix`` when there is one, and
    passing ``url_ok`` below it (``section``; as the owner's pattern
    when ``owner_pattern``; as a sitemap's entry, ``sitemap_entry``).
    At most ``room`` pages. Returns ``(pages, nested sitemaps, listed)``,
    ``listed`` False when the document named no page or sitemap at all,
    before any filter (an HTML page, an empty urlset). Pure and
    CPU-bound (an XML parse, then a URL test per entry), so
    ``_crawl_sitemaps`` runs it in a thread."""
    found, nested = _parse_sitemap_entries(text)
    pages: list[_Entry] = []
    for entry in found:
        if len(pages) >= room:
            break
        if path_prefix and not _under_prefix(entry[0], path_prefix):
            continue
        if url_ok(
            entry[0], site, owner_pattern=owner_pattern, section=path_prefix, sitemap_entry=True
        ):
            pages.append(entry)
    return pages, nested, bool(found or nested)


_MONTHS = {
    name: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}
# /2026/, /2026/07/, /2026/july/, /2026/07/27/
_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})/(?:([a-z]+|\d{1,2})/(?:(\d{1,2})/)?)?", re.IGNORECASE)


def _entry_date(url: str, lastmod: str | None) -> dt.datetime | None:
    """Best guess at when a sitemap entry was published or changed:
    its ``<lastmod>``, else a date in its URL path, else None."""
    if lastmod:
        try:
            parsed = dt.datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    match = _URL_DATE_RE.search(urlparse(url).path)
    if not match:
        return None
    year = int(match.group(1))
    month_raw = (match.group(2) or "").lower()
    month = int(month_raw) if month_raw.isdigit() else _MONTHS.get(month_raw, 1)
    day = int(match.group(3)) if match.group(3) else 1
    try:
        return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
    except ValueError:
        return dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)


def _newest_first(entries: list[tuple[str, str | None]]) -> list[str]:
    """URLs ordered newest first; undated ones keep their order, last.
    Takes ``(url, lastmod)`` pairs: sitemap pages are projected to them
    before sorting."""
    dated = [(url, _entry_date(url, lastmod)) for url, lastmod in entries]
    ordered = sorted(
        dated,
        key=lambda pair: (pair[1] is None, -(pair[1].timestamp()) if pair[1] else 0.0),
    )
    return [url for url, _ in ordered]


def _under_prefix(url: str, prefix: str) -> bool:
    """True for pages below ``prefix`` (not the section page itself)."""
    path = urlparse(url).path
    return path.startswith(prefix) and path.rstrip("/") != prefix.rstrip("/")


async def _crawl_sitemaps(
    page_url: str,
    limit: int,
    path_prefix: str | None = None,
    *,
    headers: dict | None = None,
    raise_errors: bool = False,
    input_is_sitemap: bool = False,
    owner_pattern: bool = False,
    failures: list[BaseException] | None = None,
    empty: list[str] | None = None,
) -> list[Candidate]:
    """Find article candidates for ``page_url`` from its sitemap(s),
    using only SSRF-guarded fetches. ``page_url`` may be a sitemap
    itself (``input_is_sitemap``, or an address that looks like one) or
    a site URL; for a site URL, robots.txt ``Sitemap:`` lines are tried
    first, then a few common paths. An owner's ``sitemap_url`` that
    doesn't look like a sitemap and names nothing (the site's own
    address, an HTML site-map page) is read as a site URL after it.
    Returns the newest ``limit`` (see ``discover_sitemap_entries``).

    A document that fails is skipped and the crawl moves on: robots.txt
    and the guessed paths 404 on many sites, and a nested sitemap can be
    gone. With ``raise_errors``, a crawl that found nothing raises the
    failure that explains it, in this order:

    - the input's own failure, when ``page_url`` is a sitemap: it is
      the user's address (their ``sitemap_url``), so a 404 there is
      ``[not found]``;
    - a refusal, when every sitemap that could list pages was refused.
      A sitemap index that loaded doesn't count either way: it only
      names the sitemaps to read, so an index whose post sitemaps are
      all refused is refused. A refused robots.txt alone doesn't count,
      because the guessed paths are still tried;
    - a network error, when a document couldn't be fetched at all.

    Otherwise it returns ``[]``: the site has no sitemap with articles.

    ``failures`` collects every document that failed, robots.txt
    included unless it was refused (see ``discover_sitemap_entries``). A
    5xx on a document other than the input reads as empty, as before; it
    is only collected. ``empty`` gets the input's address when it is an
    empty sitemap (see ``discover_sitemap_entries``).
    """
    parsed = urlparse(page_url)
    host = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{parsed.netloc}"
    input_doc: str | None = None
    own_failure: BaseException | None = None
    refused: list[FetchRefusedError] = []
    network: list[BaseException] = []
    # Every document that failed (``failures``).
    failed: list[BaseException] = []
    # The input loaded as an XML sitemap that names nothing (``empty``).
    input_empty = False

    async def site_sitemaps() -> list[str]:
        """The site's sitemaps: robots.txt ``Sitemap:`` lines, else the
        usual paths."""
        robots = ""
        try:
            robots = await fetch_html_checked(
                f"{origin}/robots.txt",
                custom_headers=headers,
                content_types=None,
                raise_for_status=True,
            ) or ""
        except FetchRefusedError:
            # Read as no robots.txt: the guessed paths below still run,
            # and a refusal counts only if they are refused too. Nor is
            # it a failed document (``failures``): a refusal of robots.txt
            # tends to last, and would leave every poll's read degraded.
            pass
        except httpx.HTTPStatusError as exc:
            # A 404 is routine: no robots.txt.
            if exc.response.status_code >= 500:
                failed.append(exc)
        except NETWORK_ERRORS as exc:
            network.append(exc)
            failed.append(exc)
        listed = [
            line.split(":", 1)[1].strip()
            for line in robots.splitlines()
            if line.lower().startswith("sitemap:")
        ]
        return order_sitemaps(listed) if listed else [origin + path for path in _GUESSED_SITEMAP_PATHS]

    if input_is_sitemap or looks_like_sitemap(page_url):
        input_doc = page_url
        queue = [page_url]
    else:
        queue = await site_sitemaps()

    seen: set[str] = set()
    pages: list[_Entry] = []
    page_set: set[str] = set()
    fetched = 0
    # Documents that loaded but list no pages of their own (sitemap
    # indexes, and an owner's sitemap_url that turned out to be a page):
    # the refusal rule above leaves them out of ``fetched``.
    indexes = 0
    while queue and fetched < _MAX_SITEMAP_DOCS and len(pages) < _MAX_SITEMAP_COLLECT:
        url = queue.pop(0)
        if url in seen or not same_site(url, host):
            continue
        seen.add(url)
        fetched += 1
        try:
            text = await fetch_html_checked(
                url,
                custom_headers=headers,
                content_types=None,
                raise_for_status=True,
            )
        except FetchRefusedError as exc:
            refused.append(exc)
            failed.append(exc)
            if url == input_doc:
                own_failure = exc
            continue
        except httpx.HTTPStatusError as exc:
            # A status fails the crawl only on the input sitemap: it is
            # the user's own address. Any other document that 404s reads
            # as empty, and the crawl moves on; a 5xx too, but it failed.
            if url == input_doc:
                own_failure = exc
            if exc.response.status_code >= 500:
                failed.append(exc)
            continue
        except NETWORK_ERRORS as exc:
            network.append(exc)
            failed.append(exc)
            if url == input_doc:
                own_failure = exc
            continue
        found, nested, listed = [], [], False
        if text:
            found, nested, listed = await asyncio.to_thread(
                _read_sitemap,
                text,
                page_url,
                path_prefix,
                _MAX_SITEMAP_COLLECT - len(pages),
                owner_pattern,
            )
        if url == input_doc and not listed and text:
            input_empty = await asyncio.to_thread(_is_sitemap_xml, text)
        if url == input_doc and not listed and not looks_like_sitemap(url):
            # The owner's sitemap_url names nothing and doesn't look like
            # a sitemap: it is likely the site's address or an HTML
            # site-map page, so read the site's sitemaps, as for a site.
            indexes += 1
            queue.extend(await site_sitemaps())
            continue
        if nested:
            indexes += 1
        for entry in found:
            if entry[0] not in page_set:
                page_set.add(entry[0])
                pages.append(entry)
        # The nested sitemaps worth reading, best class first, and
        # within a class newest first, so the document cap spends its
        # fetches on recent articles (e.g. post-sitemap12 before 1, and
        # never the page or tag sitemaps beside them).
        queue.extend(order_sitemaps(_newest_first(nested)))

    if failures is not None:
        failures.extend(failed)
    if empty is not None and input_empty and not pages:
        empty.append(page_url)
    if not pages and raise_errors:
        if own_failure is not None:
            raise own_failure
        if refused and len(refused) == fetched - indexes:
            raise most_telling_error(refused)
        if network:
            raise network[0]
    # ``pages`` is in the site's own order (``Candidate.place``).
    by_url = {
        url: (published, lastmod, title, place)
        for place, (url, published, lastmod, title) in enumerate(pages)
    }
    ordered = _newest_first([(url, published or lastmod) for url, published, lastmod, _ in pages])
    out = []
    for url in ordered[:limit]:
        published, lastmod, title, place = by_url[url]
        out.append(Candidate(url, parse_date(published), title, parse_date(lastmod), place))
    return out
