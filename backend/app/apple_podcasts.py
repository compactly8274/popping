"""Resolve an Apple Podcasts catalog link to its actual RSS feed URL.

An Apple Podcasts show page (``https://podcasts.apple.com/us/podcast/
<slug>/id<N>``) is what a browser or the Apple Podcasts app shows you —
it's HTML, not a feed, and pasting it directly into a podcast source
fails ingestion (feedparser can't extract entries from an HTML page;
the source auto-disables after enough consecutive failures). The only
way to get the real feed URL used to be manually querying Apple's
lookup API and hunting through the JSON for "feedUrl" by hand.

This module does that resolution server-side: detect the pattern,
extract the numeric collection id, call Apple's public (documented,
unauthenticated) lookup API, and return the ``feedUrl`` field. Wired
into the source Test/Create/Update routes so pasting the Apple
Podcasts link just works.
"""

from __future__ import annotations

import re

import httpx

# Matches the numeric collection id in an Apple Podcasts show URL —
# e.g. "https://podcasts.apple.com/us/podcast/how-did-this-get-made/
# id409287913" -> "409287913". Deliberately doesn't try to capture an
# episode id (a "?i=..." query param some Apple Podcasts links carry)
# — the lookup API resolves at the show/collection level, and a
# specific episode within that show isn't a distinct feed.
_APPLE_PODCASTS_URL_RE = re.compile(r"podcasts\.apple\.com/.*?/id(\d+)", re.IGNORECASE)

_LOOKUP_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=15.0)


class PodcastResolutionError(RuntimeError):
    """Raised when an Apple Podcasts URL is detected but can't be
    resolved to a real feed URL — network failure, unknown id, or a
    lookup response with no ``feedUrl`` (e.g. a show whose full feed
    is gated behind a subscriber-only app)."""


def apple_podcasts_id(url: str) -> str | None:
    """Extract the numeric collection id from an Apple Podcasts show
    URL, or None if ``url`` doesn't match the pattern. Pure, no I/O —
    lets a caller decide whether resolution is even worth attempting."""
    m = _APPLE_PODCASTS_URL_RE.search(url)
    return m.group(1) if m else None


async def resolve_feed_url(url: str) -> str:
    """Resolve ``url`` to a real RSS feed URL if it's an Apple
    Podcasts show page; otherwise return it unchanged. Safe to call
    unconditionally on every source URL before validation — a no-op
    pass-through for anything that doesn't match the Apple Podcasts
    pattern (including reddit references and ordinary feed URLs).

    The lookup call always targets ``itunes.apple.com`` — a fixed,
    trusted host this function constructs itself from the extracted
    id, never the caller's URL — so it doesn't need the
    ``check_url_safe`` SSRF guard applied to user-supplied feed URLs
    elsewhere in this codebase.
    """
    podcast_id = apple_podcasts_id(url)
    if podcast_id is None:
        return url
    lookup_url = f"https://itunes.apple.com/lookup?id={podcast_id}"
    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT) as client:
            resp = await client.get(lookup_url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PodcastResolutionError(
            f"couldn't resolve the Apple Podcasts link (id {podcast_id}): {exc}"
        ) from exc
    results = data.get("results") or []
    if not results or not isinstance(results[0], dict):
        raise PodcastResolutionError(
            f"Apple Podcasts id {podcast_id} wasn't found by Apple's lookup API"
        )
    feed_url = results[0].get("feedUrl")
    if not feed_url:
        raise PodcastResolutionError(
            "this show doesn't expose a public RSS feed in Apple's catalog "
            "(often means it's subscriber/premium-only) — paste the feed "
            "URL directly if you have one from another source"
        )
    return str(feed_url)
