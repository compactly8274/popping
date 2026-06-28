"""RSS feed source — phase 1's first source, phase 3 widens it.

Fetches any RSS / Atom feed via feedparser-over-httpx. Concrete plugins
are instantiated at module load via the `@register_source` decorator;
the scheduler picks them up by name from `list_sources()`.

BBC News is the default source: no auth, stable URL, exercises the
ingestion path end-to-end.

User-Agent note: many feeds (BBC since 2024, Reddit, etc.) throttle
or 403 the default `python-httpx` UA. We send a descriptive UA and
an Accept header that explicitly asks for RSS/Atom — both cheap and
uncontroversial.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import feedparser
import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"
_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": _ACCEPT,
}


def _parse_published(entry: Any) -> dt.datetime | None:
    """Best-effort parse of a feedparser entry's published/updated field."""
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


# First <img src> in a summary blob. Lazy fallback when the feed
# doesn't ship a structured image field — common in WordPress-style
# HTML summaries.
_IMG_SRC_RE = re.compile(
    r"""<img\s[^>]*?src=["']([^"']+)["']""",
    re.IGNORECASE,
)


def _pick_image_url(entry: Any) -> str | None:
    """Best thumbnail URL from a feedparser entry, or None.

    Priority matches what real feeds actually ship:
      1. media:thumbnail (Media RSS — most common)
      2. media:content with image/* type
      3. enclosure with image/* type (RSS 2.0)
      4. itunes:image (podcast artwork)
      5. first <img src> regex over summary (HTML summaries)
    """
    # 1. media:thumbnail
    mt = entry.get("media_thumbnail")
    if mt:
        url = mt[0].get("url") if isinstance(mt, list) else mt.get("url")
        if url:
            return url
    # 2. media:content
    mc = entry.get("media_content")
    if mc:
        items = mc if isinstance(mc, list) else [mc]
        for m in items:
            ct = (m.get("type") or "").lower()
            if ct.startswith("image/") and m.get("url"):
                return m["url"]
    # 3. enclosure
    for enc in entry.get("enclosures") or []:
        ct = (enc.get("type") or "").lower()
        if ct.startswith("image/") and enc.get("href"):
            return enc["href"]
    # 4. itunes:image
    ii = entry.get("image")
    if isinstance(ii, dict) and ii.get("href"):
        return ii["href"]
    # 5. inline <img src> in summary
    summary = entry.get("summary") or ""
    if summary:
        m = _IMG_SRC_RE.search(summary)
        if m:
            return m.group(1)
    return None


async def fetch_rss(url: str) -> list[dict]:
    """Fetch and parse any RSS/Atom feed at ``url``.

    Module-level helper so the class-driven ``_RssPlugin`` and the
    row-driven ``DynamicRssPlugin`` (see ``dynamic_rss.py``) share
    the same parsing logic. Image picking uses the same priority as
    the BBC plugin: media:thumbnail → media:content (image/*) →
    enclosure (image/*) → itunes:image → first <img src> in summary.
    No DB / scheduler awareness here — this is a pure HTTP→list[dict]
    function that the scheduler's ``_ingest`` consumes through the
    plugin's ``fetch()`` method.
    """
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=_DEFAULT_HEADERS)
        resp.raise_for_status()
    feed = feedparser.parse(resp.text)
    items: list[dict] = []
    for entry in feed.entries:
        image_url = _pick_image_url(entry)
        items.append(
            {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published_at": _parse_published(entry),
                "summary": entry.get("summary", ""),
                # Top-level so the ingest pipeline can pop it out of
                # meta cleanly. NULL when the feed ships no image.
                "image_url": image_url,
            }
        )
    return items


class _RssPlugin(SourcePlugin):
    """Generic RSS/Atom fetcher. Subclasses set name/url/refresh.

    Thin wrapper around ``fetch_rss`` so the class-driven plugins
    (BBC today; future built-ins if any) and the row-driven
    ``DynamicRssPlugin`` share one implementation.
    """

    type = "rss"
    category = "news"

    async def fetch(self) -> list[dict]:
        return await fetch_rss(self.url)


@register_source
class BbcNews(_RssPlugin):
    name = "bbc_news"
    url = "https://feeds.bbci.co.uk/news/rss.xml"
    refresh_interval_seconds = 3600  # 1 hour