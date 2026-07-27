"""entries cached_summary retry window

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-27 00:30:00.000000

The /api/entries/{id}/summary endpoint caches its result in
``entries.cached_summary`` — NULL means "not asked yet", an
empty string means "asked, no usable text available". The
empty-string branch was previously permanent: once an entry
got an empty cache, the next chevron tap would return
``summary=""`` forever, even if the source later started
shipping body text, the article URL later became fetchable,
or the LLM provider became configured (all real scenarios
that the original 2026-07-24 review called out).

Add a ``cached_summary_fetched_at`` timestamp column (nullable
for back-compat — pre-migration rows just always retry) and
gate the cache on age: ``cached_summary == ""`` AND
``cached_summary_fetched_at`` was within
``cached_summary_retry_hours`` (default 24) means return
without retrying; older than that means re-run the fetch /
extract / LLM chain. The non-empty-string branch is unchanged
(once we have a real summary, keep it forever — re-summarizing
the same article would just churn the LLM budget).

Indexing: a single-row lookup by ``id`` is the existing query
shape (PK), so no extra index is needed here. The timestamp is
read in the same PK lookup that already runs.

The companion runtime change is in ``routes/entries.py``'s
``entry_summary_endpoint``: ``cached_summary_fetched_at`` is
now set on every cache-write (including the empty-string
write), and the cache-hit decision is mediated by a small
helper ``_should_retry_empty_cache`` so the rule is unit-
testable without a DB.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column(
            "cached_summary_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("entries", "cached_summary_fetched_at")