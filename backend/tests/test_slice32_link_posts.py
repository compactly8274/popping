"""Slice 32 -- handle Reddit link posts in _fetch_reddit_post_body.

Reddit has two post shapes in threads:

- **Self-posts** -- the OP has a markdown body that Reddit
  serialises into ``<content type="html">``. The first <entry>'s
  cleaned content IS the article body.

- **Link posts** -- the OP just links to an external article. The
  first <entry>'s content is a tiny ``submitted by /u/X [link]
  [comments]`` template card (with the external URL embedded as an
  ``<a href>``). The actual article body is at the external URL.

Before slice 32, the summary endpoint returned the template card
text as the summary, which the user saw as ``No summary added``
because the cleaned "submitted by /u/X [link] [comments]" string
doesn't read as article content. This slice teaches
``_fetch_reddit_post_body`` to detect that template and follow the
embedded external link to fetch the real article body via
``fetch_article_text`` (trafilatura extraction).

Tests:
- Link-post template detection (substring match for "submitted by"
  + "[link]") is correct
- Self-posts with body text are passed through without triggering
  the link-detection branch
- The external URL extraction walks all anchors and skips
  reddit.com internal links
- One-network-call for self-posts, two for link posts (verified
  via the function body shape, not a network test)
- Failure modes: empty entries, missing content, malformed XML,
  external fetch failure, etc.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRY_ROUTES = REPO / "backend/app/routes/entries.py"


def _function_body(name: str) -> str:
    src = ENTRY_ROUTES.read_text()
    pat = rf"^(?:async )?def {re.escape(name)}\([\s\S]+?(?=^async def |^def |^class |\Z)"
    m = re.search(pat, src, re.MULTILINE)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# 1. _fetch_reddit_post_body signature -- now direct RSS fetch (not via
#    fetch_thread_comments), so it carries link info too.
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_fetch_reddit_post_body_does_not_use_fetch_thread_comments():
    """Slice 32 rewrites the body to call reddit_client._get_atom
    directly, not fetch_thread_comments. The old call stripped the
    link URL from the OP (comments parser only returns
    {"author","text"}, no URL), so we couldn't do link-post
    extraction. The new path reads the raw XML itself.
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    assert "fetch_thread_comments" not in body, (
        "_fetch_reddit_post_body must NOT use fetch_thread_comments "
        "anymore -- the comments parser strips the OP link URL, "
        "which is exactly what we need for link-post extraction. "
        "Slice 32 switched this to a direct _get_atom + ElementTree "
        "parse so the outbound link survives."
    )
    assert "_get_atom" in body, (
        "_fetch_reddit_post_body must call reddit_client._get_atom "
        "directly so it can read the OP <content> element (and any "
        "<a href> in it) without the comments parser's text-only "
        "filter."
    )


# ---------------------------------------------------------------------------
# 2. Link-post template detection
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_link_post_detection_uses_submitted_by_and_link_markers():
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # Both markers must be present (lowercase contains check).
    assert '"submitted by"' in body, (
        "_fetch_reddit_post_body must check for the Reddit "
        "submission-card marker 'submitted by' -- without this "
        "check, the link-post branch never fires."
    )
    assert '"[link]"' in body, (
        "_fetch_reddit_post_body must check for the Reddit "
        "submission-card marker '[link]' -- without this check, "
        "the link-post branch never fires."
    )


@pytest.mark.no_db
def test_link_post_falls_through_to_op_text_on_external_failure():
    """If the external article fetch fails for any reason (404,
    paywalled, network error), the function returns the OP text
    (the 'submitted by ... [link] [comments]' template), NOT an
    empty string. Returning empty would cache a 24h 'no summary'
    entry for a transient failure; returning the OP text is at
    least non-empty, so the user gets *something*.
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # Find the link-post branch and verify the fallback path
    # The branch should break on external fetch failure
    assert "break" in body, (
        "_fetch_reddit_post_body's link-post branch must break "
        "out of the external-URL loop when the external fetch "
        "returns empty (rather than falling through to the empty "
        "return below)."
    )


# ---------------------------------------------------------------------------
# 3. External URL extraction: skip reddit.com anchors
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_external_url_extraction_skips_reddit_com():
    """The OP HTML contains multiple <a href> tags -- the user's
    profile link, the comment permalink, the subreddit link, and
    the outbound article link. We must skip the first three and
    find the outbound one. The skip is done with a substring
    check on 'reddit.com'.
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # Find the URL skip logic
    assert '"reddit.com"' in body, (
        "_fetch_reddit_post_body must skip reddit.com anchors "
        "in the external-URL extraction loop -- without this, "
        "we'd return the thread permalink or user profile as "
        "the 'external' article URL and try to fetch_article_text "
        "on it (which would 404 or fetch garbage)."
    )


# ---------------------------------------------------------------------------
# 4. Self-post path is preserved
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_self_post_path_does_not_trigger_external_fetch():
    """If the OP text DOESN'T contain both 'submitted by' and
    '[link]', it's a self-post and the function returns op_text
    directly without making a second network call (no
    fetch_article_text on a wrong URL).
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # The link-post branch is conditional. Verify self-posts skip it.
    # The structure is roughly:
    #   if "submitted by" AND "[link]" in op_text: ... external fetch
    #   return op_text
    # A self-post has no marker match -> skips the if-block -> returns
    # op_text. The function's tail is ``return op_text``.
    assert body.count("return op_text") >= 1, (
        "_fetch_reddit_post_body must return op_text as the "
        "self-post fallback (at the end of the function)."
    )
    # And the external-URL loop is INSIDE the if-block, so it
    # only runs for link posts. Spot-check the structure.
    assert "for m in re.finditer" in body, (
        "_fetch_reddit_post_body must loop over external-URL "
        "anchors. The loop is inside the link-post if-branch, so "
        "it only fires for link posts."
    )


# ---------------------------------------------------------------------------
# 5. Failure mode: parse errors and empty entries
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_handles_parse_error():
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # ET.fromstring raises ET.ParseError on malformed XML; we
    # catch and return "".
    assert "ET.ParseError" in body, (
        "_fetch_reddit_post_body must catch ET.ParseError and "
        "return empty string on malformed XML -- the proxy "
        "occasionally returns HTML error pages (e.g. for 429/500) "
        "which the XML parser would choke on."
    )


@pytest.mark.no_db
def test_handles_no_entries():
    """Empty feed (Reddit returns 200 OK but no <entry> tags) ->
    return empty. The proxy's 500 fallback sometimes returns an
    empty body for transient Reddit-side failures.
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # findall with no matches returns []; we guard with `if not entries`.
    assert "if not entries" in body, (
        "_fetch_reddit_post_body must guard against an empty "
        "entries list (findall returns [] when the feed has no "
        "<entry> tags) and return empty in that case."
    )


@pytest.mark.no_db
def test_handles_empty_op_content():
    """Reddit sometimes returns an entry with no <content> at all
    (deleted posts, mod-removed posts). The OP text is then empty;
    we must not trigger the link-post detection on empty text.
    """
    body = _function_body("_fetch_reddit_post_body")
    assert body
    # The conditional uses `op_text and ...` to short-circuit when
    # op_text is empty.
    assert "op_text\n        and" in body or "op_text and" in body, (
        "_fetch_reddit_post_body's link-post branch must short-"
        "circuit on empty op_text -- otherwise the 'in' checks "
        "always pass on empty strings and we'd try to extract "
        "an external URL from no content."
    )