"""entries.reddit_comment_summary — LLM summary of a Reddit thread's
comment discussion, cached on first read

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-24 00:40:00.000000

Entries whose ``meta.reddit_thread_url`` is set (stamped by the
cross-reference sweep — see ``app.scheduler``) get their thread's
top-level comments fetched via Reddit's ``.rss`` comment feed and
summarized by the configured LLM provider on the first "Summarize
comments" tap. Same NULL / empty-string / populated cache contract as
``cached_summary`` (0011) / ``podcast_transcript_summary`` (0016) —
another sibling column, since this caches yet another distinct kind
of text (a discussion summary, not the article itself).

Pure additive change — no backfill, no rewrite of existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column("reddit_comment_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entries", "reddit_comment_summary")
