"""Slice-15 wire tests for the generic_scrape + Track-anyway changes.

The slice-15 change is purely frontend (no backend change). The
sandbox doesn't have node, so we can't run the TypeScript
compiler — instead these tests lock the wire at the source-text
level: the sourceType union, the radio button, the URL field
label, the validateForm branches, the trackAnyway button, and
the slugify helper. A future refactor that drops or renames any
of these is caught here before it ships.

This is the slice-11/12/13/14 no_db test style applied to a
frontend change. The contract the tests assert:

  1. The sourceType union includes 'generic_scrape'.
  2. There's a 5th radio input with value="generic_scrape".
  3. The radio's label text mentions "no RSS" (so the user
     understands what it does).
  4. validateForm treats 'generic_scrape' like the URL-validating
     group (rss/podcast/youtube_channel/generic_scrape) — i.e. it
     rejects a non-URL string.
  5. The useEffect that mirrors validateForm on every change also
     treats 'generic_scrape' as a URL-validating type.
  6. The dynamic URL field label/placeholder render a
     generic_scrape-specific string when sourceType === 'generic_scrape'.
  7. The auto-discovery error message renders a "Track this URL
     anyway" button when the error text includes the
     "couldn't find a feed" string.
  8. The trackAnyway handler exists and POSTs type='generic_scrape'
     to api.createSource.
  9. The trackAnyway handler mirrors the backend's
     ``_slugify_hostname`` so the auto-derived name matches what
     the auto-discovery path would have produced.

Tests run in pure Python — no node, no TypeScript, no JSX parser.
The cost is that we assert on substrings, not on actual runtime
behavior. The CI step (and the user's local tsc) covers the
real behavior.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Walk up from this test file's location to find the repo root, then
# resolve the frontend file relative to that. The CI runner does
# ``git clone`` into a fresh directory, so a hard-coded
# ``/tmp/popping-review`` would point to a path that doesn't exist
# in CI. ``Path(__file__).parents[2]`` walks from
# ``backend/tests/`` to the repo root regardless of where the
# checkout lives.
REPO = Path(__file__).resolve().parents[2]
FEEDMANAGER = REPO / "frontend/src/components/FeedManager.tsx"


def _read_source() -> str:
    return FEEDMANAGER.read_text()


# ---------------------------------------------------------------------------
# 1. sourceType union includes 'generic_scrape'
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_sourcetype_union_includes_generic_scrape():
    src = _read_source()
    m = re.search(
        r"useState<\s*'rss'\s*\|\s*'reddit'\s*\|\s*'podcast'\s*\|\s*'youtube_channel'\s*\|\s*'generic_scrape'\s*>",
        src,
    )
    assert m, (
        "sourceType union must include 'generic_scrape'. "
        "Found union without it — the manual form's source type selector "
        "doesn't expose the 'Track page' option."
    )


# ---------------------------------------------------------------------------
# 2. 5th radio with value="generic_scrape"
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_fifth_radio_has_value_generic_scrape():
    src = _read_source()
    radio_values = re.findall(r'<input[^>]*type="radio"[^>]*value="([^"]+)"', src)
    assert "rss" in radio_values
    assert "reddit" in radio_values
    assert "podcast" in radio_values
    assert "youtube_channel" in radio_values
    assert "generic_scrape" in radio_values, (
        f"Missing 5th radio with value='generic_scrape'. "
        f"Found radio values: {radio_values}"
    )


# ---------------------------------------------------------------------------
# 3. Radio's label text mentions "no RSS"
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_generic_scrape_radio_label_mentions_no_rss():
    src = _read_source()
    # Find the <label> whose <input> is the generic_scrape radio.
    # The radio's <label> has className= and title= attributes
    # (the title contains "no RSS" as a tooltip), and contains
    # an <input value="generic_scrape">. Walk backward from
    # the <input> tag to find its enclosing <label>.
    radio_input = re.search(
        r'<input\s+type="radio"\s+name="fm-type"\s+value="generic_scrape"',
        src,
    )
    assert radio_input, "Could not find the generic_scrape radio <input>"
    # The <label> opens right before the <input>. Find the last
    # "<label" before the input.
    label_open_idx = src.rfind("<label", 0, radio_input.start())
    assert label_open_idx != -1, "Could not find the <label> opening before the radio"
    # The <label> closes after the visible text. The text is the
    # last line before </label>.
    label_close_idx = src.find("</label>", radio_input.end())
    assert label_close_idx != -1, "Could not find the </label> closing after the radio"
    label_block = src[label_open_idx:label_close_idx + len("</label>")]
    assert "no RSS" in label_block, (
        f"Label wrapping the generic_scrape radio should mention 'no RSS' "
        f"(in title or visible text) so the user knows this option is for "
        f"non-feed sites. Got: {label_block!r}"
    )
    # Also confirm the visible text is "Track page (no RSS)".
    assert "Track page" in label_block, (
        f"Visible label text should be 'Track page (no RSS)'. "
        f"Got: {label_block!r}"
    )


# ---------------------------------------------------------------------------
# 4. validateForm treats 'generic_scrape' as a URL-validating type
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_validate_form_treats_generic_scrape_as_url_type():
    src = _read_source()
    snippet = re.search(
        r"sourceType === 'rss'[^}]+sourceType === 'generic_scrape'",
        src,
        re.DOTALL,
    )
    assert snippet, (
        "validateForm does not include 'generic_scrape' alongside "
        "the other URL-validating types. A non-URL string with "
        "type='generic_scrape' will be accepted by the form and "
        "the backend will reject it as 422 instead."
    )
    assert "sourceType === 'podcast'" in src
    assert "sourceType === 'youtube_channel'" in src


# ---------------------------------------------------------------------------
# 5. The useEffect that re-validates on every change also handles
#    'generic_scrape' as a URL-validating type
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_useeffect_revalidate_treats_generic_scrape_as_url_type():
    src = _read_source()
    useeffect_blocks = re.findall(
        r"useEffect\(\(\) => \{(.*?)\}, \[name, url, sourceType\]\)",
        src,
        re.DOTALL,
    )
    assert useeffect_blocks, "Could not find the useEffect that re-validates"
    found = False
    for block in useeffect_blocks:
        if (
            "generic_scrape" in block
            and "new URL(trimmedUrl)" in block
        ):
            found = True
            break
    assert found, (
        "The useEffect that re-validates on every change does not include "
        "'generic_scrape' in its URL-validating branch."
    )


# ---------------------------------------------------------------------------
# 6. Dynamic URL field label/placeholder for generic_scrape
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_url_label_uses_generic_scrape_specific_text():
    src = _read_source()
    label_block = re.search(
        r"htmlFor=\"fm-url\"[^>]*>\s*\{([^}]+sourceType === 'generic_scrape'[^}]+)\}",
        src,
        re.DOTALL,
    )
    assert label_block, "Could not find the URL field label ternary chain"
    assert "Page URL" in label_block.group(1) or "scrape" in label_block.group(1).lower(), (
        f"URL field label for generic_scrape should mention 'Page URL' "
        f"or 'scrape'. Got: {label_block.group(1)}"
    )


@pytest.mark.no_db
def test_url_placeholder_uses_generic_scrape_specific_text():
    src = _read_source()
    placeholder_block = re.search(
        r"placeholder=\{([\s\S]+?)\}",
        src,
    )
    assert placeholder_block, "Could not find the URL placeholder ternary chain"
    # Within the placeholder, the generic_scrape branch is the
    # ``sourceType === 'generic_scrape' ? '<value>' : <next>`` clause.
    # Pull just the value assigned for generic_scrape.
    m = re.search(
        r"sourceType === 'generic_scrape'\s*\?\s*'([^']+)'",
        placeholder_block.group(1),
    )
    assert m, "Could not find the generic_scrape placeholder value"
    value = m.group(1)
    assert value == "https://example.com", (
        f"generic_scrape placeholder should be a homepage URL "
        f"('https://example.com'), not a feed URL. Got: {value!r}"
    )


# ---------------------------------------------------------------------------
# 7. Auto-discovery error renders a "Track this URL anyway" button
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_auto_discovery_error_renders_track_anyway_button():
    src = _read_source()
    assert "couldn't find a feed" in src, (
        "Missing the trigger string for the Track-anyway button."
    )
    assert "Track this URL anyway" in src, (
        "Missing the 'Track this URL anyway' button label."
    )
    pattern = re.compile(
        r"autoMessage\.kind === 'err'[\s\S]+?couldn't find a feed[\s\S]+?Track this URL anyway",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "The 'Track this URL anyway' button must be rendered inside a "
        "conditional gated on the auto-discovery error text."
    )


# ---------------------------------------------------------------------------
# 8. trackAnyway handler exists and POSTs type='generic_scrape'
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_track_anyway_handler_creates_generic_scrape_source():
    src = _read_source()
    handler = re.search(
        r"const trackAnyway = async \(\) => \{([\s\S]+?)\n  \}\n",
        src,
    )
    assert handler, "Could not find the trackAnyway handler"
    body = handler.group(1)
    assert "api.createSource" in body, "trackAnyway must call api.createSource"
    assert "type: 'generic_scrape'" in body, (
        "trackAnyway must POST type='generic_scrape'"
    )
    assert "setAutoUrl('')" in body
    assert "setAutoMessage(null)" in body
    assert "onAdded()" in body


# ---------------------------------------------------------------------------
# 9. trackAnyway mirrors the backend's _slugify_hostname
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_track_anyway_slugs_match_backend():
    src = _read_source()
    handler = re.search(
        r"const trackAnyway = async \(\) => \{([\s\S]+?)\n  \}\n",
        src,
    )
    assert handler, "Could not find the trackAnyway handler"
    body = handler.group(1)
    expected_operations = [
        "new URL(trimmed).hostname",
        "toLowerCase",
        'replace(/^www\\./, \'\')',
        'replace(/[^a-z0-9]+/g, \'_\')',
        "slice(0, 100)",
    ]
    for op in expected_operations:
        assert op in body, f"trackAnyway missing slugify operation: {op!r}"


# ---------------------------------------------------------------------------
# Bonus: confirm the existing sourceType union still includes the
# original 4 (so a future refactor doesn't drop one)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_all_five_sourcetypes_present_in_union():
    src = _read_source()
    for t in ("rss", "reddit", "podcast", "youtube_channel", "generic_scrape"):
        assert f"'{t}'" in src, f"Missing sourceType value: {t!r}"


# ---------------------------------------------------------------------------
# 10. api.testSource body type also includes 'generic_scrape'
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_api_test_source_body_type_includes_generic_scrape():
    """The api.testSource body type was a separate, narrower union
    (``'rss' | 'reddit' | 'podcast' | 'youtube_channel'``) that
    didn't include ``'generic_scrape'``. The manual form's
    ``Test`` button calls ``api.testSource({type: sourceType, ...})``
    — when ``sourceType === 'generic_scrape'``, the narrower
    type produced a TS2322 error. Extend the union to match.
    """
    api_src = (REPO / "frontend/src/api.ts").read_text()
    # Find the testSource body declaration and confirm
    # 'generic_scrape' is in the type union for the ``type`` field.
    m = re.search(
        r"testSource:\s*\(body:\s*\{[^}]*type\?:\s*'rss'\s*\|\s*'reddit'\s*\|\s*'podcast'\s*\|\s*'youtube_channel'\s*\|\s*'generic_scrape'",
        api_src,
    )
    assert m, (
        "api.testSource's body type must include 'generic_scrape' "
        "in the ``type`` union, otherwise calling test() with a "
        "generic_scrape source type produces a TS2322 error. "
        "The PR #72 build failed on this — locks the wire here."
    )
