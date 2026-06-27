"""auth: sessions table for DB-backed sessions

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-27 12:00:00.000000

Adds the ``sessions`` table used by ``app.auth.session``. The cookie
holds only an opaque random ID; user data + expiry live here so a
backend restart doesn't log everyone out.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("sub", sa.String(120), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("auth_method", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column(
            "last_used_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_sessions_sub", "sessions", ["sub"])
    op.create_index("ix_sessions_last_used_at", "sessions", ["last_used_at"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_last_used_at", table_name="sessions")
    op.drop_index("ix_sessions_sub", table_name="sessions")
    op.drop_table("sessions")