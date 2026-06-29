"""entries_meta_reddit_thread_url_index — GIN index on entries.meta for Reddit cross-ref key

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-29 19:30:00.000000

The hourly Reddit cross-reference sweep (``app.scheduler._crossref_sweep``)
queries for ``Entry`` rows whose ``meta`` does NOT yet contain the
``reddit_thread_url`` key, and writes it back via
``UPDATE entries SET meta = meta || :patch::jsonb`` when Hydra reports
a match.

For deployments with many ingested entries (tens of thousands), the
``NOT (meta ? 'reddit_thread_url')`` predicate runs as a full table
scan against the JSONB column. Adding a GIN index on ``meta`` lets
the planner use ``jsonb_ops`` for the key-existence check, which
collapses the scan from O(rows) to O(rows-without-the-key) on cold
deploys and stays cheap as the entries table grows.

The GIN index is additive: existing JSONB reads / writes still work
unmodified. The cross-ref sweep's "skip already-stamped" filter is the
only consumer that benefits today; future cross-source joins on the
``meta`` column (e.g. "show me everything tagged X") inherit the
index for free.

We use ``jsonb_path_ops`` rather than ``jsonb_ops`` because the only
operator we need is ``?`` (key existence). ``jsonb_path_ops`` is
smaller and faster for that single operator; ``jsonb_ops`` would
also support ``@>`` / ``<@`` if we ever need them, at the cost of
~2x index size. Trade goes to smaller since the existing dedup
patterns (CVE URL containment) use their own tables, not meta.

Idempotency note: a re-run on a deploy that already has the index is
a no-op (the op.create_index IF NOT EXISTS guard skips). The
migration is safe to re-run after a partial-failure state.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # GIN index on the entries.meta JSONB column. ``jsonb_path_ops``
    # is the smaller / faster operator class for key-existence
    # queries (``?`` operator); if we later add containment queries
    # (``@>``), drop and recreate with the default ``jsonb_ops``.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_entries_meta_gin "
        "ON entries USING GIN (meta jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_entries_meta_gin")