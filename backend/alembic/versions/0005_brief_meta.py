"""briefs.meta — JSON bag for dedup of notifications

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-28 12:00:00.000000

Phase 4: the Brief generator needs to dedup notifications (a high-CVSS
CVE that re-ingests should notify once; a convergence cluster that
re-fires should alert once). Storing ``notified_urls`` / ``alert_slugs``
as JSON lets us query ``WHERE meta @> '{"notified_urls": [<url>]}'``
cheaply with a GIN index, without a separate dedup table.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use JSONB (not JSON). Bare ``json`` has no default GIN operator
    # class — Postgres would reject ``CREATE INDEX ... USING gin (meta)``
    # on it with "data type json has no default operator class". JSONB
    # ships with ``jsonb_ops`` as the GIN default, so the containment
    # queries in app/scheduler.py (``meta @> '{"notified_urls": [...]}'
    # ::jsonb``) can use this index without naming an opclass.
    op.add_column("briefs", sa.Column("meta", sa.dialects.postgresql.JSONB, nullable=True))
    op.create_index(
        "ix_briefs_meta",
        "briefs",
        ["meta"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_briefs_meta", table_name="briefs")
    op.drop_column("briefs", "meta")
