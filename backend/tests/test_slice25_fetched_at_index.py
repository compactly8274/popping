"""Slice 25 — ``Entry.fetched_at`` index for hot query paths.

Four hot query paths filter on ``entries.fetched_at``:

  1. brief.py:_select_entries — brief generation
  2. scheduler.py:_rescore_recent_entries — every 5-min rescore
  3. brief.py:_select_entries_by_slug — convergence alerts
  4. scheduler.py:_recompute_preference_vector — pref-vector recompute

Before slice 25, none used an index. Each was a full table scan +
in-memory sort, observed in CI logs as multi-second ``StatementTimeout``
warnings on the rescore path. The fix is a single B-tree index.

This test file guards:

- backend/app/models.py — Entry.fetched_at has ``index=True``
- alembic/versions/0025_entries_fetched_at_index.py — creates the
  index, downgrades cleanly, has the correct down_revision
- The four hot-query paths still issue ``WHERE fetched_at >= :since``
  (regression guard: a future refactor must not drop the filter)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root resolved from the test file location.
REPO = Path(__file__).resolve().parents[2]
MODELS = REPO / "backend/app/models.py"
MIGRATION = REPO / "backend/alembic/versions/0025_entries_fetched_at_index.py"
BRIEF = REPO / "backend/app/brief.py"
SCHEDULER = REPO / "backend/app/scheduler.py"


# ---------------------------------------------------------------------------
# 1. Model has index=True on Entry.fetched_at
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_entry_fetched_at_column_has_index_true():
    src = MODELS.read_text()
    # Find the Entry class block
    m = re.search(r"^class Entry\(Base\):[\s\S]*?(?=^class )", src, re.MULTILINE)
    assert m, "Couldn't locate Entry class in models.py"
    block = m.group(0)
    # Find the fetched_at column declaration — match from the
    # ``fetched_at:`` annotation line through the closing paren of
    # ``mapped_column(...)``. The closing paren is preceded by
    # ``nullable=False, index=True`` for our slice 25 form.
    m2 = re.search(
        r"fetched_at:\s*Mapped\[dt\.datetime\]\s*=\s*mapped_column\(",
        block,
    )
    assert m2, "Couldn't locate fetched_at column declaration"
    start = m2.start()
    # Walk forward counting parens until balanced (so we don't stop
    # at the nested ``DateTime(timezone=True)`` call).
    depth = 0
    end = start
    for i in range(m2.end(), len(block)):
        c = block[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                end = i + 1
                break
            depth -= 1
    decl = block[start:end]
    assert "index=True" in decl, (
        "Entry.fetched_at must declare index=True so that "
        "Base.metadata.create_all() (used by dev setups and tests) "
        "creates the index without requiring the alembic migration. "
        "Production still applies the migration explicitly via "
        "alembic upgrade head. Decl was:\n" + decl
    )


# ---------------------------------------------------------------------------
# 2. Migration exists, correct revision chain, creates + drops the index
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_migration_0025_exists():
    assert MIGRATION.exists(), (
        "alembic/versions/0025_entries_fetched_at_index.py must exist — "
        "the migration is the canonical way production gets the index."
    )


@pytest.mark.no_db
def test_migration_revision_chain():
    src = MIGRATION.read_text()
    assert re.search(r'^revision\s*=\s*"0025"', src, re.MULTILINE), (
        "Migration must declare ``revision = '0025'`` (matches filename)."
    )
    assert re.search(r'^down_revision\s*=\s*"0024"', src, re.MULTILINE), (
        "Migration must declare ``down_revision = '0024'`` to chain "
        "correctly after the sources.link_pattern migration."
    )


@pytest.mark.no_db
def test_migration_upgrade_creates_index():
    src = MIGRATION.read_text()
    assert "op.create_index(" in src, (
        "upgrade() must call op.create_index(...)."
    )
    # The index must be on entries(fetched_at)
    assert re.search(r'op\.create_index\(\s*"ix_entries_fetched_at_desc"', src), (
        "Index name must be ``ix_entries_fetched_at_desc`` so it shows up "
        "identifiably in ``\\d entries`` output and in pg_stat_user_indexes."
    )
    assert re.search(r'"entries"', src), "Index must be on the entries table."
    assert re.search(r'\["fetched_at"\]', src), "Index must be on the fetched_at column."


@pytest.mark.no_db
def test_migration_downgrade_drops_index():
    src = MIGRATION.read_text()
    assert "op.drop_index(" in src, (
        "downgrade() must call op.drop_index(...) — symmetric with upgrade."
    )
    assert "ix_entries_fetched_at_desc" in src, (
        "downgrade must target the same index name as upgrade created."
    )


# ---------------------------------------------------------------------------
# 3. Hot query paths still filter on fetched_at (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_brief_select_entries_filters_on_fetched_at():
    """brief.py:_select_entries (or its inlined equivalent) must
    filter on ``fetched_at >= :since``. Slice 25 adds the index;
    a future refactor that drops the filter would render the
    index useless."""
    src = BRIEF.read_text()
    # Search the function body for the fetch-at filter
    m = re.search(
        r"def _select_entries[\s\S]*?(?=^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m, "Couldn't locate _select_entries in brief.py"
    body = m.group(0)
    assert re.search(r"fetched_at\s*>=", body), (
        "_select_entries must filter on ``fetched_at >= ...`` — "
        "this is the query path the new index serves."
    )


@pytest.mark.no_db
def test_scheduler_rescore_filters_on_fetched_at():
    """scheduler.py:_rescore_recent_entries must filter on
    ``fetched_at >= cutoff``."""
    src = SCHEDULER.read_text()
    m = re.search(
        r"def _rescore_recent_entries[\s\S]*?(?=^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m, "Couldn't locate _rescore_recent_entries in scheduler.py"
    body = m.group(0)
    assert re.search(r"fetched_at\s*>=", body), (
        "_rescore_recent_entries must filter on fetched_at >= cutoff."
    )


@pytest.mark.no_db
def test_scheduler_preference_vector_filters_on_fetched_at():
    """scheduler.py:_recompute_preference_vector (or its inlined
    equivalent) must filter on ``fetched_at >= :window_start`` —
    another query path the index serves.
    """
    src = SCHEDULER.read_text()
    m = re.search(
        r"def _recompute_preference_vector[\s\S]*?(?=^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m, "Couldn't locate _recompute_preference_vector in scheduler.py"
    body = m.group(0)
    assert re.search(r"fetched_at\s*>=", body), (
        "_recompute_preference_vector must filter on fetched_at >= ..."
    )


@pytest.mark.no_db
def test_brief_slug_filter_filters_on_fetched_at():
    """brief.py:_select_entries_by_slug must filter on
    ``fetched_at >= :since`` — the convergence-alert query path.
    """
    src = BRIEF.read_text()
    m = re.search(
        r"def _select_entries_by_slug[\s\S]*?(?=^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m, "Couldn't locate _select_entries_by_slug in brief.py"
    body = m.group(0)
    assert re.search(r"fetched_at\s*>=", body), (
        "_select_entries_by_slug must filter on fetched_at >= ..."
    )