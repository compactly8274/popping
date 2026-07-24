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

import logging

import httpx
import trafilatura

from app.url_safety import check_url_safe

logger = logging.getLogger("popping.article_extract")

# Article pages are just HTML — generous but bounded. 3 MB comfortably
# covers even a heavy, image-link-bloated news page; we only need the
# text, but there's no way to know how large the markup is before
# downloading it.
_MAX_HTML_BYTES = 3 * 1024 * 1024
_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=15.0)

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


async def _fetch_html(url: str) -> str | None:
    safe, reason = check_url_safe(url)
    if not safe:
        logger.info("article_extract: %s rejected by URL safety check (%s)", url, reason)
        return None
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Popping/0.2"}) as resp:
                if resp.status_code >= 400:
                    logger.info("article_extract: %s -> HTTP %s", url, resp.status_code)
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
                content_type = resp.headers.get("content-type", "")
                if content_type and "html" not in content_type.lower():
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
    except httpx.HTTPError as exc:
        logger.info("article_extract: %s fetch failed: %s", url, exc)
        return None
    if not body:
        return None
    return body.decode("utf-8", errors="replace")


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


async def fetch_article_text(url: str) -> str | None:
    """Fetch ``url`` and return its extracted article text, or None on
    any failure (network, SSRF rejection, empty body, extraction
    found nothing usable). Never raises — callers treat None as "fall
    back to the feed's own summary"."""
    html = await _fetch_html(url)
    if html is None:
        return None
    return extract_text(html)
