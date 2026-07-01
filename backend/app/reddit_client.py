"""Reddit client using the public Atom (``.rss``) feed endpoints.

Popping reads Reddit in one way now:

  - **Direct Atom mode** (only). Calls go straight to
    ``https://www.reddit.com/r/<sub>/<listing>.rss`` with a contact
    User-Agent and a per-process rate limiter. Returns Atom XML,
    parsed in-process. No proxy, no third-party service.

Why Atom instead of ``.json``?

The public ``.json`` endpoints (and ``search.json``) are CDN-gated
on most residential and VPS IP ranges. Anonymous requests from a
home IP get a 190 KB HTML block page, an opaque JS challenge, or
the per-IP rate-limit envelope (~1 req/min on a low-reputation IP).
This is true on the TrueNAS TELUS residential IP, on Cloudflare
Workers' egress, and (per operator report) on the operator's paid
VPS. There is no free, no-third-party path that handles popping's
~60 calls/hour.

The ``.rss`` path is **not CDN-gated the same way** — it lives on
the same Fastly edge but a different code path that doesn't do the
per-IP UA-challenge. Anonymous requests to ``/r/<sub>/hot.rss`` get
real Reddit Atom XML, with the same per-IP 60-call/hour rate limit
the JSON path has. Verified July 2026: 200 OK, 25 entries, real
titles / authors / permalinks.

Trade-offs vs the old ``.json`` mode:

  - **Listing data** is mostly the same: title, permalink,
    subreddit, author, updated time. We synthesize the ``id``,
    ``url``, ``created_utc``, ``score``, ``num_comments`` from
    what's in the Atom entry. ``score`` and ``num_comments`` aren't
    in the Atom body, so they show as ``None`` in the meta blob —
    the existing engagement scoring treats ``None`` as 0, so
    the visible effect is a slightly lower engagement score on
    Reddit cards. Acceptable.
  - **Cross-reference sweep** (``search_thread_by_url``) can't
    ask "is there a thread about URL X" via the Atom feed —
    Reddit doesn't have a search endpoint that does per-URL lookup
    on .rss. The new implementation searches the **in-memory
    cache of recent subreddit listings** for the URL. The
    sweep only catches URLs that were posted in the last 15 min
    in one of the configured subreddits. This is a strict
    reduction in coverage compared to the old per-URL ``/search``,
    but it works without per-URL network calls and uses the same
    HTTP rate budget as the listings. URLs posted in older
    threads won't be discovered — that's the trade.

The two operational signals:

  - ``is_configured()`` returns True whenever the feature is on
    (proxy URL set, OR direct mode not disabled). Same as before.
  - ``is_disabled()`` is True when both modes are off.

The two public coroutines never raise. ``fetch_subreddit`` and
``search_thread_by_url`` return ``[]`` / ``None`` on any failure
and log at DEBUG so the scheduler can keep ticking. The next
scheduled tick retries.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger("popping.reddit_client")

# Default timeout for Reddit calls. Atom listings are 30-100 KB, the
# round-trip should be sub-second. 10 s gives headroom for slow days
# without blocking the scheduler's next job.
_TIMEOUT = 10.0
# 2 MB cap on response bodies. A /r/python hot listing with 100 items
# is ~60 KB; the cap defends against a misbehaving upstream returning
# a multi-GB body.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Rate-limiter parameters for direct Atom mode. Reddit's anonymous
# rate limit is ~60 calls/hour, but a low-reputation IP gets a tighter
# window (~1/min per IP). The in-process token bucket shapes the
# outgoing traffic to 1 call / 65 s sustained, with a small initial
# burst of 2 calls (so a startup that needs to fetch 2-3 subreddit
# listings can do them in a few seconds rather than over 3 minutes).
_DIRECT_RPS = 1.0 / 65.0   # ~1 call per 65 s
_DIRECT_BURST = 2.0

# Cross-reference cache: how long a fetched subreddit listing stays
# in memory. The cross-ref sweep scans this cache to answer
# "is there a thread about URL X" without doing a per-URL fetch.
# 15 min is the per-subreddit refresh interval — keeps the cache
# aligned with the listings' freshness.
_CROSSREF_CACHE_TTL_S = 15 * 60

# Default User-Agent. Reddit's RSS endpoints don't require a contact
# string the way the JSON endpoints do (the JS challenge is what
# the JSON endpoint uses to enforce contact, and the RSS endpoint
# doesn't do JS challenges). Still, a contact UA is good hygiene.
_DEFAULT_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"
_DEFAULT_USER_AGENT_WITH_CONTACT = (
    "Popping/0.2 (+https://github.com/compactly8274/popping; "
    "contact: phil@philjnewman.com)"
)

# Atom XML namespaces. Reddit's hot.rss feed declares both ATOM
# (the feed itself) and MRSS (media thumbnails). The cross-ref
# sweep only needs the ATOM link and the ATOM title, so we don't
# reach for MRSS here. If the feed ever drops ATOM and uses RSS
# 2.0 only, this breaks loudly — that's the right behavior.
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
}

# Shared clients — one for proxy mode, one for direct mode. Both
# are ``None`` before ``init_client()`` and re-built on every
# lifespan cycle. ``_get_client()`` constructs a defensive one-off
# if a route fires before startup (e.g. during tests).
_proxy_client: Optional[httpx.AsyncClient] = None
_direct_client: Optional[httpx.AsyncClient] = None
# Token-bucket state for direct mode.
_bucket_tokens: float = _DIRECT_BURST
_bucket_last: float = time.monotonic()
_bucket_lock = asyncio.Lock()

# Cross-reference cache. Keyed by subreddit name. Value is a tuple
# ``(fetched_at_monotonic, [post_dict, ...])`` of the listings
# returned by the most recent ``fetch_subreddit`` call for that
# subreddit. The cross-ref sweep scans this rather than hitting
# Reddit per-URL.
_crossref_cache: dict[str, tuple[float, list[dict]]] = {}


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True when at least one of proxy / direct mode can produce
    responses. Same as before — callers don't need to know whether
    we hit JSON or Atom under the hood.
    """
    if settings.reddit_hydra_url:
        return True
    if not settings.reddit_direct_disabled:
        return True
    return False


def is_disabled() -> bool:
    """True when both modes are off (proxy unset AND direct disabled).
    """
    return not bool(settings.reddit_hydra_url) and bool(
        settings.reddit_direct_disabled
    )


def _user_agent() -> str:
    """User-Agent string for Reddit calls.
    """
    ua = os.environ.get("REDDIT_USER_AGENT", "").strip()
    if ua:
        return ua
    return _DEFAULT_USER_AGENT_WITH_CONTACT


# ---------------------------------------------------------------------------
# Init / teardown
# ---------------------------------------------------------------------------


def init_client() -> None:
    """Build the shared clients. Idempotent. Logs once which mode
    is active so an operator scanning the startup log sees the
    operational posture.
    """
    global _proxy_client, _direct_client, _bucket_tokens, _bucket_last

    if settings.reddit_hydra_url:
        headers: dict[str, str] = {"User-Agent": _user_agent()}
        if settings.reddit_hydra_token:
            headers["Authorization"] = f"Bearer {settings.reddit_hydra_token}"
        _proxy_client = httpx.AsyncClient(
            base_url=settings.reddit_hydra_url.rstrip("/"),
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        )
        logger.info(
            "reddit_client: proxy mode (url=%s, auth=%s)",
            settings.reddit_hydra_url,
            "yes" if settings.reddit_hydra_token else "no",
        )
        return

    if not settings.reddit_direct_disabled:
        _direct_client = httpx.AsyncClient(
            base_url="https://www.reddit.com",
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": _user_agent(),
                # Ask for Atom. Reddit serves the same XML regardless
                # of Accept (the .rss path is a static file), but
                # sending the right Accept keeps any intermediary
                # honest.
                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        # Reset the bucket so the first burst of cross-ref requests
        # after a restart gets a clean slate.
        _bucket_tokens = _DIRECT_BURST
        _bucket_last = time.monotonic()
        # Invalidate the cross-ref cache so a process restart
        # re-fetches instead of using stale entries.
        _crossref_cache.clear()
        logger.info(
            "reddit_client: direct Atom mode (no proxy; "
            "throttled to 1/%.0fs, burst %.0f; "
            "reads r/<sub>/<listing>.rss instead of .json because "
            "Reddit's JSON endpoints 403 / 429 most residential IPs)",
            1.0 / _DIRECT_RPS, _DIRECT_BURST,
        )
        return

    logger.info("reddit_client: disabled (no proxy + REDDIT_DIRECT_DISABLED=1)")


async def close_client() -> None:
    """Tear down the shared clients. Called from FastAPI lifespan
    exit so connection pools don't leak across ``uvicorn --reload``
    cycles.
    """
    global _proxy_client, _direct_client
    for client_attr in ("_proxy_client", "_direct_client"):
        c = globals().get(client_attr)
        if c is not None:
            await c.aclose()
            globals()[client_attr] = None


# ---------------------------------------------------------------------------
# Token bucket for direct mode
# ---------------------------------------------------------------------------


async def _take_token() -> None:
    """Consume one token from the direct-mode bucket, blocking until
    one is available. See old version for full token-bucket math.
    """
    global _bucket_tokens, _bucket_last
    while True:
        async with _bucket_lock:
            now = time.monotonic()
            elapsed = now - _bucket_last
            _bucket_tokens = min(
                _DIRECT_BURST, _bucket_tokens + elapsed * _DIRECT_RPS
            )
            _bucket_last = now
            if _bucket_tokens >= 1.0:
                _bucket_tokens -= 1.0
                return
            deficit = 1.0 - _bucket_tokens
            wait_s = deficit / _DIRECT_RPS
        await asyncio.sleep(wait_s)


# ---------------------------------------------------------------------------
# HTTP fetcher (Atom XML body)
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    """Return whichever shared client is active. Constructs a
    defensive one-off if a route fires before lifespan startup
    (e.g. during tests). Proxy wins if both happen to be set.
    """
    if _proxy_client is not None:
        return _proxy_client
    if _direct_client is not None:
        return _direct_client
    if settings.reddit_hydra_url:
        headers = {"User-Agent": _user_agent()}
        if settings.reddit_hydra_token:
            headers["Authorization"] = f"Bearer {settings.reddit_hydra_token}"
        return httpx.AsyncClient(
            base_url=settings.reddit_hydra_url.rstrip("/"),
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        )
    return httpx.AsyncClient(
        base_url="https://www.reddit.com",
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": _user_agent(),
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )


async def _get_atom(path: str) -> str:
    """Stream a Reddit-or-proxy response into raw bytes. Enforces
    ``_MAX_RESPONSE_BYTES`` on the actual streamed bytes (not just
    the advisory Content-Length header). Returns the raw XML body
    as ``str`` (utf-8) or raises ``ValueError`` if the body is too
    large / the request failed. Caller parses the XML.

    Direct-mode calls take a rate-limit token first; proxy-mode
    calls don't (the proxy has its own limiter).
    """
    if _direct_client is not None and _proxy_client is None:
        await _take_token()
    client = _get_client()
    async with client.stream("GET", path) as resp:
        resp.raise_for_status()
        cl = resp.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_RESPONSE_BYTES:
            raise ValueError(
                f"reddit_client: response Content-Length {cl} exceeds "
                f"{_MAX_RESPONSE_BYTES} cap"
            )
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"reddit_client: response body exceeds "
                    f"{_MAX_RESPONSE_BYTES} cap"
                )
    return bytes(buf).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Atom XML -> post dict parser
# ---------------------------------------------------------------------------


def _parse_atom_entries(atom_xml: str) -> list[dict]:
    """Parse a Reddit ``/r/<sub>/<listing>.rss`` (Atom XML) feed and
    return a flat list of post dicts in the same shape the old
    ``.json`` parser returned. This is the key shape contract:
    ``dynamic_reddit`` reads ``title``, ``url``, ``permalink``,
    ``score``, ``num_comments``, ``author``, ``subreddit``, ``id``,
    ``created_utc`` from each dict.

    The Atom feed gives us:
      - title (``<entry><title>``)
      - author (``<entry><author><name>``, e.g. ``/u/foo``)
      - updated / published time (``<entry><updated>``, ``<entry><published>``)
      - permalink (``<entry><link rel="alternate" href="...">``)
      - subreddit (from the feed-level ``<category term=...>``)
      - id (the part of the permalink after ``/comments/``: ``t3_xxxxx``)

    What the Atom feed does **not** give us:
      - score (upvotes)
      - num_comments
      - the underlying outbound URL (for link posts)
      - thumbnail URL

    We synthesize the missing fields as ``None``. ``dynamic_reddit``
    stores them in the meta blob; the engagement scoring treats
    ``None`` as 0, so the visible effect is a slightly lower score
    on Reddit cards. Acceptable trade for not paying for a proxy.

    Defensive: the parser never raises on a malformed entry — it
    skips and returns the entries that did parse. A completely
    malformed feed raises (caller catches ValueError).
    """
    try:
        root = ET.fromstring(atom_xml)
    except ET.ParseError as exc:
        raise ValueError(f"reddit_client: malformed Atom feed: {exc}") from exc

    # Find the channel-level subreddit. Reddit puts it in
    # ``<feed><category term="..." label="r/..."/>``. Fall back to
    # parsing the feed id (``/r/python/hot.rss``).
    subreddit_name = ""
    cat = root.find("atom:category", _NS)
    if cat is not None and cat.get("term"):
        subreddit_name = cat.get("term", "").strip()
    if not subreddit_name:
        # Last-ditch: parse from the feed id
        feed_id = (root.findtext("atom:id", default="", namespaces=_NS) or "").strip()
        m = re.search(r"/r/([^/]+)/", feed_id)
        if m:
            subreddit_name = m.group(1)

    out: list[dict] = []
    for entry in root.findall("atom:entry", _NS):
        try:
            title = (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip()
            if not title:
                continue
            # Permalink. Reddit's Atom feed puts a single
            # ``<link href=".../comments/<id>/..."/>`` on each
            # entry (no ``rel="alternate"`` attribute — that's a
            # standard-Atom nicety Reddit skips). The feed-level
            # self-link (``<link rel="self" href=".../hot.rss"``)
            # lives on the feed, not the entry, so we don't
            # confuse them. Take the first entry-level ``<link>``
            # whose href points at reddit.com (not at the .rss
            # self-link and not at an external image / static
            # asset).
            permalink = ""
            outbound_url = ""
            for link in entry.findall("atom:link", _NS):
                href = link.get("href", "") or ""
                if not href:
                    continue
                # Skip the self-link to the .rss file.
                if href.endswith(".rss") or ".rss?" in href:
                    continue
                # Skip static assets / image CDNs.
                if "redditstatic.com" in href or "redditmedia.com" in href:
                    continue
                # Take the first non-static reddit link — that's
                # the permalink.
                permalink = href
                outbound_url = href
                break
            if not permalink:
                # No usable link — skip the entry.
                continue
            # Reddit post id from the permalink: /r/<sub>/comments/<id>/...
            m = re.search(r"/comments/([a-z0-9]+)/", permalink)
            reddit_id = m.group(1) if m else ""
            # Author
            author = (
                entry.findtext("atom:author/atom:name", default="", namespaces=_NS)
                or ""
            ).strip()
            # Timestamps. Prefer ``updated`` (Reddit bumps it on
            # edits); fall back to ``published``.
            ts_text = (
                entry.findtext("atom:updated", default="", namespaces=_NS)
                or entry.findtext("atom:published", default="", namespaces=_NS)
                or ""
            ).strip()
            created_utc: Optional[float] = None
            if ts_text:
                # Atom format: ``2026-07-01T19:15:27+00:00``
                try:
                    dt = datetime.fromisoformat(ts_text)
                    created_utc = dt.timestamp()
                except ValueError:
                    created_utc = None
            # Subreddit: from the feed-level category if available,
            # else the permalink.
            sub = subreddit_name
            if not sub:
                m = re.search(r"/r/([^/]+)/comments/", permalink)
                if m:
                    sub = m.group(1)
            out.append({
                # Fields the existing dynamic_reddit reads.
                "title": title,
                "url": outbound_url,
                "permalink": permalink,
                "subreddit": sub,
                "author": author,
                "id": reddit_id,
                "created_utc": created_utc,
                # Fields dynamic_reddit reads but Atom doesn't give
                # us. Stored as None so the meta blob's shape stays
                # consistent with the JSON-mode output; the
                # engagement scorer treats None as 0.
                "score": None,
                "num_comments": None,
            })
        except (AttributeError, KeyError, TypeError) as exc:
            # A specific entry had a weird shape — log DEBUG and
            # continue. Don't let one bad apple drop the whole feed.
            logger.debug(
                "reddit_client: skipping malformed Atom entry: %s", exc,
            )
            continue
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_subreddit(
    subreddit: str, listing: str = "hot", limit: int = 50
) -> list[dict]:
    """Fetch one subreddit's listing. Returns a flat list of post
    dicts — the same shape the old ``.json`` mode returned, so
    ``app.sources.dynamic_reddit`` doesn't need to know which path
    took the call. Returns ``[]`` on any failure (rate limit, 403,
    malformed XML, response too large). Logs DEBUG so the failure
    is visible without being noisy.

    ``listing`` is one of ``"hot"``, ``"new"``, ``"top"``,
    ``"rising"``. ``limit`` is honored but Reddit's Atom feed
    caps at 25-100 entries per page; we cap the requested limit
    to 100 to stay polite.

    The fetched listings are also written to ``_crossref_cache``
    so ``search_thread_by_url`` can answer "is there a thread
    about URL X" without a per-URL network call.
    """
    if is_disabled():
        return []
    # Cap the limit; the Atom feed's natural max is 100 and
    # asking for more just costs bandwidth.
    capped_limit = max(1, min(int(limit or 50), 100))
    # Reddit's .rss path: /r/<sub>/<listing>.rss?limit=N
    path = f"/r/{subreddit}/{listing}.rss?limit={capped_limit}"
    try:
        body = await _get_atom(path)
        posts = _parse_atom_entries(body)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug(
            "reddit_client: fetch_subreddit r/%s failed: %s", subreddit, exc,
        )
        return []
    # Cache the result for the cross-ref sweep. We only cache
    # successful (non-empty) results so a transient failure doesn't
    # pollute the cache.
    if posts:
        _crossref_cache[subreddit.lower()] = (time.monotonic(), posts)
    return posts


def _crossref_cache_get(subreddit: str) -> Optional[list[dict]]:
    """Return the cached posts for ``subreddit`` if the cache entry
    is fresh; otherwise return None and drop the entry. Used only
    by ``search_thread_by_url``.
    """
    key = subreddit.lower()
    entry = _crossref_cache.get(key)
    if entry is None:
        return None
    fetched_at, posts = entry
    if (time.monotonic() - fetched_at) > _CROSSREF_CACHE_TTL_S:
        _crossref_cache.pop(key, None)
        return None
    return posts


def _normalize_url_for_match(url: str) -> str:
    """Strip a URL down to the host + path (no scheme, no query,
    no trailing slash) for a forgiving equality match. Used by
    the cross-ref sweep to decide if a cached post's permalink
    points at a URL we're looking for.

    Why not full URL equality: the entries table stores the
    canonical outbound URL (e.g. ``https://www.bbc.co.uk/news/...``)
    but the cached Reddit posts only have the thread permalink
    (e.g. ``https://www.reddit.com/r/worldnews/comments/abc/...``).
    So we can never actually match the outbound URL to a post.
    Instead, the cross-ref sweep here does the inverse: it asks
    "is any of the recent Reddit posts a thread that *contains*
    the outbound URL as a link?" — which requires the Atom feed
    to include the outbound URL. Reddit's Atom feed does NOT do
    that (it's not in the entry's content or link elements).

    So the only thing the cross-ref sweep can usefully match is
    *the URL of the Reddit thread itself* — i.e. entries whose
    URL IS a reddit.com thread. That happens for entries from
    the Reddit source itself (the dynamic_reddit plugin stores
    the thread URL as the entry URL).

    Net: this is now a no-op for non-Reddit entries and a
    permalink-equality check for Reddit entries. We log
    explicitly so the operator knows what the sweep is doing.
    """
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    return f"{host}{path}".lower()


async def search_thread_by_url(url: str) -> Optional[dict]:
    """Look up a Reddit thread matching ``url``.

    Returns the first match as ``{"permalink": str, "num_comments": int}``
    or ``None`` if no match / cache empty.

    New implementation: scans the in-memory cache of recent
    subreddit listings. The cache is populated by
    ``fetch_subreddit`` whenever the per-subreddit plugin or the
    cross-ref sweep itself refreshes a subreddit.

    Trade-off: the sweep only catches matches where the URL is
    itself a Reddit thread permalink AND that permalink is in
    the cache. Non-Reddit URLs (BBC articles, HN links, etc.)
    won't match because the Atom feed doesn't include the
    outbound URL. The downstream ``meta.reddit_thread_url``
    stamp only fires for entries from the Reddit source itself
    in practice; for everything else, it's a no-op.

    This is documented as a regression vs. the old
    ``/search.json?q=url:`` path. The trade is honest: we keep
    the per-subreddit listings (the primary user-facing data)
    working and lose the cross-reference sweep's per-URL
    coverage. The operator can choose to add OAuth app-based
    search in the future if coverage matters.

    Never raises. Logs DEBUG on failure.
    """
    if is_disabled() or not url:
        return None
    needle = _normalize_url_for_match(url)
    if not needle:
        return None
    # Scan every cached subreddit for the URL.
    best: Optional[dict] = None
    stale = []
    for sub, (fetched_at, posts) in list(_crossref_cache.items()):
        if (time.monotonic() - fetched_at) > _CROSSREF_CACHE_TTL_S:
            stale.append(sub)
            continue
        for post in posts:
            post_url = _normalize_url_for_match(post.get("url") or "")
            if post_url and post_url == needle:
                best = {
                    "permalink": post.get("permalink", ""),
                    "num_comments": post.get("num_comments") or 0,
                }
                break
        if best:
            break
    for sub in stale:
        _crossref_cache.pop(sub, None)
    if best is None:
        # Documented behavior: the sweep only finds Reddit-thread
        # URLs in the recent cache. Non-Reddit URLs will never
        # match. Log at DEBUG so it's traceable without being
        # noisy — the scheduler fires this every hour.
        logger.debug(
            "reddit_client: search_thread_by_url %s — no match in cache "
            "(Atom mode can only match entries whose URL is itself a "
            "Reddit thread permalink)",
            url,
        )
    return best
