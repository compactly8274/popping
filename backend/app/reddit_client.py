"""Reddit client with two operational modes.

Popping can read Reddit in two ways:

  1. **Proxy mode** (recommended). When ``settings.reddit_hydra_url`` is
     set, calls go through a small Bun/Node/Python proxy on a VPS that
     fronts Reddit's JSON API. The proxy holds the contact-stamped
     User-Agent and rate-limits requests to a polite cadence, so the
     TrueNAS IP never talks to Reddit directly. This is the right
     mode for any deployment on a datacenter / residential IP that
     Reddit's anti-abuse flags.

  2. **Direct mode** (fallback). When ``settings.reddit_hydra_url`` is
     unset and the user hasn't disabled the feature via
     ``settings.reddit_direct_disabled == True``, calls go straight to
     ``https://www.reddit.com/...json`` with a per-process token
     bucket (2 req/s sustained, 4 burst) and a contact-stamped
     User-Agent. This is convenient for development or small personal
     deployments but Reddit's per-IP throttling will start returning
     429 / 403 within hours of polling cadence.

Both modes share the same response shape contract — a flat list of
post dicts, or ``[]`` / ``None`` on failure — so the per-subreddit
plugin and the cross-reference sweep don't care which path took the
call. ``fetch_subreddit`` / ``search_thread_by_url`` never raise; the
caller treats ``[]`` / ``None`` as "no entries this tick" and moves
on. The next scheduled tick retries.

Disabled state
--------------
``is_configured()`` returns True when either mode can produce a
response (proxy set, or direct not explicitly disabled). ``None`` of
those: both paths return ``[]`` / ``None`` and the scheduler logs
``reddit_client: disabled`` once at startup. A new ``is_disabled()``
helper exists for the few places that want to distinguish "off
because user said so" from "off because no work to do."
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("popping.reddit_client")

# Default per-stream timeout for Reddit calls. Listings are small
# JSON; the round-trip should be sub-200ms. 10s gives headroom for
# the user's VPS / Reddit being slow without blocking the
# scheduler's next job. The same value is used for both proxy and
# direct mode so a config-tweak in one place covers both.
_TIMEOUT = 10.0
# 2 MB cap on response bodies. A /r/python hot listing with 50
# items is ~30 KB; a /search?url= response is similar. The cap
# defends against a misbehaving / compromised upstream returning a
# multi-GB body. ``reddit_client`` enforces the cap on the actual
# streamed bytes, not just the advisory Content-Length header.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Rate-limiter parameters for direct mode. Same numbers as the
# popping-proxy (Bun) so the two paths have the same external
# behavior — a deployment that switches from direct to proxy (or
# vice versa) doesn't suddenly hit Reddit at a different cadence.
# Tunable via env in the future; constants today.
_DIRECT_RPS = 2.0
_DIRECT_BURST = 4.0

# Default User-Agent. Reddit's anti-abuse wants a contact string;
# without one the request gets 403 from a residential IP within
# minutes and from a datacenter IP immediately. ``REDDIT_USER_AGENT``
# overrides — set it to ``Popping/yourname (+url; contact: you@x)``
# for the best chance of staying un-throttled. If unset, the contact
# line falls back to the GitHub repo so a brand-new deploy at least
# has a string that looks like a real client.
_DEFAULT_USER_AGENT = "Popping/0.2 (+https://github.com/compactly8274/popping)"
_DEFAULT_USER_AGENT_WITH_CONTACT = (
    "Popping/0.2 (+https://github.com/compactly8274/popping; "
    "contact: see /admin for operator email)"
)

# Shared clients — one for proxy mode, one for direct mode. Both are
# ``None`` before ``init_client()`` and re-built on every lifespan
# cycle. ``_get_client()`` constructs a defensive one-off if a route
# fires before startup (e.g. during tests).
_proxy_client: Optional[httpx.AsyncClient] = None
_direct_client: Optional[httpx.AsyncClient] = None
# Token-bucket state for direct mode. ``_lock`` guards the bucket
# because ``fetch_subreddit`` and ``search_thread_by_url`` are both
# awaited from the scheduler and can interleave at the await points.
# A small, short-lived lock is fine — the critical section is just
# arithmetic, no I/O.
_bucket_tokens: float = _DIRECT_BURST
_bucket_last: float = time.monotonic()
_bucket_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True when at least one of proxy / direct mode can produce
    responses.

    A return of ``False`` means: no proxy URL set AND direct mode
    disabled. The scheduler and the per-subreddit plugin treat this
    as "feature off, skip silently." Returns True in both
    operational modes so existing call sites (which already check
    ``is_configured()``) keep working unchanged.
    """
    if settings.reddit_hydra_url:
        return True
    if not settings.reddit_direct_disabled:
        return True
    return False


def is_disabled() -> bool:
    """True when both modes are off (proxy unset AND direct disabled).

    Used by the per-subreddit plugin to decide whether to fire the
    one-shot "feature off" warning. Without this distinction the
    warning would fire in direct mode too, even though direct mode
    is doing useful work and just hasn't been told about a proxy.
    """
    return not bool(settings.reddit_hydra_url) and bool(
        settings.reddit_direct_disabled
    )


def _user_agent() -> str:
    """User-Agent string for Reddit calls (used in both modes — the
    proxy inherits it via the shared client, direct mode sends it
    directly).

    Order of precedence:
      1. ``REDDIT_USER_AGENT`` env var (operator override)
      2. The built-in default with a contact hint

    The contact hint exists to satisfy Reddit's anti-abuse rules
    without forcing every operator to set the env var on day one.
    Proxy-mode operators can override the UA too — useful if the
    proxy is shared across multiple Popping instances with
    different contact strings.
    """
    ua = os.environ.get("REDDIT_USER_AGENT", "").strip()
    if ua:
        return ua
    return _DEFAULT_USER_AGENT_WITH_CONTACT


# ---------------------------------------------------------------------------
# Init / teardown
# ---------------------------------------------------------------------------


def init_client() -> None:
    """Build the shared clients. Idempotent — a second call replaces
    the existing clients (and closes the old ones). Logs once which
    mode is active so an operator scanning the startup log sees the
    operational posture without having to ``printenv``.

    Three branches:
      - proxy URL set: build the proxy client; direct mode stays off
        even if ``reddit_direct_disabled`` is False (no point hitting
        Reddit twice).
      - direct mode enabled: build the direct client; reset the
        token bucket so a process restart starts at full capacity.
      - both off: log "disabled" and return. Both clients stay
        ``None`` and ``_get_client`` constructs one-offs if a stray
        route fires (it shouldn't, but defensive).
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
            headers={"User-Agent": _user_agent(), "Accept": "application/json"},
        )
        # Reset the bucket so the first burst of cross-ref requests
        # after a restart gets a clean slate rather than whatever
        # state the previous process left it in.
        _bucket_tokens = _DIRECT_BURST
        _bucket_last = time.monotonic()
        logger.info(
            "reddit_client: direct mode (no proxy; "
            "throttled to %.1f req/s, burst %.0f; "
            "set REDDIT_HYDRA_URL to route through a proxy)",
            _DIRECT_RPS, _DIRECT_BURST,
        )
        return

    logger.info("reddit_client: disabled (no proxy + REDDIT_DIRECT_DISABLED=1)")


async def close_client() -> None:
    """Tear down the shared clients. Called from FastAPI lifespan
    exit so connection pools don't leak across ``uvicorn --reload``
    cycles. Both clients closed if both were built (defensive —
    today only one is ever built)."""
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
    one is available.

    Token-bucket math: tokens refill at ``_DIRECT_RPS`` per second
    up to a cap of ``_DIRECT_BURST``. If the bucket has at least one
    token, decrement and return immediately. Otherwise compute the
    wait until the next token and ``asyncio.sleep`` for it. The
    lock guards both the read and the update so two concurrent
    fetches can't both observe ``tokens >= 1`` and double-spend.

    This is a non-rejecting limiter — we shape the traffic rather
    than refusing it. Reddit's own 429s are the back-pressure when
    the bucket isn't tight enough.
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
            # Not enough tokens — compute how long until one is and
            # sleep. We drop the lock before sleeping so other
            # coroutines that wake up first get the token instead of
            # us having to wake up and re-check.
            deficit = 1.0 - _bucket_tokens
            wait_s = deficit / _DIRECT_RPS
        await asyncio.sleep(wait_s)


# ---------------------------------------------------------------------------
# JSON fetcher
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    """Return whichever shared client is active. Constructs a
    defensive one-off if a route fires before lifespan startup
    (e.g. during tests). Proxy wins if both happen to be set."""
    if _proxy_client is not None:
        return _proxy_client
    if _direct_client is not None:
        return _direct_client
    # Both None — build a one-off for the right mode, but log a
    # warning. This is a hot-path anomaly; the lifespan should
    # always have run by the time scheduler jobs fire.
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
        headers={"User-Agent": _user_agent(), "Accept": "application/json"},
    )


async def _get_json(path: str) -> Any:
    """Stream a Reddit-or-proxy response into JSON. Enforces
    ``_MAX_RESPONSE_BYTES`` on the actual streamed bytes (not just
    the advisory Content-Length header). Returns the parsed JSON
    or raises ``ValueError`` if the body is too large / unparseable.
    Never raises ``httpx.HTTPError`` — caller catches
    ``(httpx.HTTPError, ValueError)``.

    Direct-mode calls take a rate-limit token first; proxy-mode
    calls don't (the proxy has its own limiter and adding one here
    would double-throttle the user's traffic).
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
    return json.loads(bytes(buf))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_subreddit(
    subreddit: str, listing: str = "hot", limit: int = 50
) -> list[dict]:
    """Fetch one subreddit's listing. Returns a flat list of post
    dicts (Reddit's ``data.children[]`` unwrapped) — the same shape
    whether the call went through a proxy or direct to Reddit, so
    ``app.sources.dynamic_reddit`` doesn't care which path was
    taken. Returns ``[]`` on any failure (proxy down, Reddit 429 /
    403, malformed JSON, response too large). Logs DEBUG so the
    failure is visible without being noisy.

    ``listing`` is one of ``"hot"``, ``"new"``, ``"top"``,
    ``"rising"``. The per-subreddit plugin defaults to ``"hot"``;
    future surfaces can pass others.
    """
    if is_disabled():
        return []
    try:
        data = await _get_json(f"/r/{subreddit}/{listing}.json?limit={limit}")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(
            "reddit_client: fetch_subreddit r/%s failed: %s", subreddit, exc,
        )
        return []
    if not isinstance(data, dict):
        logger.debug(
            "reddit_client: r/%s returned non-dict payload (type=%s)",
            subreddit, type(data).__name__,
        )
        return []
    children = (data.get("data") or {}).get("children") or []
    if not isinstance(children, list):
        logger.debug(
            "reddit_client: r/%s returned non-list children (type=%s)",
            subreddit, type(children).__name__,
        )
        return []
    out: list[dict] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        # Reddit's wrapped child has ``{"kind": "t3", "data": {...}}``;
        # the proxy may already have unwrapped it. Accept both.
        if "data" in child and isinstance(child["data"], dict):
            out.append(child["data"])
        else:
            out.append(child)
    return out


async def search_thread_by_url(url: str) -> Optional[dict]:
    """Look up a Reddit thread matching ``url``. Returns the first
    match as ``{"permalink": str, "num_comments": int}`` or
    ``None`` if no match / upstream unreachable.

    Used by the background cross-reference sweep to stamp
    ``reddit_thread_url`` onto existing entries. The proxy already
    returns a list of zero or one hit; direct mode returns
    Reddit's full listing envelope, which we unwrap here. The
    caller (``reddit_client.search_thread_by_url``'s only
    consumer, the cross-ref sweep) only cares about the first hit.

    Never raises. Logs DEBUG on failure.
    """
    if is_disabled() or not url:
        return None
    from urllib.parse import quote
    try:
        if _direct_client is not None and _proxy_client is None:
            # Direct mode: hit Reddit's search endpoint with the
            # ``url:`` operator and a single result. The proxy
            # does the same on its end and returns a list of hits;
            # in direct mode we get the raw listing and unwrap.
            data = await _get_json(
                f"/search.json?q=url%3A{quote(url, safe='')}"
                f"&limit=1&sort=relevance&restrict_sr=&type=link"
            )
        else:
            data = await _get_json(f"/search?url={quote(url, safe='')}")
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.debug(
            "reddit_client: search_thread_by_url %s failed: %s", url, exc,
        )
        return None
    if not isinstance(data, list):
        # Proxy returns a list. Direct returns a listing envelope
        # (a dict with ``data.children``). Unwrap the envelope
        # before taking the first child.
        if isinstance(data, dict):
            children = (data.get("data") or {}).get("children") or []
            if not isinstance(children, list) or not children:
                return None
            first = children[0]
            if not isinstance(first, dict):
                return None
            match = first.get("data") if isinstance(first.get("data"), dict) else first
        else:
            return None
    else:
        if not data:
            return None
        match = data[0]
    permalink = match.get("permalink") if isinstance(match, dict) else None
    num_comments = match.get("num_comments") if isinstance(match, dict) else None
    if not isinstance(permalink, str) or not isinstance(num_comments, int):
        logger.debug(
            "reddit_client: search match missing permalink/num_comments: %r",
            match,
        )
        return None
    return {"permalink": permalink, "num_comments": num_comments}
