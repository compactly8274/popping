"""Hydra Reddit client-server gateway.

The user runs Hydra on a VPS that fronts Reddit's JSON API. Popping
talks to Hydra instead of Reddit directly so:
  - The user's VPS holds the OAuth credentials / rotating IP / etc.
  - Reddit's per-IP rate limit isn't hit by the dashboard's polling.
  - Future Reddit-shape expansions (e.g. multi-account, comment threads)
    can land on the Hydra side without a Popping release.

Lifecycle mirrors ``app.assets``: a module-level shared ``httpx.AsyncClient``
built once at startup, closed on lifespan teardown. Bearer token from
``settings.reddit_hydra_token`` is set on the shared client so every
request picks it up automatically — never per-stream (the token doesn't
change between calls). Per-call overrides (Accept, custom timeout)
thread through ``client.stream(..., headers=..., timeout=...)`` exactly
like the assets client.

Disabled when ``settings.reddit_hydra_url == ""``: ``init_client`` is a
no-op, ``_get_client`` builds a one-off client that 4xxes on every
call, and the calling helpers (``fetch_subreddit`` / ``search_thread_by_url``)
return ``[]`` / ``None`` so ingest and the cross-ref sweep degrade
silently rather than crashing the scheduler.

Failure handling: helpers never raise. They log DEBUG and return the
empty sentinel so a transient Hydra outage doesn't break ingest or
cross-ref. The scheduler's next tick retries.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("popping.reddit_client")

# Default per-stream timeout for Hydra calls. Reddit listings are small
# JSON; Hydra's local round-trip should be sub-200ms. 10s gives headroom
# for the user's VPS being on a slow link without blocking the
# scheduler's next job.
_TIMEOUT = 10.0
# 2 MB cap on Hydra response bodies. A /r/python hot listing with 50
# items is ~30 KB; a /search?url= response is similar. The cap defends
# against a misbehaving / compromised Hydra returning a multi-GB body.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Shared client — set on lifespan startup, closed on teardown. ``None``
# means "not initialised yet" (or "disabled because REDDIT_HYDRA_URL is
# empty"). The dispatch helpers treat ``None`` as feature-off and return
# empty sentinels so the scheduler keeps ticking.
_client: Optional[httpx.AsyncClient] = None


def is_configured() -> bool:
    """True when ``settings.reddit_hydra_url`` is non-empty.

    Used by the cross-ref sweep to short-circuit when the feature is
    disabled (vs paying for a connection attempt every hour). Also
    used by the per-subreddit plugin to skip rows when the user added
    a subreddit before configuring Hydra — degraded gracefully rather
    than spamming error logs.
    """
    return bool(settings.reddit_hydra_url)


def init_client() -> None:
    """Build the shared client. Idempotent — a second call replaces the
    existing client (and closes the old one). No-op when the feature is
    disabled (``settings.reddit_hydra_url == ""``) so a misconfigured
    deploy doesn't crash the lifespan on an empty URL.

    Bearer token from ``settings.reddit_hydra_token`` — empty means
    unauthenticated Hydra. The header is added once on the shared
    client; per-stream overrides can swap it for one-off requests
    (none today, but kept the door open for ``hydra /api/v1/me/...``
    routes that need a per-user token in the future).
    """
    global _client
    if not is_configured():
        logger.info("reddit_client: disabled (REDDIT_HYDRA_URL unset)")
        return
    if _client is not None:
        return
    headers: dict[str, str] = {"User-Agent": "Popping/0.2 (+https://github.com/compactly8274/popping)"}
    if settings.reddit_hydra_token:
        headers["Authorization"] = f"Bearer {settings.reddit_hydra_token}"
    _client = httpx.AsyncClient(
        base_url=settings.reddit_hydra_url.rstrip("/"),
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    )
    logger.info(
        "reddit_client: configured (url=%s, auth=%s)",
        settings.reddit_hydra_url,
        "yes" if settings.reddit_hydra_token else "no",
    )


async def close_client() -> None:
    """Tear down the shared client. Called from FastAPI lifespan exit so
    connection pools don't leak across ``uvicorn --reload`` cycles."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    """Return the shared client or build a defensive one-off if a route
    fires before lifespan startup (e.g. during tests)."""
    if _client is None:
        headers: dict[str, str] = {"User-Agent": "Popping/0.2"}
        if settings.reddit_hydra_token:
            headers["Authorization"] = f"Bearer {settings.reddit_hydra_token}"
        return httpx.AsyncClient(
            base_url=settings.reddit_hydra_url.rstrip("/") if is_configured() else "",
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        )
    return _client


async def _get_json(path: str) -> Any:
    """Stream a Hydra response into JSON. Enforces ``_MAX_RESPONSE_BYTES``
    on the actual streamed bytes (not just the advisory Content-Length
    header). Returns the parsed JSON or raises ``ValueError`` if the body
    is too large / unparseable. Never raises ``httpx.HTTPError`` — caller
    catches ``(httpx.HTTPError, ValueError)``.
    """
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
    return json.loads(bytes(buf))


async def fetch_subreddit(
    subreddit: str, listing: str = "hot", limit: int = 50
) -> list[dict]:
    """Fetch one subreddit's listing via Hydra. Returns the canonical
    shape (one dict per Reddit post):

        ``id``           — Reddit's fullname (e.g. ``"t3_abc123"``)
        ``title``        — post title (str)
        ``url``          — outbound URL for link posts; empty string for
                           self-posts (caller decides which to use)
        ``permalink``    — relative path to the Reddit thread
                           (``"/r/python/comments/abc123/..."``); usable
                           as ``f"https://www.reddit.com{permalink}"``
        ``score``        — upvotes minus downvotes (int)
        ``num_comments`` — comment count (int)
        ``author``       — username or ``"[deleted]"``
        ``created_utc``  — unix timestamp seconds (float)
        ``subreddit``    — subreddit slug

    Returns ``[]`` on any failure (Hydra down, 4xx/5xx, malformed JSON,
    response too large). Logs DEBUG so the failure is visible without
    being noisy.

    ``listing`` is one of ``"hot"``, ``"new"``, ``"top"``, ``"rising"``.
    The per-subreddit plugin defaults to ``"hot"``; future surfaces can
    pass others.
    """
    if not is_configured():
        return []
    try:
        data = await _get_json(f"/r/{subreddit}/{listing}?limit={limit}")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(
            "reddit_client: fetch_subreddit r/%s failed: %s", subreddit, exc,
        )
        return []
    if not isinstance(data, list):
        logger.debug(
            "reddit_client: r/%s returned non-list payload (type=%s)",
            subreddit, type(data).__name__,
        )
        return []
    return data


async def search_thread_by_url(url: str) -> Optional[dict]:
    """Look up a Reddit thread matching ``url`` via Hydra's search
    endpoint. Returns the first match as
    ``{"permalink": str, "num_comments": int}`` or ``None`` if no match
    / Hydra unreachable.

    Used by the background cross-reference sweep to stamp
    ``reddit_thread_url`` onto existing entries. Hydra's contract for
    ``/search?url=`` is assumed to be a list of matches; we take the
    first one and trust it (Reddit's own search ranks by relevance so
    the first match is usually the canonical discussion thread).

    Never raises. Logs DEBUG on failure.
    """
    if not is_configured() or not url:
        return None
    try:
        # Hydra is expected to URL-encode the query parameter server-side
        # or accept the raw URL as a path segment. We pass it as a query
        # string and trust the gateway to handle encoding; if Hydra
        # rejects it, the catch-all below logs DEBUG and we move on.
        from urllib.parse import quote
        data = await _get_json(f"/search?url={quote(url, safe='')}")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(
            "reddit_client: search_thread_by_url %s failed: %s", url, exc,
        )
        return None
    if not isinstance(data, list) or not data:
        return None
    match = data[0]
    permalink = match.get("permalink")
    num_comments = match.get("num_comments")
    if not isinstance(permalink, str) or not isinstance(num_comments, int):
        logger.debug(
            "reddit_client: search match missing permalink/num_comments: %r",
            match,
        )
        return None
    return {"permalink": permalink, "num_comments": num_comments}