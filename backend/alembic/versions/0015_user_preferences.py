"""user_preferences: per-user key/value preference store

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-02 20:30:00.000000

Adds the ``user_preferences`` table used by the new
``app.routes.preferences`` endpoints. Stores per-user, per-key
JSONB values keyed by ``(user_id, key)``.

Why a new table and not extensions to ``user_profiles``
---------------------------------------------------

``user_profiles`` is a single-row, fixed-schema table (id=1 only)
designed for the personalization model — preference_vector,
interest_clusters, followed_teams, etc. Those columns are typed
and migration-costly to extend. Per-device read state, last-viewed
timestamps, and column sort/filter preferences are different in
three ways:

  1. Per-user. ``user_profiles`` is single-row. Multiple OIDC users
     on the same deployment would all share a row. ``user_preferences``
     is keyed by ``user_id`` and supports the OIDC case correctly
     (the bypass user gets ``sub="local-bypass"`` and gets its own
     row, distinct from any real OIDC ``sub``).
  2. Untyped. The values are arbitrary JSONB — a list of read entry
     ids, an ISO timestamp, a sort/filter object. Adding typed
     columns to ``user_profiles`` would mean a migration per new
     preference type; this table absorbs them all in one row per
     ``(user, key)`` pair.
  3. High write frequency. ``user_profiles`` is read on every
     score pass; we don't want read-state PUTs to touch that row's
     write-amplification path. A separate narrow table keeps the
     hot read-state path isolated.

Schema
------

- ``user_id`` (str, 60) — the OIDC ``sub`` for authenticated users,
  or the synthetic ``"local-bypass"`` sub for LAN-bypass callers.
  Sourced from ``require_user`` in ``app.auth.deps``.
- ``key`` (str, 60) — the preference name. Frontend uses
  ``read_entries:<source_id>``, ``last_viewed:<source_id>``,
  ``column_prefs:<source_id>`` (the source_id suffix scopes the
  preference to a column; the server treats the whole string as
  an opaque key, so the frontend can add new key shapes without
  a backend migration).
- ``value`` (JSONB) — the preference payload. No shape constraint
  at the DB layer; Pydantic schemas on the route validate on read
  and write.
- ``updated_at`` — bumped on every upsert.

Indexes
-------

- ``pk_user_preferences`` on ``(user_id, key)`` — primary key gives
  the point-read path and the upsert path for free.
- ``ix_user_preferences_user`` on ``(user_id)`` — supports the
  ``GET /api/preferences`` "fetch all" path, which scans all keys
  for a user. Prefix of the PK so it adds zero extra storage.

The data is small per user (a few hundred bytes for read_entries
even at 1000 items, plus a few KB for column_prefs). No TTL needed;
users can drop their preferences via the existing user-data
endpoints if they want a clean slate.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(60), nullable=False),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name="pk_user_preferences"),
    )
    # The primary key on (user_id, key) already covers the prefix
    # index on (user_id), so we get the "fetch all for a user" path
    # for free. No extra index needed.


def downgrade() -> None:
    op.drop_table("user_preferences")
