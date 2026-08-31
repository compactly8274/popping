"""entries: flatten nested meta->meta into top-level keys

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-31

Data migration for the ``validate_required`` meta-flattening fix.

Before the fix, ``app.sources.base.validate_required`` bucketed every
unknown top-level key of a plugin's raw item into ``Entry.meta`` —
including the plugin's own ``meta`` dict. Plugins that ship a nested
``meta`` (HN, NVD, CISA KEV, GitHub releases, Wikipedia, dynamic
Reddit) therefore landed their source-specific keys as
``meta->'meta'->key`` while every consumer read ``meta->'key'``:

  - ``app.scoring.engagement`` reads ``engagement_score`` /
    ``engagement_comments`` → HN/Reddit engagement contributed 0 to
    ``composite_score`` (25% of the blend weight dead).
  - ``scheduler._cvss_score`` reads ``cvss_score`` → the post-ingest
    high-CVSS CVE notification scored every CVE as 0.0.
  - the routes layer projects ``meta ->> 'reddit_thread_url'`` → the
    "Discussed on Reddit" card footer never rendered for
    Reddit-source entries.
  - ``brief_filter.extract_summary`` reads ``summary`` / ``extract`` /
    ``text`` etc. at the top level → the brief prompt lost the blurb
    for every nested-meta source.

The code fix flattens the nested dict at normalize time; this
migration repairs the rows already in the database. Entries are
deduped by URL, so historical rows are never re-ingested — without
this migration the consumers above stay dead for all pre-fix data
even after the code fix.

Merge semantics match the new normalize exactly: the nested
``meta->'meta'`` keys win on collision (``||`` is right-merge),
matching ``dict.update(plugin_meta)`` in the fixed
``validate_required``. The outer ``meta`` key is dropped after the
merge (``meta - 'meta'`` first, then ``||``), so the nested dict
doesn't survive as a stale ``meta->'meta'`` blob.

Rows whose ``meta->'meta'`` is NOT a jsonb object (no plugin ships
one; defensive shape) are left untouched — the code path preserves
those values verbatim and so does this migration.

Not reversible: after the merge we can't distinguish "pre-fix
nested meta" from "a source that legitimately shipped a key named
``meta``" (the defensive case), so ``downgrade`` is a no-op. The
pre-fix shape is recoverable from backups; the post-fix shape is
the canonical one going forward.
"""

from __future__ import annotations

from alembic import op

# Revision identifiers, used by Alembic.
revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One UPDATE over the entries table. jsonb ops are pure functions
    # over the stored value — no index churn (meta has a GIN index from
    # migration 0014, but this is a row-by-row rewrite, not a query
    # pattern the index serves; the GIN rebuild cost is bounded by the
    # same one-time pass). At personal-dashboard scale (tens of
    # thousands of rows) this runs in seconds.
    op.execute(
        """
        UPDATE entries
        SET meta = (meta - 'meta') || (meta -> 'meta')
        WHERE meta ? 'meta'
          AND jsonb_typeof(meta -> 'meta') = 'object'
        """
    )


def downgrade() -> None:
    # Not reversible — see the module docstring for the rationale.
    # Intentionally a no-op rather than a best-effort re-nest (which
    # would corrupt any source that legitimately shipped a top-level
    # ``meta`` key before this migration existed).
    pass