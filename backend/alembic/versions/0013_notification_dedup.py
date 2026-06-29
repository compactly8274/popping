"""notification_dedup — single-row dedup ledger for alert paths

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-29 19:00:00.000000

The old dedup layout stored notified CVE URLs in
``Brief.meta.notified_urls`` on the *latest* Brief row, with the
reader unioning ``meta.notified_urls`` across **all** Brief rows.

The split between reader and writer broke: writes only ever touched
the latest row, while the reader assumed every row was a candidate.
After enough alerts the latest-row bucket got truncated with
``bucket[-500:]``, silently dropping the oldest URLs and re-firing
the same CVE whenever the entry re-surfaced.

The convergence slug dedup used the same Brief-row-as-ledger pattern
(``meta={"alert_slugs": [slug]}`` on a fresh Brief per slug). The
read-side union of ``alert_slugs`` across all rows is correct there,
but still does a full table scan on every check.

This migration replaces both ledgers with one normalized table:

    notification_dedup (kind, key PRIMARY KEY, last_notified_at)

    - ``kind`` is a small text discriminator: ``'cve_url'`` for CVE
      URL dedup, ``'convergence_slug'`` for convergence alerts. New
      kinds can be added without a schema change.
    - ``key`` is the actual dedup token (a URL or a slug string).
      PK constraint means INSERT … ON CONFLICT DO NOTHING is atomic
      and dedup-correct.
    - ``last_notified_at`` exists so future "garbage-collect rows
      older than N days" maintenance has a column to filter on. We
      don't prune in this migration.

Pure additive. Existing Brief.meta.alert_slugs /
Brief.meta.notified_urls rows are left intact for the brief history
view; the read paths are swapped over to the new table and the old
write paths are removed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_dedup",
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "last_notified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("kind", "key", name="pk_notification_dedup"),
    )
    # Index on (kind, last_notified_at) so a future
    # "DELETE FROM notification_dedup WHERE last_notified_at < ..."
    # maintenance query doesn't table-scan.
    op.create_index(
        "ix_notification_dedup_kind_time",
        "notification_dedup",
        ["kind", sa.text("last_notified_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_dedup_kind_time", table_name="notification_dedup")
    op.drop_table("notification_dedup")