"""entries composite index (composite_score DESC, fetched_at)

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-01

Composite index for the fetch-and-sort hot paths that walk

    ORDER BY composite_score DESC [WHERE fetched_at >= :since]

  1. ``/api/foryou`` candidate fetch (500 rows, no time filter)
  2. ``brief.py:_select_entries`` (every brief generation,
     fetched_at-filtered)
  3. ``brief.py:_select_entries_by_slug`` (every convergence alert,
     fetched_at-filtered)

Migration 0026 added ``(fetched_at, composite_score DESC)`` for the
same paths, but live EXPLAIN runs against the production-shaped
queries show the planner never selects it: the leading column
(``fetched_at``) doesn't match the sort, and the one path with no
time predicate at all (``/api/foryou``) gives the leading column
nothing to filter on. The planner falls back to
``ix_entries_composite_score`` (migration 0003) for the sort and
pays a re-check on the fetched_at predicate where one exists.

This index leads with the sort column instead:

- ``(composite_score DESC, fetched_at)`` -- the leading column
  matches ``ORDER BY composite_score DESC`` so the scan starts at
  the high-score end; the trailing ``fetched_at`` column lets the
  brief paths' ``fetched_at >= :since`` predicate prune inside the
  index walk instead of re-checking heap tuples.

Write cost: ``composite_score`` is updated by the rescore tick and
at ingest, so this index takes B-tree maintenance on those writes
-- the same order-of-magnitude cost 0026 already pays. The read
win is the eliminated re-sort on the hottest dashboard path.

0026 is deliberately left in place in this migration: it still
serves pure fetched_at range scans (e.g. the rescore tick's
filter with no sort). Dropping it is a separate, low-urgency
cleanup flagged by the audit -- see the PR notes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_entries_composite_score_fetched_at",
        "entries",
        [sa.text("composite_score DESC"), "fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_entries_composite_score_fetched_at", table_name="entries")
