"""On-demand article full-text fetch + extraction.

Feed summaries are short (see ``routes/entries.py``'s ``_clean_summary``
— usually the feed's own 1-2 sentence teaser) and ``Entry.body_text``
is never populated anywhere in this schema today, so there is no
stored full-text anywhere to summarize. This module fetches the
entry's own article page on demand (triggered by the user expanding a
card — see ``app.article_summary``) and extracts just the readable
article body via ``trafilatura``, discarding nav/ads/related-links/
comments noise.

Same SSRF-guard shape as ``app.podcast_transcript.fetch_transcript_text``
and ``app.assets._download``: check the URL before the first request,
recheck the final URL after redirects (the entry-time check alone
misses a ``public.example.com -> 127.0.0.1`` redirect hop).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import zlib
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx
import trafilatura

from app.config import settings
from app.url_safety import check_url_safe, resolve_addresses, ssrf_event_hook

logger = logging.getLogger("popping.article_extract")

# Article pages are just HTML — generous but bounded. 3 MB comfortably
# covers even a heavy, image-link-bloated news page; we only need the
# text, but there's no way to know how large the markup is before
# downloading it.
_MAX_HTML_BYTES = 3 * 1024 * 1024
# Tap-and-wait UI affordance (see app.article_summary._LLM_CALL_TIMEOUT_S)
# — a slow article host shouldn't eat a big chunk of the interactive
# budget before the LLM step even starts.
_FETCH_TIMEOUT = httpx.Timeout(connect=6.0, read=10.0, write=10.0, pool=10.0)

# Article text budget fed to the LLM (app.article_summary). A typical
# news article is 500-1500 words (~3-9k characters); this cap covers
# essentially all of them without needing a chunking pass, at the cost
# of only the opening portion of unusually long longform pieces
# feeding the summary — the same trade-off
# ``podcast_transcript._TRANSCRIPT_CHAR_BUDGET`` makes for transcripts.
_ARTICLE_CHAR_BUDGET = 12_000

# Below this, extraction is treated as having failed — a stray nav
# fragment or paywall teaser isn't worth summarizing over the feed's
# own blurb, which is usually more substantive than a failed
# extraction's leftovers.
_MIN_USABLE_CHARS = 200

# Default User-Agent for all fetches. Callers can override via the
# ``custom_headers`` parameter to ``fetch_html`` (B4 fix).
_DEFAULT_HEADERS = {"User-Agent": settings.user_agent}

# Statuses that mean the site refused us, not that the page is gone:
# 401, 402 and 403 (``scheduler._classify_error`` records them as
# ``[blocked]``, whose fix is Read as Safari) and 429 (``[http error]``,
# whose fix is Check now).
REFUSED_STATUSES = frozenset({401, 402, 403, 429})

# Transport failures ``fetch_html_checked`` passes on: connect errors,
# timeouts of every stage, dropped connections, redirect loops. The
# scheduler records them as ``[network]``. The SSRF hook's plain
# ``httpx.RequestError`` is not one of them: a redirect to a denied
# host still reads as no document.
NETWORK_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.TransportError,
    httpx.TooManyRedirects,
)


class FetchRefusedError(httpx.HTTPStatusError):
    """The site answered 401, 402, 403 or 429 (``REFUSED_STATUSES``).

    An ``httpx.HTTPStatusError``, so ``scheduler._classify_error``
    records it from its status code like any other HTTP error, and the
    Test route's ``except httpx.HTTPStatusError`` reads it the same
    way."""


def unresolved_host_error(url: str) -> httpx.ConnectError | None:
    """A network error for ``url`` when its host is a name that resolves
    to nothing, else None.

    ``check_url_safe`` refuses both a host that doesn't resolve and one
    that resolves into a denied range, and the two mean different
    things to a website feed: a lapsed domain or a resolver that is down
    is a host that doesn't answer (``[network]``, which backs off and
    can stop the feed), while a denied address is not a site we read.
    Only the first gets an error here; an IP literal never does.
    ``url_safety`` stays as it is: this asks the resolver once more,
    only after the safety check has already failed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if resolve_addresses(host):
        return None
    return httpx.ConnectError(f"{host} did not resolve", request=httpx.Request("GET", url))


def most_telling_error(errors: Iterable[BaseException]) -> BaseException | None:
    """The failure that best explains why a website gave us nothing,
    the first of its rank: a 401, 402 or 403 refusal, then a 429, then
    any other HTTP status (the user's own page or sitemap is missing
    or failing), then a network error, then anything else. None for no
    errors.

    A refusal comes first because its fix (Read as Safari) is the only
    one the user can act on, and a WAF that refuses one request usually
    refuses the next with a different code."""

    def rank(exc: BaseException) -> int:
        if isinstance(exc, FetchRefusedError):
            return 0 if exc.response.status_code != 429 else 1
        if isinstance(exc, httpx.HTTPStatusError):
            return 2
        if isinstance(exc, NETWORK_ERRORS) or isinstance(exc, asyncio.TimeoutError):
            return 3
        return 4

    best: BaseException | None = None
    for exc in errors:
        if best is None or rank(exc) < rank(best):
            best = exc
    return best


async def fetch_html(
    url: str,
    custom_headers: dict | None = None,
    *,
    content_types: tuple[str, ...] | None = ("html",),
    return_final_url: bool = False,
    raise_errors: bool = False,
    raise_for_status: bool = False,
) -> str | tuple[str, str] | None:
    """SSRF-guarded HTML fetch, public so other callers that need raw
    HTML (rather than just extracted article text) can reuse the same
    guarded fetch instead of duplicating it — see
    ``app.sources.generic_scrape``, which needs the title trafilatura's
    metadata extraction gives it, not just the body text
    ``extract_text`` below returns.

    Returns the decoded body, or None when there is nothing usable
    (refused by the URL safety check, an HTTP error, a network error, a
    content type outside ``content_types``, an empty body). With
    ``raise_errors`` (``fetch_html_checked``), a host that doesn't
    resolve raises ``httpx.ConnectError`` instead
    (``unresolved_host_error``).

    Parameters
    ----------
    url
        The URL to fetch.
    custom_headers
        Optional dict of HTTP headers to merge with (or override)
        the default headers. ``User-Agent`` and other keys supplied
        here take precedence over the defaults. ``None`` (the
        default) uses only the default headers. This is the path
        that ``GenericScrapePlugin`` uses to pass user-supplied
        ``Source.custom_headers`` through to the fetch (B4 fix).
    content_types
        Substrings, one of which the response Content-Type must contain.
        ``None`` accepts any type (sitemap discovery fetches XML, plain
        text and gzip).
    return_final_url
        Return ``(body, final_url)`` instead of the body, where
        ``final_url`` is where the redirects ended (``score_page``
        counts a redirect to another path against a page).
    raise_errors, raise_for_status
        Set by ``fetch_html_checked``; see there.
    """
    safe, reason = check_url_safe(url)
    if not safe:
        logger.info("article_extract: %s rejected by URL safety check (%s)", url, reason)
        if raise_errors:
            unresolved = unresolved_host_error(url)
            if unresolved is not None:
                raise unresolved
        return None
    # Merge default headers with custom headers. Custom headers take
    # precedence (e.g. a user-supplied User-Agent override for sites
    # that block default UAs). ``None`` values in custom_headers are
    # skipped so a caller can't accidentally null out a required header.
    headers = dict(_DEFAULT_HEADERS)
    if custom_headers:
        for k, v in custom_headers.items():
            if v is not None:
                headers[k] = v
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
            # Per-hop SSRF guard: fires before every request
            # httpx sends, including the redirects
            # ``follow_redirects=True`` walks. Catches a
            # ``public.example.com → 127.0.0.1/admin`` chain at
            # the first hop. See ``app.url_safety.ssrf_event_hook``
            # for the policy. The post-response final-URL
            # check below is the safety net for the LAST hop;
            # the hook is the per-hop guard.
            event_hooks={"request": [ssrf_event_hook]},
        ) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    logger.info("article_extract: %s -> HTTP %s", url, resp.status_code)
                    if raise_errors and resp.status_code in REFUSED_STATUSES:
                        raise FetchRefusedError(
                            f"HTTP {resp.status_code} for {url}",
                            request=resp.request,
                            response=resp,
                        )
                    if raise_errors and raise_for_status:
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code} for {url}",
                            request=resp.request,
                            response=resp,
                        )
                    return None
                final_url = str(resp.url)
                if final_url != url:
                    final_safe, final_reason = check_url_safe(final_url)
                    if not final_safe:
                        logger.info(
                            "article_extract: %s redirected to denied host %s (%s)",
                            url, final_url, final_reason,
                        )
                        return None
                content_type = resp.headers.get("content-type", "").lower()
                if content_types and content_type and not any(
                    t in content_type for t in content_types
                ):
                    # PDFs, direct media links, etc. — not something
                    # trafilatura can do anything useful with.
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(chunk_size=32 * 1024):
                    total += len(chunk)
                    if total > _MAX_HTML_BYTES:
                        logger.info("article_extract: %s exceeded %d bytes — truncating", url, _MAX_HTML_BYTES)
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
    except httpx.HTTPStatusError:
        # Raised above, only when the caller asked for it.
        raise
    except httpx.HTTPError as exc:
        logger.info("article_extract: %s fetch failed: %s", url, exc)
        if raise_errors and isinstance(exc, NETWORK_ERRORS):
            raise
        return None
    if not body:
        return None
    if body[:2] == b"\x1f\x8b":  # gzip, e.g. sitemap.xml.gz
        # Bounded output so a small compressed body can't expand
        # without limit.
        try:
            body = zlib.decompressobj(wbits=31).decompress(body, _MAX_HTML_BYTES)
        except zlib.error:
            return None
    text = body.decode("utf-8", errors="replace")
    return (text, final_url) if return_final_url else text


async def fetch_html_checked(
    url: str,
    custom_headers: dict | None = None,
    *,
    content_types: tuple[str, ...] | None = ("html",),
    return_final_url: bool = False,
    raise_for_status: bool = False,
) -> str | tuple[str, str] | None:
    """``fetch_html`` for callers that need to know why a fetch gave
    nothing (website feeds: ``app.sources.generic_scrape`` and the
    sitemap crawl in ``app.feed_autodiscovery``).

    - 401, 402, 403 and 429 raise ``FetchRefusedError``, so a site
      that refuses us reads as ``[blocked]`` (or ``[http error]`` for a
      rate limit), not as a site with no articles.
    - Connect errors, timeouts and other transport failures
      (``NETWORK_ERRORS``) raise as the httpx error (``[network]``), and
      so does a host that doesn't resolve (``httpx.ConnectError``).
    - Any other 4xx or 5xx raises ``httpx.HTTPStatusError`` only with
      ``raise_for_status``, which callers set for the user's own
      listing page or sitemap. Otherwise it returns None, as
      ``fetch_html`` does: a missing robots.txt or a guessed sitemap
      path that 404s is routine, and the crawl moves on.
    - A host in a denied range (the URL safety check), a content type
      outside ``content_types`` and an empty body still return None.
    """
    return await fetch_html(
        url,
        custom_headers,
        content_types=content_types,
        return_final_url=return_final_url,
        raise_errors=True,
        raise_for_status=raise_for_status,
    )


def extract_text(html: str) -> str | None:
    """Pure extraction step, split out from the fetch so it's testable
    against a fixture string with no network involved. Returns None
    when trafilatura finds nothing usable (or the result is too short
    to trust — see ``_MIN_USABLE_CHARS``)."""
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text:
        return None
    text = text.strip()
    if len(text) < _MIN_USABLE_CHARS:
        return None
    return text[:_ARTICLE_CHAR_BUDGET]


async def fetch_article_text(url: str, custom_headers: dict | None = None) -> str | None:
    """Fetch ``url`` and return its extracted article text, or None on
    any failure (network, SSRF rejection, empty body, extraction
    found nothing usable). Never raises — callers treat None as "fall
    back to the feed's own summary"."""
    html = await fetch_html(url, custom_headers=custom_headers)
    if html is None:
        return None
    return extract_text(html)