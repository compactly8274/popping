"""RSS feed source — phase 1's single working source.

Fetches any RSS / Atom feed via feedparser-over-httpx. Concrete plugins
are instantiated at module load via the `@register_source` decorator;
the scheduler picks them up by name from `list_sources()`.

BBC News is the default source: no auth, stable URL, exercises the
ingestion path end-to-end.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import feedparser
import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin


def _parse_published(entry: Any) -> dt.datetime | None:
    """Best-effort parse of a feedparser entry's published/updated field."""
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


class _RssPlugin(SourcePlugin):
    """Generic RSS/Atom fetcher. Subclasses set name/url/refresh."""

    type = "rss"
    category = "news"

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(self.url, headers={"User-Agent": "popping/0.1"})
            resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        items: list[dict] = []
        for entry in feed.entries:
            items.append(
                {
                    "title": entry.get("title", ""),
                    "url": entry.get("link", ""),
                    "published_at": _parse_published(entry),
                    "summary": entry.get("summary", ""),
                }
            )
        return items


@register_source
class BbcNews(_RssPlugin):
    name = "bbc_news"
    url = "http://feeds.bbci.co.uk/news/rss.xml"
    refresh_interval_seconds = 3600  # 1 hour