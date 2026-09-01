"""Slice 25 -- ``Entry.fetched_at`` index for hot query paths.

sweep2 REPLACEMENT (0002-c): the false-green fourth guard is removed
and every remaining path-guard span is bounded to a true function
boundary.

Three hot query paths filter on ``entries.fetched_at``:

  1. brief.py:_select_entries -- brief generation
  2. scheduler.py:_rescore_recent_entries -- the periodic rescore
  3. brief.py:_select_entries_by_slug -- convergence alerts

Migration 0025's docstring claims a fourth path,
``scheduler.py:_recompute_preference_vector``. That claim is false:
the recompute windows on ``Interaction.created_at`` (when the user
interacted), never on ``Entry.fetched_at``, so no fetched_at index
serves it. The original fourth guard was false-green -- its lazy
``(?=^def |\\Z)`` span ran past the end of the ``async def``
function (``^def `` doesn't match ``async def``) into
``_rescore_recent_entries``, whose own ``fetched_at >= cutoff``
satisfied the assertion -- so it verified nothing. It is removed
here rather than kept as a lie about the code.

Span hardening (the same defect, fixed everywhere it appeared):
- the scheduler guard now stops at the next module-level ``def`` OR
  ``async def`` -- the old ``^def``-only lookahead is what let the
  span leak across three unrelated functions;
- the brief.py guards stop at the next method boundary inside
  ``BriefGenerator`` instead of running to end-of-file.
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
# 1. Model has index=True on Entry.fetched_at
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_entry_fetched_at_column_has_index_true():
    src = MODELS.read_text()
    # Find the Entry class block
    m = re.search(r"^class Entry\(Base\):[\s\S]*?(?=^class )", src, re.MULTILINE)
    assert m, "Couldn't locate Entry class in models.py"
    block = m.group(0)
    # Find the fetched_at column declaration -- match from the
    # ``fetched_at:`` annotation line through the closing paren of
    # ``mapped_column(...)``.
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
        "alembic/versions/0025_entries_fetched_at_index.py must exist -- "
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
        "downgrade() must call op.drop_index(...) -- symmetric with upgrade."
    )
    assert "ix_entries_fetched_at_desc" in src, (
        "downgrade must target the same index name as upgrade created."
    )


# ---------------------------------------------------------------------------
# 3. Hot query paths still filter on fetched_at (regression guards,
#    function-bounded spans)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_brief_select_entries_filters_on_fetched_at():
    """brief.py:_select_entries must filter on ``fetched_at >= ...``.
    Bounded to the method -- the original lazy span ran to
    end-of-file."""
    body = _method(BRIEF.read_text(), "_select_entries")
    assert re.search(r"fetched_at\s*>=", body), (
        "_select_entries must filter on ``fetched_at >= ...`` -- "
        "this is a query path the 0025 index serves."
    )


@pytest.mark.no_db
def test_scheduler_rescore_filters_on_fetched_at():
    """scheduler.py:_rescore_recent_entries must filter on
    ``fetched_at >= cutoff``. Bounded to the function -- the
    original ``(?=^def |\Z)`` span leaked past the (``async def``)
    function into three unrelated ones."""
    body = _module_func(SCHEDULER.read_text(), "_rescore_recent_entries")
    assert re.search(r"fetched_at\s*>=", body), (
        "_rescore_recent_entries must filter on fetched_at >= cutoff."
    )


@pytest.mark.no_db
def test_brief_slug_filter_filters_on_fetched_at():
    """brief.py:_select_entries_by_slug must filter on
    ``fetched_at >= ...`` -- the convergence-alert query path.
    Bounded to the method."""
    body = _method(BRIEF.read_text(), "_select_entries_by_slug")
    assert re.search(r"fetched_at\s*>=", body), (
        "_select_entries_by_slug must filter on fetched_at >= ..."
    )


# NOTE: there is deliberately NO guard for
# scheduler._recompute_preference_vector here. Migration 0025's
# docstring lists it as a fourth hot path, but the recompute windows
# on Interaction.created_at, not Entry.fetched_at -- no fetched_at
# index serves it, and the original guard was false-green (see the
# module docstring). Adding a fetched_at filter to the recompute
# would be a behavior change, not something to pin here.