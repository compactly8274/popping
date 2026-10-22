"""Generic scrape plugin — periodic ingest for sites with no native
RSS/Atom feed.

Auto-feed (see ``app.feed_autodiscovery``) tries an actual feed
first; this is the fallback the "Add custom" flow wires up when none
exists. Rather than a bespoke "guess which links on this page are
articles" heuristic, it reads the site's own lists of articles: the
listing page the feed points at and the site's sitemap (see
``plan_candidates``). Each new page is extracted the same way the
on-demand article-summary feature does (``trafilatura.bare_extraction``,
same library, same SSRF-guarded fetch as ``app.article_extract``) and
kept only when ``app.article_detect.score_page`` reads it as an
article, so About, Contact, tag and shop pages stay out.

A failure says what it is (``scheduler._classify_error``): a site that
refuses us is ``[blocked]``, the feed's own address gone is
``[not found]``, a host that doesn't answer is ``[network]``, and a
site with no list of articles at all is ``[no list]``
(``NoArticleListError``, a setup state).

Not registered via ``@register_source`` — row-driven, same pattern as
``DynamicRssPlugin`` / ``DynamicYouTubePlugin``. The scheduler
instantiates one of these per ``type="generic_scrape"`` Source row
and keeps that instance alive for the process lifetime (see
``app.scheduler._ingest``'s docstring), which is what lets
``_extracted_urls`` below persist across scheduled polls without a
dedicated DB table.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import OrderedDict
from typing import Callable, Container, NamedTuple, Optional
from urllib.parse import urlparse

import httpx

from app.article_detect import (
    ACCEPT_SCORE,
    Candidate,
    listing_links,
    same_site,
    score_page,
    url_ok,
)
from app.article_extract import (
    NETWORK_ERRORS,
    FetchRefusedError,
    fetch_html_checked,
    most_telling_error,
)
from app.feed_autodiscovery import discover_sitemap_entries, looks_like_sitemap
from app.ingest_rules import IngestTunables
from app.models import Source
from app.sources.base import KnownUrls, NoArticleListError, SourcePlugin, row_fetch_target

logger = logging.getLogger("popping.sources.generic_scrape")

# How many NOT-YET-SEEN candidate URLs to actually fetch+extract per
# poll. Bounds both the load this puts on the target site and the
# backend's own per-poll work — a freshly added source pointed at a
# sitemap with thousands of URLs drains its backlog gradually over
# many poll cycles rather than fetching everything in one burst.
_MAX_NEW_PER_POLL = 10

# How many of this poll's (up to _MAX_NEW_PER_POLL) candidate fetches
# run concurrently, rather than strictly one-at-a-time. Found in a
# repo-wide audit: a source whose pages take 5-10s each could turn
# one scheduled poll into a 1-2 minute serialized job, holding that
# scheduler slot the whole time for no correctness reason. Kept
# modest (unlike scheduler.py's thumbnail-fetch concurrency of 10,
# which fans out across many DIFFERENT hosts) because every fetch
# here hits the SAME host — this is the "courtesy" source type
# (see _GENERIC_SCRAPE_DEFAULT_REFRESH's rationale in
# routes/sources.py), so a handful of concurrent connections instead
# of a full burst of 10.
_FETCH_CONCURRENCY = 3

# Upper bound on probe() (the Add/Test preview), in seconds.
_PROBE_BUDGET = 45.0

# How many candidate URLs to even ask the sitemap for. Independent of
# _MAX_NEW_PER_POLL — we want to see enough of the sitemap to find
# _MAX_NEW_PER_POLL genuinely new ones even once the easy/recent
# entries near the top have already been extracted in prior polls.
_MAX_SITEMAP_CANDIDATES = 200

# Extracted body text is stored in meta.summary (same field the
# regular RSS path uses for the feed's own blurb — see
# routes/entries.py's _extract_fallback_summary), truncated the same
# way that endpoint would truncate a long one anyway. Keeps a huge
# extracted article from bloating the entries table.
_SUMMARY_MAX_CHARS = 2000

# Slice 27: cap on the per-plugin ``_extracted_urls`` set. The DB
# UNIQUE constraint on entries.url is the real correctness backstop
# (re-attempts are silently no-op'd by ``on_conflict_do_nothing``);
# this cap bounds the in-memory footprint only. 5000 is enough to
# cover a news site's full sitemap over a multi-day window without
# forcing the FIFO eviction to re-attempt URLs the DB already has.
_MAX_EXTRACTED_URLS = 5000

# A page whose fetch fails in a way that passes (a timeout, a network
# error, a 5xx) is asked for again on later polls, and passed over
# (marked extracted) after this many failed polls in a row, so a page
# that never answers can't take a slot of every poll. The counts are
# per plugin instance, bounded like ``_extracted_urls``.
_MAX_PAGE_FAILURES = 3
_MAX_PAGE_FAILURE_URLS = 1000

# When every page a poll asked for was refused, how many other pages of
# the site ``GenericScrapePlugin._settle_refusals`` may fetch to see
# whether the site serves us at all (a gone control page is skipped).
_MAX_CONTROLS = 3

# Pages dated more than this many days ago are skipped. Sitemaps list a
# site's whole back catalogue, so without this a new source fills its
# column with years-old pages before it ever reaches this week's.
# Undated pages are judged by the undated cap instead (see
# ``GenericScrapePlugin._cap_undated``).
_MAX_AGE_DAYS = 30

# The undated cap when ``fetch`` is called without the scheduler's
# ``max_undated`` (a direct call): ``ingest.max_undated_per_poll``'s
# default.
_DEFAULT_MAX_UNDATED = IngestTunables().max_undated_per_poll


def _section_prefix(url: str) -> str | None:
    """The path section a source URL points at, e.g. ``/news/`` for
    ``https://example.com/news``; None for a homepage or a file-like
    page (``/feed.xml``, ``/news.html``), where there's no section to
    scope to."""
    path = urlparse(url).path
    if path in ("", "/"):
        return None
    if "." in path.rstrip("/").rsplit("/", 1)[-1]:
        return None
    return path if path.endswith("/") else path + "/"


def _is_site_root(url: str) -> bool:
    """The site's home page: no path beyond ``/`` (a query is allowed:
    ``/?lang=en`` is still the home page)."""
    return urlparse(url).path in ("", "/")


class Plan(NamedTuple):
    """What a website feed can read this poll (``plan_candidates``)."""

    # Which read found the candidates: "page" (the listing page) or
    # "sitemap"; "" when neither found any.
    mode: str
    # Raw discovery: every candidate that passed ``url_ok`` and the link
    # pattern, newest first, before the stored-URL skip and the caps.
    candidates: list[Candidate]
    # The candidates not already stored (``known_urls``), in the same
    # order. All of them when no lookup was given.
    fresh: list[Candidate]
    # The candidates came from the second read, because the first (the
    # usual read for the feed's kind of address) failed or found none.
    fallback: bool = False
    # The sitemap read gave the candidates while one of its documents
    # failed (a news sitemap's 503 beside a post sitemap that loads), so
    # it may lack what that document says about them.
    degraded: bool = False


async def plan_candidates(
    url: str,
    sitemap_url: str | None = None,
    link_pattern: str | None = None,
    headers: dict | None = None,
    known_urls: KnownUrls | None = None,
    *,
    limit: int = _MAX_SITEMAP_CANDIDATES,
    raise_errors: bool = False,
) -> Plan:
    """Find the pages a website feed could pull, for both
    ``GenericScrapePlugin.fetch`` and ``probe``.

    Two reads, the listing page (``url``) and the sitemap, and the
    first that finds articles wins:

    - A section URL (``/news``) reads its page first: a listing shows
      its newest posts, and the site's sitemap mixes every section.
    - A site root, a feed with an explicit ``sitemap_url``, or a URL
      that looks like a sitemap reads the sitemap first. The page comes
      second: an HTML sitemap page (``/sitemap/``) is a listing, and an
      XML one reads as no page at the cost of one fetch.

    ``Plan.fallback`` says the second read gave the candidates, and
    ``Plan.degraded`` that the sitemap read gave them while one of its
    documents failed (see ``GenericScrapePlugin.fetch``).

    The sitemap read takes the owner's ``sitemap_url`` as the sitemap
    whatever its address looks like, so a 404 there is the feed's own
    address missing (``[not found]``).

    The page read keeps the links under ``link_pattern`` when the owner
    set one (today's prefix filter), and the page's repeating list of
    posts (``article_detect.listing_links``) otherwise. The sitemap read
    keeps entries under the link pattern, else under the URL's section.
    There is no fallback from an empty section to the whole site: that
    is how a section feed used to fill with the rest of the site. Every
    candidate also passes ``article_detect.url_ok``; under the owner's
    link pattern, as the owner's choice (``owner_pattern``).

    ``known_urls`` marks the candidates already stored (``fresh`` leaves
    them out); the scheduler passes it, so stored pages are never
    fetched again, even after a restart empties ``_extracted_urls``.

    With ``raise_errors`` (the plugin's scheduled fetch), a plan that
    found nothing raises: the most telling failure of the two reads
    (``article_extract.most_telling_error``: a refusal, then the feed's
    own page or sitemap failing, then a network error), else
    ``NoArticleListError`` (``[no list]``). When one read finds
    articles, the other's failure doesn't matter. The one quiet empty
    plan: the feed's own sitemap (its ``sitemap_url``, or an address
    that looks like a sitemap) loaded as an empty sitemap and the page
    read failed in no way. A news sitemap lists only the last two days,
    so between stories it is an empty urlset, which main read as a poll
    with nothing new. Without ``raise_errors`` (``probe``, the Test
    preview), nothing raises and an empty plan comes back.
    """
    prefix = link_pattern or _section_prefix(url)
    # The sitemap documents that failed (``discover_sitemap_entries``).
    sitemap_failures: list[BaseException] = []
    # The feed's own sitemap, when it loaded and named nothing.
    empty_sitemap: list[str] = []

    async def read_sitemap() -> list[Candidate]:
        return await discover_sitemap_entries(
            sitemap_url or url,
            limit=limit,
            path_prefix=prefix,
            headers=headers,
            raise_errors=raise_errors,
            input_is_sitemap=bool(sitemap_url),
            owner_pattern=bool(link_pattern),
            failures=sitemap_failures,
            empty=empty_sitemap,
        )

    async def read_page() -> list[Candidate]:
        return await _discover_page_links(
            url, link_pattern, limit=limit, headers=headers, raise_errors=raise_errors
        )

    if sitemap_url or _is_site_root(url) or looks_like_sitemap(url):
        reads = [("sitemap", read_sitemap), ("page", read_page)]
    else:
        reads = [("page", read_page), ("sitemap", read_sitemap)]

    failures: list[BaseException] = []
    mode, found, fallback = "", [], False
    for index, (name, read) in enumerate(reads):
        try:
            found = await read()
        except Exception as exc:  # noqa: BLE001 - kept: the other read may still find the list
            logger.info("generic_scrape: %s: the %s read failed: %s", url, name, exc)
            failures.append(exc)
            continue
        if found:
            mode, fallback = name, index > 0
            break
    if not found:
        if raise_errors:
            exc = most_telling_error(failures)
            if exc is not None:
                raise exc
            if not empty_sitemap:
                raise NoArticleListError("no sitemap or listing page with articles")
            logger.debug(
                "generic_scrape: %s: its sitemap %s lists nothing yet", url, empty_sitemap[0]
            )
        return Plan("", [], [])
    stored = await known_urls([c.url for c in found]) if known_urls is not None else set()
    degraded = mode == "sitemap" and bool(sitemap_failures)
    return Plan(mode, found, [c for c in found if c.url not in stored], fallback, degraded)


def _too_old(item: dict) -> bool:
    published = item.get("published_at")
    if published is None:
        return False
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=_MAX_AGE_DAYS)
    return published < cutoff


def _read_page(
    html: str,
    url: str,
    final_url: str,
    owner_configured: bool,
    listed_date: dt.datetime | None = None,
) -> tuple[dict, str, str] | None:
    """Extract one fetched page and score it as an article: ``(data,
    title, text)`` from ``bare_extraction``, or None when there is no
    usable title or text or ``score_page`` says it isn't an article.
    ``listed_date`` is the date the site's list gave the page
    (``Candidate.published``). CPU-bound and synchronous;
    ``_extract_one`` runs it in a thread."""
    import trafilatura

    doc = trafilatura.bare_extraction(
        html,
        url=url,
        with_metadata=True,
        favor_recall=True,
    )
    if doc is None:
        return None
    data = doc.as_dict()
    title = (data.get("title") or "").strip()
    text = (data.get("text") or "").strip()
    if not title or not text:
        logger.debug("generic_scrape: %s: no usable title/text extracted", url)
        return None
    score, reasons = score_page(
        html,
        url,
        final_url=final_url,
        text=text,
        owner_configured=owner_configured,
        listed_date=listed_date,
    )
    if score < ACCEPT_SCORE:
        logger.debug(
            "generic_scrape: %s: not an article (score %d: %s)", url, score, ", ".join(reasons)
        )
        return None
    return data, title, text


async def _extract_one(
    url: str,
    custom_headers: dict | None = None,
    *,
    owner_configured: bool = False,
    listed_date: dt.datetime | None = None,
    served: set[str] | None = None,
) -> dict | None:
    """Fetch + extract a single candidate URL into a plugin-item
    dict, or None if the fetch or extraction didn't produce anything
    usable, or the page isn't an article (``score_page``). Module-level
    (not a method) so both ``GenericScrapePlugin.fetch`` and the "Test"
    endpoint's ``probe`` below share the exact same extraction logic
    without one needing a full Source row to exercise the other's code
    path.

    ``custom_headers`` (B4 fix): forwarded to ``fetch_html_checked`` so
    the plugin can pass user-supplied ``Source.custom_headers`` (e.g.
    a custom User-Agent for sites that block default UAs, or an
    Authorization header for paywalled content) through to the
    actual HTTP request. ``None`` uses only the default headers.

    ``owner_configured``: the candidates came from the owner's
    ``link_pattern`` or ``sitemap_url``, so ``score_page`` takes the
    owner's choice as the article signal (an ollama-style
    ``/library/<model>`` page declares no article type and no date).

    ``listed_date``: when the site's list says the page was published
    (``Candidate.published``). ``score_page`` counts it as the page's
    date, and it dates the item when the page itself carries none.

    ``served``: when given, ``url`` is added to it once the site answers
    with a page (HTML), whether or not it turns out to be an article, so
    the plugin knows which pages can show that the site serves us (see
    ``GenericScrapePlugin._settle_refusals``).

    Two kinds of failure raise, so the plugin can leave the page for a
    later poll instead of passing over it:

    - ``FetchRefusedError`` when the site refuses the page (401, 402,
      403, 429); the plugin reports the feed blocked when the site
      refuses everything.
    - A failure that passes: a network error or timeout
      (``NETWORK_ERRORS``) or a 5xx answer (``httpx.HTTPStatusError``).

    A page that is gone (404, 410 or another 4xx), isn't HTML, or isn't
    an article gives None: it was read, and asking again won't change it.
    """
    try:
        fetched = await fetch_html_checked(
            url,
            custom_headers=custom_headers,
            return_final_url=True,
            raise_for_status=True,
        )
    except FetchRefusedError:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code >= 500:
            raise
        logger.debug("generic_scrape: %s: gone: %s", url, exc)
        return None
    except NETWORK_ERRORS:
        raise
    except httpx.HTTPError as exc:
        logger.debug("generic_scrape: %s: fetch failed: %s", url, exc)
        return None
    if fetched is None:
        return None
    html, final_url = fetched
    if served is not None:
        served.add(url)
    try:
        # Parsing and scoring a page is CPU-bound and synchronous, and
        # a poll extracts up to ``_MAX_NEW_PER_POLL`` pages. Run on the
        # event loop, it stalled every API request and scheduler job
        # for the length of each parse. Run it in asyncio's default
        # thread pool instead, the same way ``feed_autodiscovery`` runs
        # ``determine_feed``.
        page = await asyncio.to_thread(
            _read_page, html, url, final_url, owner_configured, listed_date
        )
    except Exception:  # noqa: BLE001 - a single page's extraction failing shouldn't sink the whole poll
        logger.debug("generic_scrape: %s: extraction raised", url)
        return None
    if page is None:
        return None
    data, title, text = page
    summary = text[:_SUMMARY_MAX_CHARS]
    return {
        "title": title,
        "url": url,
        # Trafilatura returns ``data.get("date")`` as a string
        # in one of several shapes — ISO-8601 with offset
        # (``"2026-07-27T08:59:10-07:00"``), bare ISO date
        # (``"2026-07-27"``), or with a trailing ``Z`` (the
        # Z-suffix form). The downstream ``recency.score`` calls
        # ``.tzinfo`` on the value, so a raw string crashes the
        # whole ingest. Normalize via ``_parse_published_str``
        # which handles all three shapes and returns ``None`` on
        # anything it can't parse (a missing or unparseable date
        # yields zero recency contribution — the entry still
        # lands in the DB, just without the convergence boost).
        # A page that carries no date of its own takes the one its
        # site's list gave it, the date ``score_page`` counted.
        "published_at": _parse_published_str(data.get("date")) or listed_date,
        "summary": summary,
        "image_url": data.get("image"),
    }


def _parse_published_str(raw: object) -> Optional[dt.datetime]:
    """Normalize a trafilatura ``date`` value to a UTC datetime.

    Trafilatura's ``bare_extraction`` returns ``data["date"]`` in
    several shapes — depends on what metadata the page exposed:

    - ``"2026-07-27T08:59:10-07:00"`` (ISO-8601 with offset)
    - ``"2026-07-27"`` (date only — no time component)
    - ``"2026-07-27T08:59:10Z"`` (Z-suffix; some HTML metadata)
    - ``"Mon, 27 Jul 2026 15:59:10 +0000"`` (RFC-822, rare)
    - ``None`` (page didn't expose any date metadata)

    Returns a timezone-aware ``datetime`` in UTC for the parseable
    cases, or ``None`` for ``None`` / unparseable strings. Plain
    dates get their time component set to midnight UTC (the
    earliest moment of that day) so the recency score is at least
    representative — a publication-day accuracy is fine for a
    "freshness" signal, a "publication second" wouldn't add value
    the RSS path doesn't already have.

    Mirrors the spirit of ``app.sources.rss._parse_published``
    (which handles ``feedparser`` entries) but operates on a raw
    string instead, since trafilatura doesn't expose a struct_time
    the way feedparser does.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Shape 1: ISO-8601 with offset, possibly Z-suffix.
    # Python 3.11+ ``fromisoformat`` accepts the Z suffix; on
    # 3.10 and earlier we'd have to substitute. We're on
    # 3.12 (matches the deploy image), so this is safe; if
    # 3.10 ever comes back, replace ``Z`` with ``+00:00``
    # first as a belt-and-braces measure.
    try:
        # Belt-and-braces: handle Z on every Python version
        # by substituting before fromisoformat. fromisoformat
        # rejects the literal 'Z' on 3.10.
        normalized = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = dt.datetime.fromisoformat(normalized)
        # Naive datetime (date-only or no offset) → assume UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        pass
    # Shape 2: RFC-822 (rare, e.g. ``"Mon, 27 Jul 2026 15:59:10 +0000"``)
    # ``email.utils.parsedate_to_datetime`` is the canonical
    # stdlib helper for this; it raises ``TypeError`` on bad
    # input rather than returning a sentinel.
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(s)
        if parsed is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


async def probe(url: str, limit: int = 3) -> list[dict]:
    """Standalone, row-free version of ``GenericScrapePlugin.fetch``
    for ``POST /api/sources/test`` — Add's "does this actually work"
    preview needs to run before a Source row exists to construct a
    real plugin instance from. Same plan (``plan_candidates``), but it
    never raises: a site it can't read gives ``[]``. Tries a few more
    candidates than ``limit`` in case the first couple aren't articles,
    so the preview isn't overly pessimistic about a genuinely workable
    site."""
    items: list[dict] = []

    async def _run() -> None:
        plan = await plan_candidates(url, limit=max(limit * 3, 10))
        candidates = plan.candidates
        # A few pages at a time: one slow page shouldn't hold up the rest.
        for start in range(0, len(candidates), _FETCH_CONCURRENCY):
            if len(items) >= limit:
                return
            chunk = candidates[start:start + _FETCH_CONCURRENCY]
            results = await asyncio.gather(
                *(_extract_one(c.url, listed_date=c.published) for c in chunk),
                return_exceptions=True,
            )
            items.extend(r for r in results if isinstance(r, dict))

    # Add waits on this, so a slow site returns what it has (or
    # nothing) after _PROBE_BUDGET instead of spinning for minutes.
    try:
        await asyncio.wait_for(_run(), _PROBE_BUDGET)
    except asyncio.TimeoutError:
        logger.info("generic_scrape: probe of %s hit the %ss budget", url, _PROBE_BUDGET)
    return items[:limit]


def _extract_links_from_html(html: str, page_url: str, pattern: str) -> list[str]:
    """Pull every ``<a href="...">`` from ``html`` whose URL matches
    ``pattern`` (a leading-slash path prefix like ``/library/``),
    resolved to an absolute URL via ``page_url``'s origin.

    Strict same-origin only — links to other hosts are discarded.
    That keeps the SSRF guard (``app.article_extract.fetch_html``)
    effective: every URL we subsequently fetch is from the same
    origin as the source row's ``url``, which is the only host
    the operator has approved.

    Deduplicates while preserving order (first-occurrence) so the
    plugin's ``_extracted_urls`` dedup and the poll cap work as
    expected.

    Trafilatura can return ``html`` already partially cleaned
    (a "main text" view) when called via the higher-level
    ``extract()`` API, but here we work on the raw ``html`` from
    ``fetch_html`` so we can pick up the actual ``href``
    attributes. ``re.findall`` on the raw string is fast enough
    for the page sizes we care about (typically <200 KB) and
    avoids pulling BeautifulSoup as a dependency.
    """
    import re
    from urllib.parse import urljoin, urlparse

    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    seen: set[str] = set()
    out: list[str] = []
    # Anchor with any attributes in between, then ``href="..."``
    # or ``href='...'``. Order matters: capture the URL value,
    # not the surrounding HTML.
    for match in re.finditer(
        r'<a\s[^>]*href=(["\'])([^"\']*)\1[^>]*>',
        html,
        re.IGNORECASE,
    ):
        href = match.group(2).strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href.startswith("mailto:"):
            continue
        # Resolve relative URLs against the page.
        absolute = urljoin(origin, href)
        # Strict same-origin.
        abs_parsed = urlparse(absolute)
        if abs_parsed.scheme not in ("http", "https"):
            continue
        if f"{abs_parsed.scheme}://{abs_parsed.netloc}" != origin:
            continue
        # Match the path prefix.
        if not abs_parsed.path.startswith(pattern):
            continue
        # Skip the page itself and any URL with a different query
        # string than the page (avoids "newest" vs "top" sorting
        # variants being treated as separate candidates).
        if abs_parsed.path == parsed.path:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out


def _page_links(html: str, page_url: str, final_url: str, pattern: str | None) -> list[Candidate]:
    """The article links on a fetched listing page, as candidates. With
    a link pattern, the links under it (``_extract_links_from_html``),
    undated and untitled, passing ``url_ok`` as the owner's choice, which
    judges a link below the pattern by the segments below it; without,
    the page's repeating list of posts (``listing_links``), each with
    its card's title and date, passing ``url_ok``. CPU-bound (an HTML
    parse and a URL test per link), so ``_discover_page_links`` runs it
    in a thread.

    The listing is read with ``url_ok``'s rules on every segment first.
    Only when that finds no list is it read again with the page's own
    section lifted (``url_ok``'s ``section``): stories filed under a
    section that reads as static (``/about/media-centre/<slug>``) need
    it, while a category page's links to its subcategories
    (``/category/news/<child>/``) would pass with it too, and can
    outnumber its posts."""
    if pattern:
        return [
            Candidate(link, None, None)
            for link in _extract_links_from_html(html, page_url, pattern)
            if url_ok(link, page_url, owner_pattern=True, section=pattern)
        ]
    # Resolve against where the page ended up (``/news`` redirecting to
    # ``/news/`` changes what a relative link means), unless it
    # redirected off the site.
    base = final_url if same_site(final_url, page_url) else page_url
    links = listing_links(html, base)
    section = _section_prefix(page_url)
    if not links and section:
        links = listing_links(html, base, section=section)
    return links


async def _discover_page_links(
    page_url: str,
    pattern: str | None,
    limit: int = _MAX_SITEMAP_CANDIDATES,
    *,
    headers: dict | None = None,
    raise_errors: bool = False,
) -> list[Candidate]:
    """Fetch ``page_url`` and return up to ``limit`` of its article
    links (see ``_page_links``).

    ``fetch_html_checked`` enforces the URL safety check (denied hosts,
    private network ranges), so this layer inherits the SSRF
    guard for free — every link returned here is reachable +
    not on a private network.

    The page is the feed's own address, so with ``raise_errors`` its
    failures raise, any HTTP error included (a 404 is ``[not found]``);
    without, it gives ``[]``.
    """
    try:
        fetched = await fetch_html_checked(
            page_url, custom_headers=headers, return_final_url=True, raise_for_status=True
        )
    except httpx.HTTPError as exc:
        if raise_errors:
            raise
        logger.debug("generic_scrape: %s: listing page fetch failed: %s", page_url, exc)
        return []
    if fetched is None:
        return []
    html, final_url = fetched
    links = await asyncio.to_thread(_page_links, html, page_url, final_url, pattern)
    return links[:limit]


class _Read(NamedTuple):
    """What ``GenericScrapePlugin._extracted_urls`` keeps for a page."""

    # The site answered with a page (HTML) when it was read, whether it
    # was kept, too old, not an article or over the undated cap: it can
    # show that the site serves us (``_settle_refusals``). False for a
    # page that was gone, wasn't HTML, kept failing or refused everyone.
    served: bool = False
    # Read in a poll whose usual read gave nothing, on the other read's
    # weaker evidence, or whose sitemap read had a document fail (see
    # ``GenericScrapePlugin.fetch``).
    unvouched: bool = False


def _backlog_rule(
    plan: Plan, backlog_before: dt.datetime | None, read: Container[str] = frozenset(),
) -> Callable[[str], bool]:
    """Whether an undated candidate of ``plan`` is old backlog
    (``GenericScrapePlugin._cap_undated``). It depends on what orders
    the list:

    - A listing page (``plan.mode == "page"``) is in publication order,
      newest first, so a page listed below a stored story is older than
      it: backlog. Except for the stories pinned at its top (a sticky or
      featured post, stored long ago): when stored stories lead the list
      and a story that isn't stored follows them, with a story stored or
      already read (``read``: this plugin's ``_extracted_urls``) further
      down, the leading ones are pinned, and the line is the first such
      story below them. A first fetch that stores only the two sticky
      posts (the undated cap) has read the posts below them. With no such
      story further down, the leading ones are the line, as for any
      listing (after a restart, ``read`` is empty).
    - A sitemap is ordered by ``<lastmod>``, which moves a stored story
      up whenever it is edited, so that order says nothing about age. A
      page last modified before ``backlog_before`` (the line
      ``ingest_rules`` draws for dated stories on a later poll) was
      published before it: backlog. A first fetch (``backlog_before``
      None) keeps the newest pages whatever their age.
    - A sitemap page with no ``<lastmod>`` keeps the sitemap's own order,
      the only one there is, so the listing page's rule applies: listed
      below a stored story is backlog. That is its place in the sitemap
      documents (``Candidate.place``), not in the candidates, whose date
      order puts every undated entry after every dated one.
    """
    fresh = {c.url for c in plan.fresh}
    modified = {c.url: c.modified for c in plan.candidates}
    # Where the site listed each candidate: a sitemap entry's place, else
    # its place in the list (a listing page's cards are in page order).
    place = {c.url: i if c.place is None else c.place for i, c in enumerate(plan.candidates)}
    stored = sorted(place[c.url] for c in plan.candidates if c.url not in fresh)
    first_stored = stored[0] if stored else None
    first_fresh = min((place[c.url] for c in plan.fresh), default=None)
    if plan.mode == "page" and stored and first_fresh is not None and stored[0] < first_fresh:
        # Stored stories lead the listing: pinned, when a story stored or
        # already read comes below.
        below = sorted(
            place[c.url]
            for c in plan.candidates
            if (c.url not in fresh or c.url in read) and place[c.url] > first_fresh
        )
        if below:
            first_stored = below[0]

    def backlog(url: str) -> bool:
        changed = modified.get(url)
        if plan.mode == "page" or changed is None:
            return first_stored is not None and place.get(url, 0) > first_stored
        return backlog_before is not None and changed < backlog_before

    return backlog


class GenericScrapePlugin(SourcePlugin):
    """SourcePlugin bound to a single ``type="generic_scrape"`` Source row.

    Construction mirrors ``DynamicYouTubePlugin`` — see that class for
    the rationale on mirroring the row's fields onto ``self``.
    """

    # ``fetch`` takes ``known_urls`` and ``max_undated`` (see
    # ``SourcePlugin.takes_ingest_hints``).
    takes_ingest_hints = True

    def __init__(self, source_row: Source) -> None:
        self._source_row = source_row
        self.name = source_row.name
        self.type = source_row.type
        self.category = source_row.category
        self.url = source_row.url
        self.refresh_interval_seconds = source_row.refresh_interval_seconds
        self.source_id = source_row.id
        # The address this instance fetches (see ``SourcePlugin.fetch_target``).
        self.fetch_target = row_fetch_target(source_row)
        # Optional direct sitemap URL. When set, the plugin skips
        # robots.txt and the guessed sitemap paths (which don't work
        # on every site — e.g. theprogress.com's sitemap_index has
        # 1100+ cross-domain entries that all 404) and reads the
        # user-specified sitemap first. Same code path, same SSRF
        # guard, same dedup; just a more specific entry point. None
        # falls back to the original behavior so old rows aren't
        # affected by the migration.
        self.sitemap_url: Optional[str] = getattr(source_row, "sitemap_url", None)
        # Optional path-prefix for the page_links strategy. When
        # set, only links (and sitemap entries) under this prefix
        # are candidates, and the listing page is read with a plain
        # prefix filter instead of ``listing_links``. Designed for
        # sites that don't publish a sitemap at all (e.g. ollama.com)
        # but DO expose a "list of recent items" page with stable URL
        # shapes (``/library/<model>``, ``/blog/<slug>``, etc).
        # gettattr with a default preserves pre-migration rows.
        self.link_pattern: Optional[str] = getattr(source_row, "link_pattern", None)
        # B4 fix: user-supplied custom HTTP headers (e.g. a custom
        # User-Agent for sites that block default UAs, or an
        # Authorization header for paywalled content). Stored on
        # the Source row via ``add_source`` / ``update_source``;
        # forwarded to every request this feed makes: robots.txt,
        # sitemaps, the listing page and each candidate URL.
        # ``None`` (or empty dict) means "use default headers only" —
        # the pre-B4 behavior.
        self.custom_headers: dict | None = getattr(source_row, "custom_headers", None)
        # In-process cache of URLs we've already attempted extraction
        # for — see the module docstring for why this instance
        # persists across polls. Resets on backend restart; stored
        # pages are skipped through ``known_urls`` anyway, so a restart
        # only means one extra pass over pages that were fetched but
        # not kept (not articles, too old, over the undated cap).
        #
        # Slice 27: bounded via ``OrderedDict`` with FIFO eviction at
        # ``_MAX_EXTRACTED_URLS``. Without the bound, a source with a
        # growing sitemap (e.g. a news site with daily new articles)
        # accumulates URLs for the lifetime of the backend process —
        # tens of thousands of entries over weeks. The DB dedup
        # constraint is the real correctness backstop, but the in-
        # memory set still paid linear-cost scans + memory. The
        # FIFO cap gives a worst-case fixed footprint; eviction
        # just means a previously-seen URL gets re-attempted, which
        # the entries-table UNIQUE constraint silently no-ops.
        self._extracted_urls: "OrderedDict[str, _Read]" = OrderedDict()
        # Consecutive failed polls per page whose fetch failed in a way
        # that passes (see ``_page_failed``); bounded FIFO.
        self._page_failures: "OrderedDict[str, int]" = OrderedDict()
        # Pages a poll left for a later one (refused, failed in a way that
        # passes, or read unvouched) while they weren't backlog. Their
        # next read keeps that verdict (see ``fetch``); bounded FIFO.
        self._deferred_new: "OrderedDict[str, None]" = OrderedDict()

    def _mark_extracted(self, url: str, *, served: bool = False, unvouched: bool = False) -> None:
        """Add ``url`` to the seen-set, evicting the oldest entry if
        the cap is exceeded. ``OrderedDict.popitem(last=False)`` is
        O(1) and returns FIFO order.

        ``served``: the site answered with a page (``_Read.served``).
        ``unvouched``: the page was read in a poll whose usual read gave
        nothing or had a sitemap fail (see ``fetch``), so ``_seen`` lets a
        poll whose usual read lists it, complete, read it once more.
        """
        # ``setdefault`` would also work but ``__setitem__`` is faster
        # and we don't need the existing-value semantics.
        self._extracted_urls[url] = _Read(served, unvouched)
        self._page_failures.pop(url, None)
        if not unvouched:
            self._deferred_new.pop(url, None)
        while len(self._extracted_urls) > _MAX_EXTRACTED_URLS:
            self._extracted_urls.popitem(last=False)

    def _seen(self, url: str, usual: bool) -> bool:
        """Whether this poll skips ``url`` as already read: it is marked,
        unless it was marked ``unvouched`` and this poll's candidates
        came from the feed's usual read with nothing failed (``usual``)."""
        if url not in self._extracted_urls:
            return False
        return not (usual and self._extracted_urls[url].unvouched)

    def _page_failed(self, url: str, exc: BaseException) -> None:
        """A page's fetch failed in a way that passes (a timeout, a
        network error, a 5xx). It stays unmarked, so the next poll asks
        for it again, until it has failed ``_MAX_PAGE_FAILURES`` polls in
        a row: then it is marked like a page that was read, so a page
        that never answers can't take a slot of every poll."""
        failures = self._page_failures.pop(url, 0) + 1
        if failures >= _MAX_PAGE_FAILURES:
            logger.info(
                "generic_scrape: %s: %s failed %d checks in a row (%s); skipping it",
                self.name, url, failures, exc,
            )
            self._mark_extracted(url)
            return
        logger.debug("generic_scrape: %s: %s failed (%s); asking again next check", self.name, url, exc)
        self._page_failures[url] = failures
        while len(self._page_failures) > _MAX_PAGE_FAILURE_URLS:
            self._page_failures.popitem(last=False)

    async def fetch(
        self,
        *,
        known_urls: KnownUrls | None = None,
        max_undated: int | None = None,
        backlog_before: dt.datetime | None = None,
    ) -> list[dict]:
        """One poll: plan the candidates (``plan_candidates``), fetch
        and score up to ``_MAX_NEW_PER_POLL`` new ones, and return the
        articles, newest candidate first.

        ``known_urls``, ``max_undated`` and ``backlog_before`` come from
        ``scheduler._ingest`` (``takes_ingest_hints``). Called without
        them, nothing counts as stored, the undated cap is
        ``ingest.max_undated_per_poll``'s default, and the poll is read
        as a first fetch (see ``_cap_undated``).

        Raises when the feed can't read its list of articles (see
        ``plan_candidates``) and when the site refuses every page this
        poll asked for (see ``_settle_refusals``). A poll whose
        candidates are all stored or already tried returns ``[]``: the
        list is there, it just has nothing new.
        """
        # If the row carries an explicit sitemap_url, it is read first
        # and directly — finding the sitemap from a homepage URL
        # (robots.txt, guessed paths) is fragile (e.g. theprogress.com's
        # sitemap_index is 1.1MB of cross-domain entries that all 404).
        # Letting the user point at a known-good sitemap URL side-steps
        # the discovery heuristic entirely.
        plan = await plan_candidates(
            self.url,
            self.sitemap_url,
            self.link_pattern,
            self.custom_headers,
            known_urls,
            raise_errors=True,
        )
        # The owner's choice is what makes a page an article for
        # ``score_page``, but only when the candidates came from it: the
        # link pattern filters both reads, while a ``sitemap_url`` that
        # listed nothing leaves the home page's links, which the owner
        # never chose.
        owner_configured = bool(self.link_pattern) or (
            bool(self.sitemap_url) and plan.mode == "sitemap"
        )
        # A poll whose usual read failed or found nothing (a 503, a
        # timeout, an empty answer while a sitemap rebuilds) reads the
        # other one (``Plan.fallback``), which judges a page on other
        # evidence: a listing page's links carry no news sitemap date (nor
        # a ``sitemap_url``'s word), a sitemap's entries no listing card's
        # date, and the backlog rule reads another order. A sitemap read
        # with one document failed (``Plan.degraded``: the news sitemap
        # down, the post sitemap up) lacks that document's evidence too.
        # So each page such a poll reads is marked as read on that
        # evidence (``unvouched``), and a later poll whose usual read
        # lists it, complete, reads it once more: one bad poll mustn't
        # lose a page.
        complete = not (plan.fallback or plan.degraded)
        unvouched = not complete
        if plan.fallback and self.sitemap_url:
            logger.info(
                "generic_scrape: %s: its sitemap_url %s gave no pages; read %s instead",
                self.name, self.sitemap_url, self.url,
            )
        elif plan.fallback:
            logger.debug(
                "generic_scrape: %s: its usual read gave no pages; read the %s instead",
                self.name, plan.mode,
            )
        elif plan.degraded:
            logger.debug(
                "generic_scrape: %s: a sitemap failed; its pages are read again once all load",
                self.name,
            )
        batch: list[Candidate] = []
        for candidate in plan.fresh:
            if self._seen(candidate.url, complete):
                continue
            if len(batch) >= _MAX_NEW_PER_POLL:
                # Leave the rest for the next poll — see
                # _MAX_NEW_PER_POLL. Deliberately NOT marked as
                # extracted, so they're candidates again next cycle
                # instead of being skipped forever.
                break
            batch.append(candidate)
        # A page an earlier poll left for later while it was new is still
        # new: a story stored in the meantime and listed above it doesn't
        # make it backlog (``_cap_undated``).
        new_before = {c.url for c in batch if c.url in self._deferred_new}
        backlog = _backlog_rule(plan, backlog_before, self._extracted_urls)
        # The pages the site answered with a page (``_extract_one``).
        served: set[str] = set()

        # Fetch the batch concurrently (bounded — see
        # _FETCH_CONCURRENCY) instead of one at a time: a source
        # whose pages take several seconds each would otherwise
        # serialize a full _MAX_NEW_PER_POLL batch into a single long
        # scheduler tick for no correctness reason (each URL's fetch
        # and extraction is independent of every other's).
        sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def _bounded_extract(candidate: Candidate) -> dict | None:
            async with sem:
                # B4 fix: pass custom_headers through to _extract_one
                # so user-supplied headers (e.g. a custom User-Agent
                # or Authorization) are actually used by the HTTP
                # fetch. Previously the headers were stored on the
                # Source row but never passed to fetch_html.
                return await _extract_one(
                    candidate.url,
                    custom_headers=self.custom_headers,
                    owner_configured=owner_configured,
                    listed_date=candidate.published,
                    served=served,
                )

        results = await asyncio.gather(
            *(_bounded_extract(candidate) for candidate in batch), return_exceptions=True,
        )

        items: list[dict] = []
        refused: dict[str, FetchRefusedError] = {}
        for candidate, result in zip(batch, results, strict=True):
            url = candidate.url
            if isinstance(result, FetchRefusedError):
                # Not marked: a later poll (for example after Read as
                # Safari) asks for the page again.
                refused[url] = result
                continue
            if isinstance(result, httpx.HTTPError):
                # A timeout, a network error or a 5xx: it passes, so
                # the page is asked for again (``_page_failed``). It
                # never fails the poll.
                self._page_failed(url, result)
                continue
            # Use the bounded helper (FIFO eviction at
            # ``_MAX_EXTRACTED_URLS``) instead of ``.add()`` to keep
            # the in-memory set's footprint fixed.
            self._mark_extracted(url, served=url in served, unvouched=unvouched)
            if isinstance(result, BaseException):
                logger.debug(
                    "generic_scrape: extraction failed for %s: %s", url, result,
                )
                continue
            if result is not None and not _too_old(result):
                items.append(result)
        # The pages this poll leaves for a later one (refused, failed in a
        # way that passes, or read unvouched) while they are new stay new
        # until they are read for good.
        for candidate in batch:
            read = self._extracted_urls.get(candidate.url)
            if (read is None or read.unvouched) and not backlog(candidate.url):
                self._deferred_new[candidate.url] = None
        while len(self._deferred_new) > _MAX_PAGE_FAILURE_URLS:
            self._deferred_new.popitem(last=False)
        if batch and len(refused) == len(batch):
            await self._settle_refusals(refused, plan)
        return self._cap_undated(items, backlog, max_undated, new_before)

    async def _settle_refusals(self, refused: dict[str, FetchRefusedError], plan: Plan) -> None:
        """Every page this poll asked for was refused. A site that
        serves its sitemap but refuses its article pages is common (a
        challenge in front of the pages), and so is a members-only page
        among public ones. To tell them apart, fetch a page of the site
        outside the batch as a control. The controls are the stored
        stories, newest first (each loaded as an article before), then
        the pages this feed read and didn't keep that answered with a
        page (too old, not an article, over the undated cap: a site that
        posts rarely has stored nothing), then the candidates not tried
        yet. A page that was gone, wasn't a page, kept failing or was a
        members-only page skipped on an earlier poll is never one: none
        of those can say whether the site serves us.

        - The control serves a page: the site serves us, and these pages
          refuse everyone. The 401-403 ones are marked, so a lone
          members-only page can't fail every poll and stop the feed. A
          429 stays unmarked: a rate limit passes.
        - The control is gone (404, 410): it says nothing about us, so
          the next candidate is tried, up to ``_MAX_CONTROLS``.
        - Anything else, or no control at all (a new feed's first poll):
          the site doesn't serve us. A refusal, a 5xx (a challenge page
          often answers 503), a network error, or an answer with no
          page in it (not HTML, empty). Raise the refusal, so the feed
          reads ``[blocked]`` (Read as Safari), or ``[http error]`` for
          a 429. The pages stay unmarked, so the next poll asks for them
          again.
        """
        served: str | None = None
        fresh = {c.url for c in plan.fresh}
        others = [c.url for c in plan.fresh if c.url not in refused]
        read = self._extracted_urls
        controls = (
            [c.url for c in plan.candidates if c.url not in fresh]
            + [url for url in others if url in read and read[url].served]
            + [url for url in others if url not in read and url not in self._page_failures]
        )
        for control in controls[:_MAX_CONTROLS]:
            try:
                body = await fetch_html_checked(
                    control, custom_headers=self.custom_headers, raise_for_status=True
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 410):
                    continue
                break
            except httpx.HTTPError:
                break
            if body is not None:
                served = control
            break
        if served is None:
            exc = most_telling_error(refused.values())
            assert exc is not None  # ``refused`` is never empty here
            raise exc
        logger.info(
            "generic_scrape: %s: %d page(s) refused while %s loads; skipping them",
            self.name, len(refused), served,
        )
        for url, exc in refused.items():
            if exc.response.status_code != 429:
                self._mark_extracted(url)

    def _cap_undated(
        self,
        items: list[dict],
        backlog: Callable[[str], bool],
        max_undated: int | None,
        new_before: set[str],
    ) -> list[dict]:
        """At most ``max_undated`` undated pages per poll (D12), and none
        that are old backlog (``_backlog_rule``). This is the rule
        ``ingest_rules`` applies to every other feed type, which it can't
        apply to this plugin's items because they never include stored
        URLs. An undated page here is one whose page and list both gave
        no publication date.

        A page in ``new_before`` was left for this poll by an earlier one
        (refused, failed in a way that passes, or read unvouched: see
        ``fetch``) while it wasn't backlog, so it isn't now: it
        would have come in then, before the stories stored since.

        Every page dropped here stays marked extracted: it was read and
        passed over, and fetching it again every poll would only drop it
        again."""
        cap = _DEFAULT_MAX_UNDATED if max_undated is None else max_undated
        kept: list[dict] = []
        undated = dropped = 0
        for item in items:
            if item.get("published_at") is not None:
                kept.append(item)
            elif undated < cap and (item["url"] in new_before or not backlog(item["url"])):
                kept.append(item)
                undated += 1
            else:
                dropped += 1
        if dropped:
            logger.info("generic_scrape: %s: skipped %d undated page(s)", self.name, dropped)
        return kept

    # normalize() is inherited from SourcePlugin's default
    # (validate_required) — the dict shape _extract_one returns
    # already matches its contract (title/url required, everything
    # else flows into meta).
