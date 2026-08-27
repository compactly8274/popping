"""entries composite index (fetched_at, composite_score DESC)

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-27 20:40:00.000000

Composite index for the three hot query paths that do
``WHERE fetched_at >= :since ORDER BY composite_score DESC``:

  1. ``/api/foryou`` candidate fetch (500 rows)
  2. ``brief.py:_select_entries`` (500 rows, every brief generation)
  3. ``brief.py:_select_entries_by_slug`` (30 rows, every convergence alert)

Single-column indexes already exist on both columns:
  - ``ix_entries_fetched_at_desc`` (migration 0025)
  - the implicit ``ix_entries_composite_score`` (migration 0003, via
    ``composite_score`` ``index=True`` on the model)

But Postgres can't combine two single-column indexes for a
filter+sort: it picks one for the range scan, then does an explicit
Sort on the other column.  The composite index lets the planner do
a single index range scan that returns rows already sorted by
``composite_score DESC``, eliminating the Sort step entirely.

Index column order: ``fetched_at`` first (the range predicate), then
``composite_score DESC`` (the sort).  This is the standard
"range-then-sort" composite index pattern — the leading column
narrows via the WHERE clause, and the trailing column(s) provide the
ORDER BY ordering within each fetched_at bucket.

Write cost: one B-tree maintenance on every INSERT/UPDATE of
``fetched_at`` or ``composite_score``.  ``fetched_at`` is set once at
INSERT and never updated; ``composite_score`` is updated by the
rescore tick (every 5-10 min) and at ingest.  The write cost is on
the order of microseconds per row — negligible compared to the read
win of eliminating an explicit Sort on 500+ rows.

The existing single-column indexes are kept: the composite_score-only
index still serves queries that sort by composite_score without a
fetched_at filter (e.g. ``/api/entries`` without a time window), and
the fetched_at-only index still serves queries that filter by
fetched_at without sorting by composite_score (e.g. the rescore
tick's ``WHERE fetched_at >= :cutoff`` with no ORDER BY).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_entries_fetched_at_composite_score",
        "entries",
        ["fetched_at", sa.text("composite_score DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_entries_fetched_at_composite_score", table_name="entries")