"""The phone checks feed names, categories and headers before saving,
using copies of the backend's rules in frontend/src/lib. These tests
read those TypeScript files and fail when a copy drifts from the
backend, in either direction, so a rule changed on one side can't
leave the phone accepting what the server refuses (or the reverse).

The TS constants are written as plain array and object literals so a
regex can read them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.brief import _HISTORICAL_SOURCE_SLUGS
from app.routes import sources as routes
from app.sources import list_sources

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "frontend/src/lib"


def _ts(name: str) -> str:
    return (LIB / name).read_text()


def _string_list(src: str, const: str) -> list[str]:
    m = re.search(rf"export const {const}\b[^=]*=\s*\[([^\]]*)\]", src)
    assert m, f"couldn't find the {const} array literal"
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return re.findall(r"'([^']*)'", body)


def _number(src: str, const: str) -> int:
    m = re.search(rf"export const {const}\b[^=]*=\s*(\d+)", src)
    assert m, f"couldn't find the {const} number"
    return int(m.group(1))


def _string_map(src: str, const: str) -> dict[str, str]:
    m = re.search(rf"export const {const}\b[^=]*=\s*\{{([^}}]*)\}}", src)
    assert m, f"couldn't find the {const} object literal"
    return dict(re.findall(r"'([^']*)'\s*:\s*'([^']*)'", m.group(1)))


@pytest.mark.no_db
def test_known_built_ins_match_the_plugin_registry():
    names = _string_list(_ts("feedRules.ts"), "KNOWN_BUILT_INS")
    assert len(names) == len(set(names))
    assert set(names) == set(list_sources())


@pytest.mark.no_db
def test_historical_sources_match_brief():
    """The feeds whose cards say "On this day" in place of a time (PR4d)
    are the ones Brief and For You keep out as history."""
    names = _string_list(_ts("feedRules.ts"), "HISTORICAL_SOURCES")
    assert len(names) == len(set(names))
    assert set(names) == set(_HISTORICAL_SOURCE_SLUGS)


@pytest.mark.no_db
def test_header_denylist_matches_both_ways():
    names = _string_list(_ts("headers.ts"), "HEADER_DENYLIST")
    assert len(names) == len(set(names))
    assert set(names) == set(routes._HEADER_DENYLIST_ALL)


@pytest.mark.no_db
def test_header_limits_and_canonical_names_match():
    src = _ts("headers.ts")
    assert _number(src, "HEADERS_MAX") == routes._CUSTOM_HEADERS_MAX
    assert _number(src, "HEADER_VALUE_MAX") == routes._HEADER_VALUE_MAX
    assert _number(src, "HEADER_NAME_MAX") == routes._HEADER_NAME_MAX
    assert _string_map(src, "CANONICAL_HEADER_NAMES") == routes._CANONICAL_HEADER_NAMES


@pytest.mark.no_db
def test_category_limits_match():
    src = _ts("categories.ts")
    assert _number(src, "CATEGORY_MAX") == routes._CATEGORY_MAX
    assert set(_string_list(src, "RESERVED_CATEGORIES")) == routes._RESERVED_CATEGORY_NAMES


# The old inline edit form (a datalist for the category, which iOS
# Safari never shows, and a JSON textarea for headers) was replaced by
# the feed editor in components/feeds. Keep it from coming back.
FEED_MANAGER = REPO / "frontend/src/components/FeedManager.tsx"


@pytest.mark.no_db
@pytest.mark.parametrize("text", ["<datalist", "custom headers (JSON)", "JSON.parse(editHeaders"])
def test_old_edit_form_is_gone(text):
    assert text not in FEED_MANAGER.read_text()
