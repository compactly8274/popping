"""tz-aware datetime columns

Phase 1 created all DateTime columns as naive (`TIMESTAMP WITHOUT TIME
ZONE`). Python code consistently passes tz-aware datetimes (UTC, from
the ingest path). Postgres rejects the mismatch with
"can't subtract offset-naive and offset-aware datetimes".

Fix: ALTER every user-supplied datetime column to ``TIMESTAMP WITH TIME
ZONE``. Existing naive values are reinterpreted as UTC by Postgres —
which matches what the app has been doing on insert anyway, so no data
loss.

Server-defaulted columns (``created_at``, ``fetched_at``, ``updated_at``)
get the same treatment for consistency and to avoid the same bug class
when adding new code paths.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column[, ...]) — one ALTER per column keeps the migration
# reviewable and lets us drop individual columns on downgrade.
_TZ_COLUMNS: list[tuple[str, str]] = [
    ("sources", "last_fetch_at"),
    ("sources", "created_at"),
    ("entries", "published_at"),
    ("entries", "fetched_at"),
    ("entries", "expires_at"),
    ("interactions", "created_at"),
    ("watchlist_items", "last_checked_at"),
    ("watchlist_items", "last_notified_at"),
    ("watchlist_items", "created_at"),
    ("user_profiles", "updated_at"),
    ("sessions", "created_at"),
    ("sessions", "last_used_at"),
    ("sessions", "expires_at"),
    ("briefs", "generated_at"),
    ("briefs", "delivered_at"),
]


def upgrade() -> None:
    for table, column in _TZ_COLUMNS:
        # ``USING `` clause is implicit; Postgres treats existing naive
        # values as UTC, matching what the app has been writing.
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TIMESTAMP WITH TIME ZONE'
        )


def downgrade() -> None:
    # Reverse — TIMESTAMP WITH TIME ZONE → TIMESTAMP WITHOUT TIME ZONE.
    # Loses tz info but Postgres is happy to truncate.
    for table, column in reversed(_TZ_COLUMNS):
        op.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TIMESTAMP WITHOUT TIME ZONE'
        )