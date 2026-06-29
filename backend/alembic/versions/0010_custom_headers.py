"""sources.custom_headers — per-source HTTP header overrides

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-29 03:00:00.000000

Some sources block our default User-Agent (``Popping/0.2``) at the
edge — CBC's CDN reads the UA, sees a self-identifying scraper, and
drops the connection. The fix is per-source header overrides so the
user can swap the UA for just the affected feeds without changing
the global default.

Free-form JSON for now. The route layer validates ``str → str`` on
update (rejects nested values, reject-keys blocks a small denylist
of headers we don't want users to be able to set — ``Cookie``,
``Authorization``, ``Host``). Adding fine-grained validation later
just needs a regex on the key; the JSONB column won't change.

Why JSONB and not a separate column: a fixed-shape table can't grow
later, and a fixed set of headers (just UA today) would lock out
the ``Accept-Language`` / ``Referer`` overrides that future feeds
might need. One JSONB column stays out of the way.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "custom_headers",
            postgresql.JSONB,
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "custom_headers")
