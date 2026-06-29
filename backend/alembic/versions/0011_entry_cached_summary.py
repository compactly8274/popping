"""entries.cached_summary — per-entry summary cache populated on first read

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-29 06:00:00.000000

The frontend's per-card summary expansion (chevron tap) needs a
text body under each card. The source feed already ships ``summary``
in ``Entry.meta.summary`` at ingest time, but the meta field is
HTML, can be very long, and reading it requires stripping + a
length cap on every request.

Cache the cleaned text on the row so:
  1. Subsequent reads are a column fetch (no regex / truncate).
  2. The same article re-ingested across versions yields the same
     answer (single source of truth).
  3. A future LLM-summary path can populate this same column
     without another migration.

NULL means "user hasn't asked yet" (no extra work to do). The route
distinguishes NULL from empty string when surfacing "no summary
available" vs "summary requested but feed shipped nothing".

Pure additive change — no backfill, no rewrite of existing rows.
Existing entries land with ``cached_summary = NULL``; the first
chevron tap populates it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # TEXT (not VARCHAR(N)) so we don't have to migrate the column
    # later if the length cap moves. The cap lives at the application
    # boundary (the route handler) rather than as a schema invariant.
    op.add_column(
        "entries",
        sa.Column("cached_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entries", "cached_summary")