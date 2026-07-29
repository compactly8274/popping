"""Slice 31 — Reddit summary fixes.

Two related bugs that combine to make the Reddit summary
experience broken:

1. **Article summary never works for Reddit-source entries.**
   ``fetch_article_text`` returns empty for every ``reddit.com`` URL
   because Reddit is a client-rendered SPA (the post body never
   lands in the initial HTML payload). The fallback to the feed's
   own blurb also returns empty because the dynamic_reddit source
   parser stores ``meta.summary = ""`` (Reddit listings don't ship
   a blurb field). The user sees "no summary available".

2. **Comment summary always returns ``available: false`` for
   Reddit-source entries.** The endpoint only reads
   ``meta.reddit_thread_url`` which is never stamped for
   Reddit-source entries (the cross-reference sweep explicitly
   skips them because their URL IS the thread). Combined with
   the proxy 404'ing on thread URLs (the proxy only relays
   listing + search endpoints), the user can't get a comment
   summary either.

Fixes:

A. ``entry_summary_endpoint`` detects Reddit URLs and uses the
   thread's ``.rss`` (first ``<entry>`` is the post itself) as the
   article body for the LLM summarization. Without an LLM, falls
   through to the post body directly (truncated to the card's
   char cap).

B. ``entry_reddit_comment_summary_endpoint`` falls back to
   ``row.url`` when ``meta.reddit_thread_url`` is missing AND
   ``row.url`` is a Reddit URL — the entry URL IS the thread URL
   for Reddit-source entries, and pre-slice-31 DB rows don't have
   the field stamped.

C. ``dynamic_reddit`` source now stamps ``meta.reddit_thread_url``
   on every ingest — so the canonical field is populated for new
   Reddit-source entries.

D. ``fetch_thread_comments`` falls back to direct-from-Reddit
   when the proxy returns 404 (the proxy only relays listing +
   search endpoints, not thread permalinks). The polite rate-limit
   bucket still applies so a runaway tap can't burn Reddit trust.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRY_ROUTES = REPO / "backend/app/routes/entries.py"
REDDIT_CLIENT = REPO / "backend/app/reddit_client.py"
DYNAMIC_REDDIT = REPO / "backend/app/sources/dynamic_reddit.py"


def _function_body(src: str, name: str) -> str:
    """Extract the body of a top-level function ``name`` from ``src``.

    Anchored on the ``def name(`` or ``async def name(`` line; matches
    up to the next top-level ``def``/``async def``/``class``/EOF.
    Returns "" if the function isn't found.
    """
    pat = rf"^(?:async )?def {re.escape(name)}\([\s\S]+?(?=^def |^async def |^class |\Z)"
    m = re.search(pat, src, re.MULTILINE)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# 1. _is_reddit_url helper
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_is_reddit_url_helper_exists():
    assert _function_body(ENTRY_ROUTES.read_text(), "_is_reddit_url"), (
        "routes/entries.py must define ``_is_reddit_url`` — the Reddit "
        "summary path needs a hostname check to route Reddit URLs into "
        "the .rss-based fetch instead of the SPA-rendering fetch."
    )


@pytest.mark.no_db
def test_is_reddit_url_matches_reddit_com_and_www_reddit_com():
    body = _function_body(ENTRY_ROUTES.read_text(), "_is_reddit_url")
    assert body, "Couldn't extract _is_reddit_url body"
    assert '"reddit.com"' in body, "_is_reddit_url must match reddit.com"
    assert '"www.reddit.com"' in body, (
        "_is_reddit_url must match www.reddit.com — the cross-ref "
        "sweep and the comment-summary endpoint both store the www "
        "variant in the meta blob."
    )


@pytest.mark.no_db
def test_is_reddit_url_does_not_match_old_reddit_com():
    """old.reddit.com is the no-app-prompt variant opened by the
    user. The article-fetch path shouldn't fire on it.
    """
    body = _function_body(ENTRY_ROUTES.read_text(), "_is_reddit_url")
    assert body
    assert '"old.reddit.com"' not in body, (
        "_is_reddit_url must NOT match old.reddit.com — that's a "
        "different routing case (the user already navigated there via "
        "the safeExternalUrl rewrite; the .rss-based fetch is for the "
        "modern SPA variant)."
    )


@pytest.mark.no_db
def test_is_reddit_url_rejects_empty_or_invalid_input():
    body = _function_body(ENTRY_ROUTES.read_text(), "_is_reddit_url")
    assert body
    assert "isinstance" in body, (
        "_is_reddit_url must guard against non-string input."
    )
    assert "return False" in body, (
        "_is_reddit_url must return False on bad input (not raise) — "
        "a raise would 500 the request."
    )


# ---------------------------------------------------------------------------
# 2. _fetch_reddit_post_body helper
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_fetch_reddit_post_body_helper_exists():
    src = ENTRY_ROUTES.read_text()
    assert re.search(r"async def _fetch_reddit_post_body\(", src), (
        "routes/entries.py must define ``_fetch_reddit_post_body``."
    )


@pytest.mark.no_db
def test_fetch_reddit_post_body_uses_thread_comments_or_direct_atom():
    """The helper must read the thread's .rss in some way (via
    fetch_thread_comments OR a direct _get_atom call). The thread's
    .rss first entry is the post body; the rest are comments.
    Reusing the existing Reddit .rss fetch path (or, in slice 32,
    calling _get_atom directly) means a single HTTP call serves
    both the comment-summary endpoint AND the article-summary
    endpoint on the same thread. This is true either way --
    direct _get_atom still hits the same .rss URL, just doesn't
    share the parsed comments cache.
    """
    body = _function_body(ENTRY_ROUTES.read_text(), "_fetch_reddit_post_body")
    assert body, "Couldn't extract _fetch_reddit_post_body body"
    # Either path is correct -- slice 31 used fetch_thread_comments,
    # slice 32 switched to direct _get_atom so link-post extraction
    # has access to the <a href> in the OP <content>. Both share
    # the same Reddit .rss underlying call.
    assert "fetch_thread_comments" in body or "_get_atom" in body, (
        "_fetch_reddit_post_body must read the Reddit thread's "
        ".rss feed -- either via fetch_thread_comments (slice 31) "
        "or a direct _get_atom call (slice 32). The Reddit .rss "
        "feed is the only reliable source of the OP body, since "
        "reddit.com main site is a client-rendered SPA. "
        "fetch_article_text returns empty for every reddit.com URL."
    )


@pytest.mark.no_db
def test_fetch_reddit_post_body_returns_empty_on_failure():
    body = _function_body(ENTRY_ROUTES.read_text(), "_fetch_reddit_post_body")
    assert body
    assert 'return ""' in body, (
        "_fetch_reddit_post_body must return empty string on failure "
        "so the summary endpoint's ``if post_body:`` gate cleanly "
        "falls through to the feed-blurb fallback."
    )


# ---------------------------------------------------------------------------
# 3. Summary endpoint uses the Reddit path
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_summary_endpoint_calls_reddit_helper_for_reddit_urls():
    body = _function_body(ENTRY_ROUTES.read_text(), "entry_summary_endpoint")
    assert body, "Couldn't extract entry_summary_endpoint body"
    assert "if _is_reddit_url(row.url)" in body, (
        "entry_summary_endpoint must gate on _is_reddit_url(row.url) — "
        "this is the routing decision that puts Reddit URLs into the "
        ".rss-based fetch path."
    )
    assert "_fetch_reddit_post_body(row.url)" in body, (
        "entry_summary_endpoint must call _fetch_reddit_post_body "
        "for Reddit URLs to get the post body."
    )


@pytest.mark.no_db
def test_summary_endpoint_uses_post_body_direct_when_no_llm():
    body = _function_body(ENTRY_ROUTES.read_text(), "entry_summary_endpoint")
    assert body
    assert "final = post_body" in body, (
        "entry_summary_endpoint must use the Reddit post body as the "
        "summary when the LLM call fails / isn't configured."
    )


# ---------------------------------------------------------------------------
# 4. Comment summary falls back to row.url
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_comment_summary_endpoint_falls_back_to_row_url():
    body = _function_body(ENTRY_ROUTES.read_text(), "entry_reddit_comment_summary_endpoint")
    assert body, "Couldn't extract entry_reddit_comment_summary_endpoint body"
    assert re.search(
        r"_is_reddit_url\(row\.url\).*?thread_url\s*=\s*row\.url",
        body,
        re.DOTALL,
    ), (
        "entry_reddit_comment_summary_endpoint must fall back to "
        "row.url when meta.reddit_thread_url is missing AND row.url "
        "is a Reddit URL. This handles pre-slice-31 DB rows that "
        "don't have the field stamped."
    )


# ---------------------------------------------------------------------------
# 5. dynamic_reddit source stamps reddit_thread_url
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_dynamic_reddit_stamps_reddit_thread_url():
    src = DYNAMIC_REDDIT.read_text()
    assert '"reddit_thread_url"' in src, (
        "dynamic_reddit source must stamp meta.reddit_thread_url on "
        "every Reddit-source entry — the comment-summary endpoint "
        "and the 'Discussed on Reddit' card link both read this "
        "field, and without it, Reddit-source entries are stuck "
        "with available: false forever (the cross-reference sweep "
        "explicitly skips Reddit-source entries because their URL "
        "IS the thread)."
    )


# ---------------------------------------------------------------------------
# 6. fetch_thread_comments proxy 404 fallback
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_thread_proxy_404_sentinel_exists():
    src = REDDIT_CLIENT.read_text()
    assert re.search(r"class _ThreadProxy404\(Exception\)\s*:\s*\n\s*pass", src), (
        "reddit_client.py must define ``_ThreadProxy404(Exception)`` "
        "as a sentinel exception for the proxy-404 control flow."
    )


@pytest.mark.no_db
def test_thread_proxy_404_raised_on_404_from_proxy():
    src = REDDIT_CLIENT.read_text()
    assert re.search(
        r"resp\.status_code\s*==\s*404\s+and\s+_proxy_client\s+is\s+not\s+None",
        src,
    ), (
        "fetch_thread_comments must only treat 404 as 'proxy doesn't "
        "support this URL shape' when _proxy_client is set."
    )


@pytest.mark.no_db
def test_thread_proxy_404_caught_and_retried_direct():
    src = REDDIT_CLIENT.read_text()
    # The except clause and the return may wrap across lines, so
    # allow whitespace in between (the ``[^\n]*`` in the earlier
    # version didn't tolerate that).
    assert re.search(
        r"except _ThreadProxy404:[\s\S]*?return\s+await\s+_fetch_thread_comments_direct",
        src,
    ), (
        "fetch_thread_comments must catch _ThreadProxy404 and retry "
        "via _fetch_thread_comments_direct — the whole point of the "
        "fallback is to bypass the proxy and hit Reddit directly when "
        "the proxy 404s on a thread URL."
    )


@pytest.mark.no_db
def test_fetch_thread_comments_direct_function_exists():
    src = REDDIT_CLIENT.read_text()
    assert re.search(
        r"async def _fetch_thread_comments_direct\(", src,
    ), (
        "reddit_client.py must define ``_fetch_thread_comments_direct`` "
        "— the proxy-404 retry path."
    )


@pytest.mark.no_db
def test_fetch_thread_comments_direct_takes_rate_limit_token():
    body = _function_body(REDDIT_CLIENT.read_text(), "_fetch_thread_comments_direct")
    assert body, "Couldn't extract _fetch_thread_comments_direct body"
    assert "_try_take_token" in body, (
        "_fetch_thread_comments_direct must take a rate-limit token "
        "before opening the socket — same gate as the rest of the "
        "direct-mode Reddit calls."
    )


@pytest.mark.no_db
def test_fetch_thread_comments_direct_uses_proper_user_agent():
    body = _function_body(REDDIT_CLIENT.read_text(), "_fetch_thread_comments_direct")
    assert body
    assert "_user_agent()" in body, (
        "_fetch_thread_comments_direct must use the configured "
        "User-Agent (via _user_agent()) — Reddit blocks generic UAs "
        "within hours of polling cadence."
    )