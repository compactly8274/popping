"""Slice 21 regression tests — ``React.memo`` wrap on ``Column`` + ``useCallback`` on App handlers.

This test file is Python-side smoke (no JS runtime needed). The real
verification is the TypeScript + ESLint build in CI; here we assert the
structural invariants the slice is supposed to enforce.

Why a Python test for a frontend slice:
  - Frontend slice bugs are structurally obvious: a missing memo wrap, a
    handler that wasn't useCallback'd, a comparator that compares by
    shallow equality on a fresh-object prop. Catching these in code
    review is fine; catching them in a Python regex test is faster.
  - The user has a ``no_db`` pytest marker pattern (slices 9f / 10) for
    tests that don't need Postgres. Frontend-only slices belong in this
    bucket.
  - Pattern mirrors ``test_no_db_skip_*.py`` from earlier slices — a
    small file with focused assertions about source shape.

What this test guards against:
  - Someone deletes the memo wrap while doing a frontend refactor (the
    wrap is the only thing preventing every App re-render from
    re-rendering every Column).
  - Someone reverts the useCallback wraps on App handlers (would
    re-introduce the inline-arrow JSX props and defeat the memo).
  - Someone breaks the comparator (e.g. by adding ``===`` on a fresh
    object like ``prefs`` that defeats the memo — already documented in
    the comparator's comment, but a regression guard is cheap).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root resolved from the test file location — works in any CI
# environment. ``backend/tests/test_slice21_column_memo.py`` →
# parents[0]=tests, parents[1]=backend, parents[2]=repo root.
REPO = Path(__file__).resolve().parents[2]
COLUMN = REPO / "frontend/src/components/Column.tsx"
APP = REPO / "frontend/src/App.tsx"


# ---------------------------------------------------------------------------
# 1. Column is wrapped in React.memo with a custom comparator
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_column_inner_renamed_from_column():
    """``Column`` is now ``ColumnInner`` so we can wrap it in ``memo``."""
    src = COLUMN.read_text()
    assert "export function ColumnInner" in src, (
        "Column must be renamed to ColumnInner so it can be wrapped in "
        "React.memo without breaking the JSX usage sites that import "
        "Column as the default."
    )


@pytest.mark.no_db
def test_column_exported_via_memo():
    """The exported ``Column`` is the ``memo()`` of ``ColumnInner``."""
    src = COLUMN.read_text()
    assert re.search(r"export const Column = memo\(ColumnInner", src), (
        "Column must be exported as ``memo(ColumnInner, _columnPropsEqual)``. "
        "Without this, every App re-render re-renders every Column, "
        "which re-renders every Card subtree."
    )


@pytest.mark.no_db
def test_memo_imported_in_column():
    src = COLUMN.read_text()
    assert re.search(r"import \{[^}]*\bmemo\b[^}]*\} from 'react'", src), (
        "``memo`` must be imported from React in Column.tsx for the "
        "memo wrap to compile."
    )


# ---------------------------------------------------------------------------
# 2. The comparator checks the right data-driven props
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_column_comparator_function_exists():
    src = COLUMN.read_text()
    assert "function _columnPropsEqual" in src, (
        "The memo comparator must be a named function so React's "
        "memo() can call it on every render. An inline arrow would "
        "defeat the memo by changing identity on every render."
    )


@pytest.mark.no_db
@pytest.mark.parametrize("prop", [
    "sections",
    "sourcesById",
    "sourcesByName",
    "newCount",
    "selectedId",
    "prefs",
    "totalCount",
    "categoriesBySourceId",
    "faviconBySourceId",
    "expandedSummaries",
    "starredSet",
    "hiddenSet",
    "votedMap",
    "sectionsCollapsed",
])
def test_column_comparator_checks_prop(prop):
    """The comparator must compare each data-driven prop by reference.

    ``===`` (reference equality) is what makes the memo skip when the
    parent re-renders without changing data. The prop list mirrors
    Column's ``Props`` type minus the callback props (which are
    intentionally ignored — they're re-allocated by the App layer
    every render, and a callback that's reference-equal but closes
    over stale state is a worse bug than a false-negative memo).
    """
    src = COLUMN.read_text()
    pattern = rf"prev\.{prop}\s*===\s*next\.{prop}"
    assert re.search(pattern, src), (
        f"_columnPropsEqual must compare ``{prop}`` by reference. "
        "Missing it would let an unchanged parent prop trigger a "
        "re-render, defeating the memo."
    )


# ---------------------------------------------------------------------------
# 3. All 9 App handlers are wrapped in useCallback
# ---------------------------------------------------------------------------


HANDLERS = [
    "markColumnRead",
    "toggleSource",
    "onSourceRenamed",
    "toggleSourceAndMaybeClose",
    "clearSourceFilters",
    "jumpToCategory",
    "setPrefsFor",
    "setColumnSection",
    "sectionsCollapsedFor",
]


@pytest.mark.no_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_app_handler_uses_usecallback(handler):
    """Each handler must be a ``useCallback`` so its identity is stable.

    Without this, the JSX ``onMarkRead={() => markColumnRead(col.name)}``
    inline closures re-allocate every render. The Column.memo comparator
    intentionally ignores callback props, so the memo *would* skip
    anyway — but Card.memo is more permissive and these handlers are
    also passed to Card children via ColumnSection's per-card arrows.
    Stable identity here makes the whole tree memo-friendly.
    """
    src = APP.read_text()
    m = re.search(rf"const\s+{handler}\s*=", src)
    assert m, f"Handler ``{handler}`` not found in App.tsx"
    # Look 60 chars ahead to catch the useCallback( call
    snippet = src[m.start():m.start()+60]
    assert "useCallback(" in snippet, (
        f"Handler ``{handler}`` is not wrapped in useCallback. "
        f"Got: {snippet!r}. Reverting this wrap would re-introduce "
        "inline-arrow JSX closures that defeat Card.memo downstream."
    )


@pytest.mark.no_db
def test_sections_collapsed_uses_memo_cache():
    """``sectionsCollapsedFor`` returns from a useMemo cache for stable identity.

    The Column.memo comparator uses ``===`` on ``sectionsCollapsed``,
    so the App layer must return a stable object reference per column
    name. A naive lookup returning a fresh ``{ new, history }`` per
    render would always defeat the memo on this prop alone.
    """
    src = APP.read_text()
    assert "sectionsCollapsedCache" in src, (
        "Expected a useMemo-backed cache to give sectionsCollapsedFor "
        "stable identity. Without it, Column.memo skips are "
        "defeated for every Column."
    )
    assert re.search(
        r"sectionsCollapsedCache\s*=\s*useMemo", src
    ), "sectionsCollapsedCache should be a useMemo"
    assert re.search(
        r"sectionsCollapsedFor\s*=\s*useCallback", src
    ), "sectionsCollapsedFor should itself be a useCallback"
    assert "sectionsCollapsedCache[columnName]" in src, (
        "sectionsCollapsedFor must read from the cache, not build "
        "a fresh object per call."
    )


# ---------------------------------------------------------------------------
# 4. No naked const = arrow for the 9 handlers (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_app_handler_not_naked_arrow(handler):
    """Negative test — none of the 9 handlers should be plain ``const = arrow``."""
    src = APP.read_text()
    # Match: const h = (args) => { ... but NOT inside useCallback
    # A naked arrow would be: ``const h = (name: string) => {``
    # useCallback wraps with: ``const h = useCallback((name: string) => {``
    m = re.search(
        rf"const\s+{handler}\s*=\s*\(([^)]*)\)\s*=>\s*\{{",
        src,
    )
    assert not m, (
        f"Handler ``{handler}`` is a naked arrow (no useCallback). "
        "This regresses slice 21 — wrap it in useCallback."
    )