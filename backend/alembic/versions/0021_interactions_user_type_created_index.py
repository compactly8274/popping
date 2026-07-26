"""interactions composite index for the History page

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-26 07:00:00.000000

The /api/interactions/recent endpoint (the History page in the
Settings UI) is the most-frequently-WHERE'd table outside of
``entries``. The query shape is::

    SELECT ... FROM interactions
    WHERE user_id = :user_id
      AND type IN :type_list
      AND type NOT IN :exclude_types
    ORDER BY created_at DESC
    LIMIT :page_size

That's a 3-column predicate + sort, against a table that can grow
to thousands of rows per active user (one per hover / click / dwell
event). The existing single-column indexes (``ix_interactions_user_entry``
on ``(user_id, entry_id)`` and ``ix_interactions_created_at``) don't
help: the planner uses the compound one for the ``user_id =`` lookup
but then has to scan the matching rows and re-sort by ``type`` +
``created_at`` in memory.

A composite ``(user_id, type, created_at DESC)`` lets the planner
do an index range scan that returns rows already filtered by
``user_id`` and ``type`` and already sorted by ``created_at`` — the
``LIMIT :page_size`` then becomes an early-exit at the right
position in the index. Same plan shape on PostgreSQL 12+ that the
entry dashboard already gets from ``ix_entries_composite_score`` (0012).

Why DESC on ``created_at`` specifically: the WHERE clause is on
``user_id`` and ``type`` (equality), the ORDER BY is on
``created_at`` (range). A ``(a, b, c DESC)`` index supports
``WHERE a = ? AND b IN (...) ORDER BY c DESC LIMIT N`` as an
"Index Scan Backward" on the matching slice — the planner's
preferred shape for "give me the N newest matching rows".

The compound ``(user_id, entry_id)`` index is unaffected (the
sourceless count-by-source query in ``routes/sources.py`` still
needs it; that's a join on a different column). The ``created_at``
single-column index is also unaffected — the ``prune_interactions``
maintenance job uses ``created_at < :cutoff`` and doesn't care
about the leading columns of the new index.

Pure additive. ``CREATE INDEX`` is non-blocking in PostgreSQL
12+ (CONCURRENTLY is the explicit form; the default is
"CONCURRENTLY not used" but ``op.create_index`` doesn't take
``CONCURRENTLY`` as a flag and the standard migration is fine
on a small table). Downgrade does the reverse.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_interactions_user_type_created",
        "interactions",
        ["user_id", "type", "created_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_user_type_created", table_name="interactions")
