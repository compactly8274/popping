"""Slice 30 — safeExternalUrl() rewrites reddit.com to old.reddit.com.

Reddit's main site (``reddit.com`` / ``www.reddit.com``) shows a
full-screen "Get the App" modal the first time a thread is opened
from a mobile browser — including the iOS PWA popout window, where
it traps the user (no back button, no close, no escape without
force-quitting the app). ``old.reddit.com`` is the legacy desktop
site, which never shows that modal.

The function is a one-liner host-rewrite, but it's worth pinning
the behavior with tests so a future refactor doesn't accidentally
drop the rewrite (e.g. by inlining it into each call site) and
re-introduce the iOS-PWA-trap.

This test file guards:
- ``reddit.com`` and ``www.reddit.com`` are rewritten to ``old.reddit.com``
- Path, query string, fragment, and port are preserved
- Non-Reddit URLs (https://example.com, http, relative paths, etc.) are
  passed through unchanged
- Empty / non-string inputs are passed through (defensive)
- The function is exported from api.ts (so other components can use it)
- All 4 window.open call sites in Card.tsx that could open Reddit
  URLs are now wrapped in safeExternalUrl (the call site that
  intentionally excludes is the podcast audio URL)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
API = REPO / "frontend/src/api.ts"
CARD = REPO / "frontend/src/components/Card.tsx"


# ---------------------------------------------------------------------------
# 1. Source-shape checks (function exists, exported, doc'd)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_safe_external_url_function_exists():
    src = API.read_text()
    assert re.search(
        r"export function safeExternalUrl\(", src,
    ), (
        "api.ts must export safeExternalUrl so other components can "
        "use the reddit.com -> old.reddit.com rewrite."
    )


@pytest.mark.no_db
def test_safe_external_url_documents_why_old_reddit():
    """The function's doc comment must explain WHY we rewrite — so a
    future maintainer doesn't remove the rewrite thinking it's dead
    code. The "Get the App" trap on iOS PWA is the real reason."""
    src = API.read_text()
    # The function + its JSDoc comment block immediately above. The
    # WHY (Get the App / iOS PWA / modal) lives in the docstring,
    # not the function body, so we have to capture both.
    m = re.search(
        r"/\*\*[\s\S]+?\*/\s*export function safeExternalUrl\([^)]*\): string \{[\s\S]+?\n\}",
        src,
    )
    assert m, "Couldn't locate safeExternalUrl function + docstring"
    full = m.group(0)
    assert "reddit.com" in full.lower() or "old.reddit" in full.lower(), (
        "safeExternalUrl must reference reddit.com and old.reddit.com in "
        "its body or docstring so the next maintainer knows the rewrite "
        "is intentional, not a typo."
    )
    assert re.search(
        r"Get the App|iOS|PWA|modal|overlay",
        full,
    ), (
        "safeExternalUrl's docstring must explain the iOS PWA "
        "trap (Get the App modal) so a future maintainer doesn't "
        "remove the rewrite."
    )


# ---------------------------------------------------------------------------
# 2. Behavioral tests
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_safe_external_url_rewrites_reddit_com():
    """The function must check ``reddit.com`` and ``www.reddit.com``
    hostnames and rewrite them to ``old.reddit.com``. Other URLs
    pass through unchanged."""
    src = API.read_text()
    m = re.search(
        r"export function safeExternalUrl\([^)]*\): string \{(.+?)^\}",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, "Couldn't extract safeExternalUrl body"
    body = m.group(1)

    assert re.search(r"u\.hostname\s*===\s*['\"]reddit\.com['\"]", body), (
        "safeExternalUrl must check hostname === 'reddit.com'"
    )
    assert re.search(r"u\.hostname\s*===\s*['\"]www\.reddit\.com['\"]", body), (
        "safeExternalUrl must check hostname === 'www.reddit.com' "
        "(the cross-reference sweep and reddit source both store the "
        "www variant)."
    )
    assert re.search(
        r"u\.hostname\s*=\s*['\"]old\.reddit\.com['\"]", body,
    ), "Rewrite target must be old.reddit.com"


@pytest.mark.no_db
def test_safe_external_url_passes_through_other_urls():
    """Non-Reddit URLs are unchanged. The function must wrap the
    hostname check in a try/catch (new URL() throws on invalid input)
    and return the input unchanged on failure."""
    src = API.read_text()
    m = re.search(
        r"export function safeExternalUrl\([^)]*\): string \{(.+?)^\}",
        src,
        re.MULTILINE | re.DOTALL,
    )
    body = m.group(1)

    assert "try" in body, "safeExternalUrl must try/catch around new URL()"
    assert "catch" in body, "safeExternalUrl must catch URL parse errors"
    assert re.search(r"return\s+url\b", body), (
        "safeExternalUrl must return the input url unchanged on "
        "parse failure or empty input."
    )


# ---------------------------------------------------------------------------
# 3. Card.tsx call-site coverage
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_card_uses_safe_external_url_for_entry_url():
    """The context-menu Open in new tab action must wrap
    entry.url in safeExternalUrl. This is the path the user
    takes when they long-press a card and pick Open in new tab."""
    src = CARD.read_text()
    m = re.search(
        r"label:\s*['\"]Open in new tab['\"][\s\S]*?window\.open\((safeExternalUrl\([^)]+\))",
        src,
    )
    assert m, (
        "Couldn't find the context-menu Open in new tab handler using "
        "safeExternalUrl. Expected window.open(safeExternalUrl(entry.url)."
    )


@pytest.mark.no_db
def test_card_uses_safe_external_url_for_reddit_thread_url():
    src = CARD.read_text()
    # href= and window.open() for the reddit thread link
    m = re.search(
        r"href=\{entry\.reddit_thread_url\}",
        src,
    )
    assert not m, (
        "The reddit-thread link href must be wrapped in safeExternalUrl, "
        "not the raw entry.reddit_thread_url. Middle-click / cmd-click / "
        "context-menu Open in new tab use the href, so this path matters."
    )
    m = re.search(
        r"href=\{safeExternalUrl\(entry\.reddit_thread_url\)\}",
        src,
    )
    assert m, "Expected href={safeExternalUrl(entry.reddit_thread_url)}"


@pytest.mark.no_db
def test_card_uses_safe_external_url_for_thumbnail_open():
    """The Thumbnail component's open() must use safeExternalUrl
    so thumbnail clicks on Reddit-source entries also bypass the
    app prompt."""
    src = CARD.read_text()
    m = re.search(
        r"const open = \(\) => \{[\s\S]*?window\.open\((safeExternalUrl\([^)]+\))",
        src,
    )
    assert m, (
        "Couldn't find Thumbnail's open() function using safeExternalUrl. "
        "Expected window.open(safeExternalUrl(url)."
    )


@pytest.mark.no_db
def test_card_uses_safe_external_url_for_related_coverage():
    """The related-coverage a.url open must also use
    safeExternalUrl — related articles from a Reddit thread
    share the host."""
    src = CARD.read_text()
    # Just look for the wrap anywhere in the file (the related.map
    # block is far from the window.open call, regex backreferences
    # through the intervening JSX would be fragile). The two must
    # both be present for the related-articles path to use the wrap.
    assert "safeExternalUrl(a.url)" in src, (
        "Couldn't find safeExternalUrl(a.url) — related-articles click "
        "should be wrapped. The wrap is missing entirely."
    )
    assert re.search(
        r"related\.articles\.map",
        src,
    ), (
        "Couldn't find related.articles.map — the related-coverage "
        "section is missing entirely (different bug, but worth surfacing)."
    )


@pytest.mark.no_db
def test_card_does_not_wrap_audio_url():
    """The podcast audio_url window.open is intentionally NOT
    wrapped in safeExternalUrl — it's not a Reddit URL. Pin this
    so a future refactor doesn't blanket-wrap everything."""
    src = CARD.read_text()
    m = re.search(
        r"window\.open\(entry\.audio_url!",
        src,
    )
    assert m, "Couldn't find audio_url window.open"
    assert "safeExternalUrl" not in m.group(0), (
        f"audio_url window.open should NOT be wrapped in safeExternalUrl — "
        f"podcast audio URLs aren't Reddit threads. Got: {m.group(0)!r}"
    )