"""Slice 28 -- entries (composite_score DESC, fetched_at) index.

Guards migration 0028 and the three query paths it serves. The
query-shape guards use FUNCTION-BOUNDED spans: a refactor that
drops the ``ORDER BY composite_score DESC`` (or the brief paths'
``fetched_at >=`` predicate) renders the index useless, and these
tests must fail when that happens -- without a lazy end-of-file
span that a later unrelated edit anywhere in the module could
satisfy. (The slice-25 suite's original spans had exactly that
defect; this suite and its slice-25 replacement use bounded
extraction instead.)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root resolved from the test file location.
REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "backend/alembic/versions/0028_entries_composite_score_fetched_at_index.py"
FORYOU = REPO / "backend/app/routes/foryou.py"
BRIEF = REPO / "backend/app/brief.py"


def _module_func(src: str, name: str) -> str:
    """Module-level (``async``) function body, bounded by the next
    module-level ``def`` / ``async def`` / ``class`` -- never by
    end-of-file."""
    m = re.search(
        rf"^(?:async )?def {re.escape(name)}\(.*?(?=\n^(?:async )?def |\n^class |\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, f"Couldn't locate {name}() at module level"
    return m.group(0)


def _method(src: str, name: str) -> str:
    """Method body inside a class, bounded by the next same-indent
    method / decorator or the class's end."""
    m = re.search(
        rf"^    (?:async )?def {re.escape(name)}\(.*?(?=\n    (?:async )?def |\n    @|\n[^\s]|\Z)",
        src,
        re.MULTILINE | re.DOTALL,
    )
    assert m, f"Couldn't locate {name}() as a method"
    return m.group(0)


# ---------------------------------------------------------------------------
# 1. Migration exists, correct revision chain, creates + drops the index
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_migration_0028_exists():
    assert MIGRATION.exists(), (
        "alembic/versions/0028_entries_composite_score_fetched_at_index.py "
        "must exist -- the migration is how production gets the index."
    )


@pytest.mark.no_db
def test_migration_revision_chain():
    src = MIGRATION.read_text()
    assert re.search(r'^revision\s*=\s*"0028"', src, re.MULTILINE), (
        "Migration must declare revision = '0028' (matches filename)."
    )
    assert re.search(r'^down_revision\s*=\s*"0027"', src, re.MULTILINE), (
        "Migration must declare down_revision = '0027' -- current head is "
        "0027_flatten_nested_entry_meta."
    )


@pytest.mark.no_db
def test_migration_upgrade_creates_index():
    src = MIGRATION.read_text()
    assert re.search(
        r'op\.create_index\(\s*"ix_entries_composite_score_fetched_at"', src
    ), "upgrade() must create ix_entries_composite_score_fetched_at."
    assert re.search(r'"entries"', src), "Index must be on the entries table."
    # Leading column is the sort (descending); trailing column is the range.
    assert "composite_score DESC" in src
    assert '"fetched_at"' in src
    assert src.index("composite_score DESC") < src.index('"fetched_at"'), (
        "Column order matters: composite_score DESC must lead so the scan "
        "starts at the high-score end."
    )


@pytest.mark.no_db
def test_migration_downgrade_drops_index():
    src = MIGRATION.read_text()
    assert "op.drop_index(" in src
    assert "ix_entries_composite_score_fetched_at" in src, (
        "downgrade must target the same index name as upgrade created."
    )


# ---------------------------------------------------------------------------
# 2. Query-shape guards -- the paths the index serves (function-bounded)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_foryou_still_orders_by_composite_score_desc():
    """/api/foryou must keep ordering candidates by composite_score
    DESC -- the primary path the 0028 index serves (no time filter
    here, so the leading sort column is all it can use)."""
    body = _module_func(FORYOU.read_text(), "foryou")
    assert re.search(r"composite_score\.desc\(\)", body), (
        "foryou must keep ORDER BY composite_score DESC -- dropping it "
        "renders the 0028 index useless on the hottest path."
    )


@pytest.mark.no_db
def test_brief_select_entries_keeps_sort_and_fetched_at_filter():
    """brief._select_entries must keep BOTH the composite_score DESC
    sort and the fetched_at >= predicate -- the index serves the pair
    (leading sort + trailing range)."""
    body = _method(BRIEF.read_text(), "_select_entries")
    assert re.search(r"desc\(Entry\.composite_score\)", body)
    assert re.search(r"Entry\.fetched_at\s*>=", body)


@pytest.mark.no_db
def test_brief_select_entries_by_slug_keeps_sort_and_fetched_at_filter():
    body = _method(BRIEF.read_text(), "_select_entries_by_slug")
    assert re.search(r"desc\(Entry\.composite_score\)", body)
    assert re.search(r"Entry\.fetched_at\s*>=", body)
