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
import json
import logging
from typing import Any

import httpx

from app.sources import register_source
from app.sources.base import SourcePlugin

logger = logging.getLogger("popping.sources.hn")

_BASE = "https://hacker-news.firebaseio.com/v0"
_TOP_N = 30  # plenty for a personal feed; well within rate-limit courtesy
_TIMEOUT = 10.0
# 5 MB. topstories.json is ~30 KB; per-item JSON is <5 KB.
# The cap defends against a compromised / misconfigured
# upstream returning a multi-gigabyte body before we OOM.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
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
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_DEFAULT_HEADERS,
            follow_redirects=True, max_redirects=5,
        ) as client:
            # Body cap helper: enforce ``_MAX_RESPONSE_BYTES`` on the
            # actual streamed bytes, not just the advisory
            # Content-Length header. Returns the parsed JSON or raises
            # ``ValueError`` if the body is too large / unparseable.
            async def _get_json(url: str) -> Any:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > _MAX_RESPONSE_BYTES:
                        raise ValueError(
                            f"hn: response Content-Length {cl} exceeds "
                            f"{_MAX_RESPONSE_BYTES} cap"
                        )
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_RESPONSE_BYTES:
                            raise ValueError(
                                f"hn: response body exceeds "
                                f"{_MAX_RESPONSE_BYTES} cap"
                            )
                return json.loads(bytes(buf))

            ids: list[int] = (await _get_json(self.url))[:_TOP_N]
            # Concurrent per-item fetch; cap concurrency so a slow HN
            # doesn't hold open 30 sockets if the API hiccups.
            sem = asyncio.Semaphore(8)

            async def _one(item_id: int) -> dict | None:
                async with sem:
                    try:
                        data = await _get_json(f"{_BASE}/item/{item_id}.json")
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.debug("hn: item %d failed: %s", item_id, exc)
                        return None
                if not data or data.get("deleted") or data.get("dead"):
                    return None
                return {
                    "title": data.get("title", ""),
                    "url": data.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
                    "published_at": _parse_hn_time(data.get("time")),
                    "summary": data.get("text", "") or "",
                    "meta": {
                        "hn_id": item_id,
                        # Legacy per-source keys kept for the schema and
                        # any UI that reads them by name.
                        "score": data.get("score"),
                        "comments": data.get("descendants"),
                        "by": data.get("by"),
                        # Canonical engagement keys consumed by
                        # ``app.scoring.engagement``. Same data as the
                        # legacy keys above; the canonical pair is what
                        # ``composite.score`` reads. Mirrored rather
                        # than renamed so the API surface for HN entries
                        # doesn't change.
                        "engagement_score": data.get("score"),
                        "engagement_comments": data.get("descendants"),
                    },
                }

            results = await asyncio.gather(*(_one(i) for i in ids))
        return [r for r in results if r]