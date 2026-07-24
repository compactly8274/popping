"""Auto-discover a feed for a URL the user pastes into "Add custom" —
either an existing RSS/Atom feed, or (when none exists) a sitemap-
derived list of article URLs suitable for the generic-scrape fallback
source type (``app.sources.generic_scrape``).

Uses trafilatura's own feed/sitemap discovery (``trafilatura.feeds``,
``trafilatura.sitemaps``) rather than reimplementing "search a page
for <link rel=alternate>" or "guess an external service's API shape"
ourselves — well-tested, actively maintained, and already a project
dependency (added in ``app.article_extract`` for full-article LLM
summaries).

SSRF note: trafilatura does its own internal HTTP fetching for
redirects and any secondary URLs it discovers along the way (a
candidate feed URL, a sitemap linked from robots.txt, etc.) — those
calls are NOT individually routed through
``app.url_safety.check_url_safe`` the way every other network call in
this codebase is, because trafilatura has no hook for injecting an
external URL check into its internal fetch layer. We check the
user-supplied entry URL before calling into trafilatura at all (blocks
the direct/obvious case), but accept the internal-fetch gap as a
lower-severity, lower-probability trade for a single-operator
dashboard where the URL always originates from the operator's own
deliberate input, not an adversarial multi-tenant path. The ONGOING
periodic re-fetch of any discovered article URL (the generic_scrape
plugin's scheduled polling, not this one-time discovery step) goes
through ``app.article_extract.fetch_article_text``, which DOES apply
the full SSRF guard on every single call — so the unguarded window is
only this one-shot, user-initiated discovery request.
"""

from __future__ import annotations

import asyncio
import logging

from app.sources.rss import fetch_rss
from app.url_safety import check_url_safe

logger = logging.getLogger("popping.feed_autodiscovery")

_MAX_FEED_CANDIDATES = 5
_MAX_SITEMAP_URLS = 50


async def discover_feed_url(page_url: str) -> str | None:
    """Return a working RSS/Atom feed URL for ``page_url``, or None
    if none could be found.

    Tries ``page_url`` itself first — the user may have pasted a feed
    URL directly, which is the common case and needs no discovery at
    all — then asks trafilatura for candidate feed URLs on the page
    and tries each in turn until one actually parses with at least
    one item. A candidate merely existing in the page's markup isn't
    enough to trust; feed-detection heuristics false-positive on
    stale/broken `<link>` tags often enough that "does it actually
    parse" is the only real test.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        return None

    try:
        items = await fetch_rss(page_url)
        if items:
            return page_url
    except Exception:  # noqa: BLE001 - not itself a feed; fall through to discovery
        pass

    try:
        from trafilatura import feeds as trafilatura_feeds

        # find_feed_urls is synchronous AND does its own network I/O
        # internally (fetching the page, following redirects, probing
        # candidate feed URLs) — calling it directly here would block
        # the entire event loop for however long that takes (measured
        # 15-30s against an unreachable host), freezing every other
        # request the backend is handling concurrently. to_thread runs
        # it on a worker thread instead.
        candidates = await asyncio.to_thread(trafilatura_feeds.find_feed_urls, page_url)
    except Exception as exc:  # noqa: BLE001 - third-party discovery, never let it fail the request
        logger.info("feed_autodiscovery: find_feed_urls failed for %s: %s", page_url, exc)
        return None

    for candidate in candidates[:_MAX_FEED_CANDIDATES]:
        try:
            items = await fetch_rss(candidate)
        except Exception:  # noqa: BLE001
            continue
        if items:
            return candidate
    return None


async def discover_sitemap_urls(page_url: str, limit: int = _MAX_SITEMAP_URLS) -> list[str]:
    """Return up to ``limit`` article-page URLs from ``page_url``'s
    sitemap, or an empty list if none is found or discovery fails.
    Used as the "this site has no native feed" fallback when adding a
    ``type="generic_scrape"`` source — see
    ``app.sources.generic_scrape``.
    """
    safe, reason = check_url_safe(page_url)
    if not safe:
        logger.info("feed_autodiscovery: %s rejected by URL safety check (%s)", page_url, reason)
        return []
    try:
        from trafilatura import sitemaps as trafilatura_sitemaps

        # Same to_thread reasoning as find_feed_urls above —
        # sitemap_search is synchronous and does its own network I/O.
        urls = await asyncio.to_thread(trafilatura_sitemaps.sitemap_search, page_url)
    except Exception as exc:  # noqa: BLE001
        logger.info("feed_autodiscovery: sitemap_search failed for %s: %s", page_url, exc)
        return []
    return urls[:limit]
