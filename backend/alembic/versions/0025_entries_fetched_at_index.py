"""entries.fetched_at index — hot query path

The ``entries.fetched_at`` column is the gate on at least four hot
query paths that already exist in production:

  1. ``brief.py:_select_entries`` — every brief generation
     (``SELECT ... WHERE fetched_at >= :since ORDER BY composite_score DESC``)
  2. ``scheduler.py:_rescore_recent_entries`` — every 5-minute rescore
     tick (``SELECT ... WHERE fetched_at >= :cutoff``)
  3. ``brief.py:_select_entries_by_slug`` — every convergence alert
     (``SELECT ... WHERE fetched_at >= :since``)
  4. ``scheduler.py:_recompute_preference_vector`` — every pref-vector
     recompute (``SELECT ... WHERE fetched_at >= :window_start``)

Before this migration, none of these queries used an index on
``fetched_at``. They each did a full table scan + in-memory sort
(``ORDER BY composite_score DESC`` on a column that IS indexed, but
the scan-to-filter on ``fetched_at`` ran first and dominated).

At homelab scale (30+ sources, ~5-15k recent rows on a heavy week)
the scans are noticeable — observed in CI logs as multi-second
``StatementTimeout`` warnings on the rescore path. The fix is a
single B-tree index on ``fetched_at DESC`` so the planner can do an
index range scan + join to ``composite_score`` for the sort.

Index choice: ``DESC`` because every query is
``fetched_at >= :since`` paired with an ``ORDER BY fetched_at DESC``
(most recent first). The DESC index lets the planner skip the
in-memory sort on the rescore and brief-select paths.

Indexing trade-off: writes are slightly more expensive (B-tree
maintenance on every INSERT/UPDATE of ``fetched_at``). The table
gets ~100 inserts per ingest cycle (every 5-15 min per source),
so the write cost is on the order of microseconds per insert.
The read win (multiple seconds per brief / rescore cycle) is
worth many orders of magnitude over the write cost.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain B-tree DESC index. Postgres can use a DESC index for both
    # ``WHERE fetched_at >= :since`` range scans AND
    # ``ORDER BY fetched_at DESC`` sort avoidance, so a single index
    # serves all four hot-query paths above. Partial index
    # (``WHERE fetched_at IS NOT NULL``) is not used — the column is
    # NOT NULL with a server_default, so every row qualifies.
    op.create_index(
        "ix_entries_fetched_at_desc",
        "entries",
        ["fetched_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_entries_fetched_at_desc", table_name="entries")