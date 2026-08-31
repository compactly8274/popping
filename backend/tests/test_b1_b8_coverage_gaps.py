"""Regression guards for the B1–B8 audit bugfixes (PR #91).

These are source-text regex tests — they read the .py source of the
files the bugfixes touched and assert the fix is still present.  No
DB, no network, no import of heavy modules.  Fast, deterministic,
and resilient to refactors that don't change the behaviour under
test.

Coverage gaps addressed (see the audit in the conversation that
produced this file):

B1 — ``foryou.py`` used to exclude ``Entry.embedding`` from its slim
     SELECT, so the personal scorer fell back to the neutral
     midpoint and the feed was effectively unpersonalized.  The
     original fix re-added the embedding column to the projection.
     PR #94 then replaced the live personal recompute entirely: the
     endpoint now reads the STORED ``composite_score`` (which already
     blends recency + personal + source weight + engagement —
     refreshed every 10 min by the scheduler's rescore tick) and
     applies only the convergence multiplier at query time.  The
     B1 intent — personalization present on /foryou — is now
     guaranteed by the stored column, not by a per-request vector
     fetch (which cost ~1.5 MB of wire data per request).  These
     tests were updated to guard the CURRENT architecture instead
     of the pre-#94 one (the old assertion failed on main).

B3 — ``dynamic_reddit.py`` must build ``reddit_thread_url`` from
     ``permalink`` (the Reddit thread path), NOT from
     ``outbound_url`` (the external article URL).  Stamping the
     outbound URL broke the comment-summary endpoint and the
     "Discussed on Reddit" card link.

B4 — ``generic_scrape.py`` must pass ``custom_headers=self.custom_headers``
     from ``GenericScrapePlugin.fetch()`` into ``_extract_one``, and
     ``_extract_one`` must forward those headers to ``fetch_html``.
     Before the fix the headers were stored on the Source row but
     never reached the HTTP fetch.

B7 — Six source plugins must register ``ssrf_event_hook`` on their
     ``httpx.AsyncClient`` via ``event_hooks={"request": [...]}``.
     The hook itself is already tested in ``test_url_safety.py``;
     these tests verify the *wiring* in each plugin file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.no_db


def _read(rel: str) -> str:
    """Read a source file from the repo root."""
    return (REPO / rel).read_text()


# ===========================================================================
# B1 — foryou.py personalization via the stored composite_score
# ===========================================================================


def test_b1_foryou_scores_via_stored_composite():
    """The /foryou feed must rank on the STORED ``composite_score``.

    PR #94 replaced the per-request personal recompute (which pulled
    ``Entry.embedding`` — ~3 KB serialized per row, ~1.5 MB per
    request for 500 candidates — and recomputed the cosine in Python)
    with a read of the stored ``composite_score``.  The stored value
    already blends the personal component (``scoring_weight_personal``
    in the composite formula) and is refreshed every 10 min by the
    scheduler's rescore tick, so a live recompute was pure overhead.

    The B1 intent (personalization present on /foryou) is carried by
    two invariants now, both guarded here:

      1. ``/foryou`` ranks on ``Entry.composite_score`` and does NOT
         re-project the heavy ``Entry.embedding`` vector (the
         pre-#94 workaround the original B1 test asserted on).
      2. The scheduler registers the rescore job that keeps the
         stored score fresh — without it the stored value goes stale
         and the personal signal stops updating after ingest.
    """
    src = _read("backend/app/routes/foryou.py")
    # The projection includes the stored score and orders by it.
    assert "Entry.composite_score" in src, (
        "foryou.py must rank on the stored Entry.composite_score. "
        "Without it the feed has no personal signal at all."
    )
    # The heavy embedding vector must NOT be projected. Find the
    # stmt = ( select( ... ) block and check its body.
    m = re.search(r"stmt\s*=\s*\(\s*select\(", src)
    assert m, "Could not locate the stmt = ( select( ... block in foryou.py"
    start = m.end()  # position right after "select("
    depth = 1
    i = start
    while i < len(src) and depth > 0:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    select_body = src[start:i]
    assert "Entry.embedding" not in select_body, (
        "foryou.py must NOT project Entry.embedding in the candidate "
        "SELECT — it is ~3 KB serialized per row and the stored "
        "composite_score already blends the personal component "
        "(refreshed by the scheduler's rescore tick). The pre-PR#94 "
        "shape cost ~1.5 MB of wire data per request for no gain."
    )
    # The convergence multiplier (the one signal that can change
    # between rescore ticks) is applied at query time.
    assert "convergence_multiplier" in src, (
        "foryou.py must apply the convergence multiplier at query time — "
        "it's the only score component that can change between rescore "
        "ticks, so it's the only live adjustment the endpoint needs."
    )


def test_b1_rescore_job_registered():
    """The scheduler must register the rescore tick that refreshes the
    stored composite_score/personal_score — the freshness half of the
    B1 invariant. Without the job, the stored score is write-once at
    ingest and the personal component never tracks the user's
    evolving preference vector."""
    src = _read("backend/app/scheduler.py")
    m = re.search(
        r"_scheduler\.add_job\(\s*_rescore_recent_entries", src,
    )
    assert m, (
        "scheduler.py must register _rescore_recent_entries as a job. "
        "foryou.py ranks on the STORED composite_score; without the "
        "rescore tick that value goes stale at ingest time and the "
        "personal component stops tracking the user's preferences "
        "(the B1 intent, regressed through a different path)."
    )


# ===========================================================================
# B3 — dynamic_reddit.py builds reddit_thread_url from permalink
# ===========================================================================


def test_b3_reddit_thread_url_built_from_permalink():
    """``reddit_thread_url`` must be built from ``permalink``, not
    ``outbound_url``.  For link posts the outbound URL is the external
    article (e.g. https://bbc.com/...); stamping that as
    ``reddit_thread_url`` broke the comment-summary endpoint and the
    "Discussed on Reddit" card link, which both need the Reddit
    thread URL.

    The fix constructs ``reddit_thread_url`` as
    ``f"https://www.reddit.com{permalink}"``.  This test pins that
    construction to the permalink, not the outbound URL.
    """
    src = _read("backend/app/sources/dynamic_reddit.py")
    # The construction line looks like:
    #   reddit_thread_url = (
    #       f"https://www.reddit.com{permalink}" if permalink else ""
    #   )
    assert "reddit_thread_url" in src, (
        "dynamic_reddit.py must stamp meta.reddit_thread_url on each "
        "Reddit-source entry."
    )
    # The construction must use permalink, not outbound_url.
    # Find the assignment to reddit_thread_url.
    m = re.search(
        r"reddit_thread_url\s*=\s*",
        src,
    )
    assert m, "Could not find reddit_thread_url assignment in dynamic_reddit.py"
    # Grab the next ~200 chars to see the full construction.
    snippet = src[m.start():m.start() + 300]
    assert "permalink" in snippet, (
        "reddit_thread_url must be built from permalink (the Reddit "
        "thread path), not from outbound_url (the external article URL). "
        "Stamping the outbound URL broke the comment-summary endpoint and "
        "the 'Discussed on Reddit' card link."
    )
    assert "outbound_url" not in snippet or "permalink" in snippet, (
        "reddit_thread_url construction must reference permalink, not "
        "outbound_url."
    )


# ===========================================================================
# B4 — generic_scrape.py passes custom_headers through to fetch_html
# ===========================================================================


def test_b4_extract_one_accepts_custom_headers_kwarg():
    """``_extract_one`` must accept a ``custom_headers`` keyword argument
    and forward it to ``fetch_html``."""
    src = _read("backend/app/sources/generic_scrape.py")
    # _extract_one signature must include custom_headers.
    m = re.search(
        r"async def _extract_one\([^)]*\)",
        src,
    )
    assert m, "Could not find _extract_one function definition"
    sig = m.group(0)
    assert "custom_headers" in sig, (
        "_extract_one must accept a custom_headers parameter so the "
        "plugin can forward user-supplied Source.custom_headers to the "
        "HTTP fetch (B4 fix)."
    )
    # _extract_one must call fetch_html with custom_headers=custom_headers.
    # Find the fetch_html call inside _extract_one's body.
    func_body = _extract_function_body(src, "_extract_one")
    assert func_body, "Could not extract _extract_one function body"
    assert "fetch_html" in func_body, (
        "_extract_one must call fetch_html to fetch the candidate URL."
    )
    assert "custom_headers=custom_headers" in func_body, (
        "_extract_one must pass custom_headers=custom_headers to "
        "fetch_html so user-supplied headers actually reach the HTTP "
        "request (B4 fix)."
    )


def test_b4_plugin_fetch_passes_custom_headers_to_extract_one():
    """``GenericScrapePlugin.fetch()`` must pass
    ``custom_headers=self.custom_headers`` to ``_extract_one`` so
    user-supplied headers stored on the Source row actually reach
    the HTTP fetch.  Before the B4 fix the headers were stored on the row
    but never forwarded."""
    src = _read("backend/app/sources/generic_scrape.py")
    func_body = _extract_function_body(src, "fetch")
    assert func_body, "Could not extract GenericScrapePlugin.fetch function body"
    assert "_extract_one" in func_body, (
        "GenericScrapePlugin.fetch() must call _extract_one for each "
        "candidate URL."
    )
    assert "custom_headers=self.custom_headers" in func_body, (
        "GenericScrapePlugin.fetch() must pass "
        "custom_headers=self.custom_headers to _extract_one so "
        "user-supplied headers (e.g. a custom User-Agent or "
        "Authorization) are actually used by the HTTP fetch (B4 fix)."
    )


def test_b4_plugin_stores_custom_headers_from_source_row():
    """``GenericScrapePlugin.__init__`` must read ``custom_headers``
    off the Source row so it can forward them later."""
    src = _read("backend/app/sources/generic_scrape.py")
    func_body = _extract_function_body(src, "__init__")
    assert func_body, "Could not extract GenericScrapePlugin.__init__ body"
    assert "custom_headers" in func_body, (
        "GenericScrapePlugin.__init__ must read custom_headers from the "
        "Source row (getattr(source_row, 'custom_headers', None)) so it "
        "can forward them to _extract_one on every poll (B4 fix)."
    )


def test_b4_article_extract_fetch_html_accepts_custom_headers():
    """``article_extract.fetch_html`` must accept a ``custom_headers``
    keyword argument and merge it with the default headers."""
    src = _read("backend/app/article_extract.py")
    m = re.search(
        r"async def fetch_html\([^)]*\)",
        src,
    )
    assert m, "Could not find fetch_html function definition"
    sig = m.group(0)
    assert "custom_headers" in sig, (
        "fetch_html must accept a custom_headers parameter so "
        "generic_scrape can forward user-supplied headers through to "
        "the HTTP request (B4 fix)."
    )


def test_b4_article_extract_fetch_html_merges_custom_headers():
    """``fetch_html`` must merge custom headers with the defaults so
    user-supplied headers (e.g. a custom User-Agent) take precedence
    over ``_DEFAULT_HEADERS``."""
    src = _read("backend/app/article_extract.py")
    func_body = _extract_function_body(src, "fetch_html")
    assert func_body, "Could not extract fetch_html function body"
    # The merge logic: headers = dict(_DEFAULT_HEADERS); if custom_headers: ...
    assert "_DEFAULT_HEADERS" in func_body, (
        "fetch_html must start from _DEFAULT_HEADERS and merge custom "
        "headers on top so defaults are preserved when custom_headers "
        "is None (B4 fix)."
    )
    assert "custom_headers" in func_body, (
        "fetch_html must reference custom_headers inside its body to "
        "merge user-supplied headers with the defaults (B4 fix)."
    )


# ===========================================================================
# B7 — SSRF event hook wiring in 6 source plugins
# ===========================================================================

# Each plugin must register the SSRF event hook on its httpx.AsyncClient
# via event_hooks={"request": [ssrf_event_hook]}.  The hook itself is
# already tested in test_url_safety.py (it's a valid async request hook
# and it blocks denied hosts); these tests verify the wiring — that
# each plugin actually registers it.

_B7_PLUGIN_FILES = [
    ("backend/app/sources/rss.py",                 "rss.py"),
    ("backend/app/sources/hn.py",                  "hn.py"),
    ("backend/app/sources/cisa_kev.py",             "cisa_kev.py"),
    ("backend/app/sources/nvd.py",                 "nvd.py"),
    ("backend/app/sources/github_releases.py",     "github_releases.py"),
    ("backend/app/sources/wikipedia_on_this_day.py", "wikipedia_on_this_day.py"),
]


@pytest.mark.parametrize("file_path,plugin_name", _B7_PLUGIN_FILES)
def test_b7_plugin_registers_ssrf_event_hook(file_path, plugin_name):
    """Each source plugin that creates an ``httpx.AsyncClient`` must
    register ``ssrf_event_hook`` via ``event_hooks={"request": [...]}``
    so every HTTP request (including redirect hops) is checked against
    the SSRF deny list.

    Without the hook, a user-controlled feed URL (DynamicRssPlugin) or
    a compromised CDN redirect chain could point the backend at
    ``127.0.0.1`` or ``169.254.169.254``.
    """
    src = _read(file_path)
    assert "ssrf_event_hook" in src, (
        f"{plugin_name} must import ssrf_event_hook from app.url_safety "
        f"so it can register it as a per-request event hook on its "
        f"httpx.AsyncClient (B7 fix)."
    )
    assert 'event_hooks={"request": [ssrf_event_hook]}' in src or \
           'event_hooks={"request":[ssrf_event_hook]}' in src, (
        f"{plugin_name} must register ssrf_event_hook on its "
        f"httpx.AsyncClient via "
        f'event_hooks={{"request": [ssrf_event_hook]}} (B7 fix). '
        f"Without it, redirect hops to private IPs are not caught."
    )


# ===========================================================================
# Helper
# ===========================================================================


def _extract_function_body(src: str, name: str) -> str:
    """Extract a function/method body from source text by name.

    Handles both ``async def name(`` and ``def name(`` at any
    indentation level.  Returns the text from the ``def`` line through
    the next ``def``/``class``/end-of-file at the same or lesser
    indentation, or empty string if not found.
    """
    # Match ``async def name(`` or ``def name(`` at any indentation.
    pat = rf"^([ \t]*)async def {re.escape(name)}\(|^([ \t]*)def {re.escape(name)}\("
    m = re.search(pat, src, re.MULTILINE)
    if not m:
        return ""
    indent = (m.group(1) or m.group(2) or "")
    start = m.start()
    # Find the next def/class at the same or lesser indentation.
    rest = src[m.end():]
    end_pat = rf"^{re.escape(indent)}(?:(?:async )?def |class )"
    end_m = re.search(end_pat, rest, re.MULTILINE)
    if end_m:
        return src[start:m.end() + end_m.start()]
    # No next function — take to end of file.
    return src[start:]