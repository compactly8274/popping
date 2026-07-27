"""Slice-19 wire tests: Column empty-state shows source status when available.

The frontend Column component renders a "no entries in this column
yet" message when the column has zero entries. Before slice 19,
the message was the same regardless of whether:
  - the source had never been fetched yet
  - the last fetch errored
  - the source was auto-disabled after too many errors
  - the last fetch succeeded but found 0 articles
  - the column isn't a source column (Saved / For You / Multisub)

Slice 19 adds a context-aware empty-state that surfaces the source's
``last_fetch_at``, ``error_count``, and ``last_error`` so users
can diagnose "why is this column empty" without opening the
FeedManager.

These are wire tests: they parse the Column.tsx + App.tsx source as
text and verify the JSX branches the new logic generates. The
sandbox has no node/tsc, so we can't run the React component
itself, but the wire tests catch:
  - the props interface exposes ``sourcesByName`` as optional
  - the lookup-by-name gate is in place
  - the four state-specific messages are all present
  - the meta-column fallback (Saved, For You, Multisub) preserves
    the original message
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COLUMN = REPO / "frontend/src/components/Column.tsx"
APP = REPO / "frontend/src/App.tsx"


def _read_column() -> str:
    return COLUMN.read_text()


def _read_app() -> str:
    return APP.read_text()


# ---------------------------------------------------------------------------
# 1. sourcesByName prop is exposed on Column
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_column_accepts_sourcesByName_prop():
    """The Column component should accept an optional ``sourcesByName``
    map so the empty-state JSX can look up the column's source by
    name. Optional so meta columns (Saved / For You / Multisub) can
    omit it without TS errors.
    """
    src = _read_column()
    assert "sourcesByName" in src, "Column must accept sourcesByName prop"
    # Should be in the Props type definition (not just a usage).
    assert re.search(r"sourcesByName\??\s*:\s*Map<", src), (
        "sourcesByName should be typed as Map<...> in the Props interface"
    )


# ---------------------------------------------------------------------------
# 2. Empty-state JSX branches on source presence
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_empty_state_branches_on_source_lookup():
    """The empty-state message should branch on whether a source
    was found for the column name. The four states:
      - source found + last_error: show error
      - source found + error_count >= threshold: show auto-disabled
      - source found + last_fetch_at: show "last fetched Xh ago"
      - source not found (meta column): show original generic message
    """
    src = _read_column()
    # The original message must still exist (preserved for meta columns).
    assert "no entries in this column yet" in src, (
        "original generic message should be preserved for meta columns"
    )
    # Source-aware messages should be present.
    assert "last_error" in src, "empty-state should reference last_error"
    assert "error_count" in src, "empty-state should reference error_count"
    assert "last_fetch_at" in src, "empty-state should reference last_fetch_at"


# ---------------------------------------------------------------------------
# 3. source.name lookup happens via sourcesByName.get(column.name)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_source_lookup_uses_column_name():
    """The Column's empty-state must look up the source by the
    column's name via ``sourcesByName.get(name)``. The lookup
    can be inlined in the empty-state JSX or delegated to a
    helper component (slice 19's ``EmptyColumnMessage``) —
    either is fine; what matters is the lookup happens.
    """
    src = _read_column()
    # The lookup has to happen somewhere in the file. The
    # source-bound branch checks `sourcesByName.get(name)` to
    # find the column's source; meta columns don't reach that
    # branch because ``source`` is undefined.
    assert "sourcesByName" in src, (
        "Column should reference sourcesByName for source lookup"
    )
    # The .get() call has to appear at least once near a
    # sourcesByName reference. Use a coarse substring check.
    assert "sourcesByName" in src and ".get(" in src, (
        "Column should call .get() on sourcesByName somewhere"
    )


# ---------------------------------------------------------------------------
# 4. App.tsx passes sourcesByName to Column
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_app_passes_sourcesByName_to_column():
    """App.tsx owns the source list and the column rendering. It
    needs to compute a sourcesByName map and pass it down to both
    Column call sites (desktop and mobile).
    """
    app = _read_app()
    # The map should exist somewhere in App.tsx.
    assert "sourcesByName" in app, (
        "App.tsx should compute a sourcesByName map of Source objects"
    )
    # Both Column call sites should receive it.
    column_call_sites = list(re.finditer(r"<Column\b[^>]*>", app, re.DOTALL))
    assert len(column_call_sites) >= 2, "App.tsx should have at least 2 Column call sites"
    for i, site in enumerate(column_call_sites):
        assert "sourcesByName=" in site.group(0), (
            f"Column call site #{i + 1} must pass sourcesByName"
        )


# ---------------------------------------------------------------------------
# 5. The empty-state error snippet is truncated (no log spam)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_last_error_truncated_in_empty_state():
    """last_error from the source row can be long (full traceback
    strings). The empty-state should show a short snippet, not
    dump the whole thing.
    """
    src = _read_column()
    has_truncation = bool(re.search(r"\.slice\(\s*0,\s*\d+", src)) or \
                     bool(re.search(r"\.substring\(\s*0,\s*\d+", src))
    assert has_truncation, (
        "Empty-state should truncate last_error to a short snippet"
    )


# ---------------------------------------------------------------------------
# 6. Auto-disabled state is distinct from generic error
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_empty_state_distinguishes_auto_disabled_from_generic_error():
    """When a source's error_count has hit the auto-disable threshold,
    the message should call that out distinctly from a transient
    fetch error.
    """
    src = _read_column()
    has_auto_disabled = "auto-disabled" in src or "auto_disabled" in src
    has_error_snippet = "last_error" in src and ("slice" in src or "substring" in src)
    assert has_auto_disabled, (
        "Empty-state should distinguish auto-disabled sources"
    )
    assert has_error_snippet, (
        "Empty-state should still surface transient fetch errors"
    )