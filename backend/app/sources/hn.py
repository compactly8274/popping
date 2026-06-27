"""Hacker News top stories.

Uses the public Firebase API at hacker-news.firebaseio.com. Two-step:
  1. GET /v0/topstories.json — returns the top ~500 story IDs.
  2. For each ID (capped at TOP_N), GET /v0/item/<id>.json — full story.

The API is anonymous, no auth, no rate limits beyond a courtesy ceiling.
We fetch the per-item endpoints concurrently with ``asyncio.gather`` so
the wall time is one round-trip rather than 30× serial.

Each item's ``score`` and ``descendants`` (comment count) land in
``Entry.meta`` so the UI can render them later.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.hn")

_BASE = "https://hacker-news.firebaseio.com/v0"
_TOP_N = 30  # plenty for a personal feed; well within rate-limit courtesy
_TIMEOUT = 10.0
_DEFAULT_HEADERS = {
    "User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)",
}


def _parse_hn_time(ts: int | float | None) -> dt.datetime | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc)


class _HnItem(dict):
    """Tiny wrapper to give us a normalize-able dict shape matching
    the rest of the source plugins."""

    pass


@register_source
class HnTop(SourcePlugin):
    name = "hn_top"
    type = "api"
    category = "tech"
    url = f"{_BASE}/topstories.json"
    refresh_interval_seconds = 300  # 5 min — HN moves fast

    async def fetch(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_DEFAULT_HEADERS) as client:
            r = await client.get(self.url)
            r.raise_for_status()
            ids: list[int] = r.json()[:_TOP_N]
            # Concurrent per-item fetch; cap concurrency so a slow HN
            # doesn't hold open 30 sockets if the API hiccups.
            sem = asyncio.Semaphore(8)

            async def _one(item_id: int) -> dict | None:
                async with sem:
                    try:
                        resp = await client.get(f"{_BASE}/item/{item_id}.json")
                        resp.raise_for_status()
                    except httpx.HTTPError as exc:
                        logger.debug("hn: item %d failed: %s", item_id, exc)
                        return None
                data = resp.json()
                if not data or data.get("deleted") or data.get("dead"):
                    return None
                return {
                    "title": data.get("title", ""),
                    "url": data.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
                    "published_at": _parse_hn_time(data.get("time")),
                    "summary": data.get("text", "") or "",
                    "meta": {
                        "hn_id": item_id,
                        "score": data.get("score"),
                        "comments": data.get("descendants"),
                        "by": data.get("by"),
                    },
                }

            results = await asyncio.gather(*(_one(i) for i in ids))
        return [r for r in results if r]