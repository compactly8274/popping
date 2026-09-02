"""Regression test for a bug found in a repo-wide audit:
``scheduler._crossref_sweep``'s candidate scan fetched the
``_CROSSREF_BATCH * 4`` (200) most-recent-by-id entries unconditionally,
then filtered "already stamped" / "Reddit-sourced" in Python. On any
install where the 200 most-recent entries have mostly already been
checked (the steady-state case after the first sweep, or simply a
burst of Reddit-sourced ingest), the SQL-level LIMIT was spent
entirely on rows the Python loop immediately discarded — genuinely
unchecked (but older) entries could sit just outside that window and
never get checked at all, even once, no matter how many sweeps ran.

Migration 0014 added a GIN index specifically so ``NOT (meta ?
'reddit_thread_url')`` could run as an indexed SQL predicate — its own
docstring names this sweep as "the only consumer" — but the query
never actually used it. The fix pushes both the "not yet stamped" and
"not a Reddit source" filters into the SQL WHERE clause, so every row
LIMIT 200 returns is a genuine candidate.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app import reddit_client, scheduler
from factories import make_entry, make_source

pytestmark = pytest.mark.asyncio


async def test_candidate_scan_skips_stamped_entries_to_reach_older_unstamped_ones(db_session):
    """The core regression: 200 already-stamped (higher-id, more
    recent) entries must not crowd out a handful of older, genuinely
    unstamped entries from the candidate set."""
    source = await make_source(db_session, "crossref_source", type="rss")

    # A handful of OLDER (lower-id — created first, so they sort
    # after the higher-id stamped batch below in an id-descending
    # scan) genuinely unstamped entries.
    unstamped_entries = [
        await make_entry(db_session, source, f"Unstamped {i}", url=f"https://news.example.com/{i}")
        for i in range(3)
    ]
    await db_session.commit()

    # 200 MORE RECENT entries (higher id — created after the ones
    # above), already stamped. These must not appear in the candidate
    # set, and must not consume the LIMIT budget ahead of the older,
    # genuine candidates created above.
    for i in range(scheduler._CROSSREF_BATCH * 4):
        stamped = await make_entry(db_session, source, f"Stamped {i}")
        stamped.meta = {"reddit_thread_url": "https://www.reddit.com/r/test/comments/x/y/"}
        db_session.add(stamped)
    await db_session.commit()

    seen_urls: list[str] = []

    async def fake_search(url):
        seen_urls.append(url)
        return None

    with patch.object(reddit_client, "is_disabled", lambda: False), \
         patch.object(reddit_client, "_crossref_cache", {"test": (0.0, [])}), \
         patch.object(reddit_client, "search_thread_by_url", new=AsyncMock(side_effect=fake_search)):
        await scheduler._crossref_sweep()

    assert set(seen_urls) == {e.url for e in unstamped_entries}


async def test_candidate_scan_excludes_reddit_sourced_entries(db_session):
    reddit_source = await make_source(db_session, "crossref_reddit_source", type="reddit")
    rss_source = await make_source(db_session, "crossref_rss_source", type="rss")

    reddit_entry = await make_entry(
        db_session, reddit_source, "A Reddit thread", url="https://www.reddit.com/r/test/comments/1/a/"
    )
    rss_entry = await make_entry(
        db_session, rss_source, "An RSS article", url="https://news.example.com/article"
    )

    seen_urls: list[str] = []

    async def fake_search(url):
        seen_urls.append(url)
        return None

    with patch.object(reddit_client, "is_disabled", lambda: False), \
         patch.object(reddit_client, "_crossref_cache", {"test": (0.0, [])}), \
         patch.object(reddit_client, "search_thread_by_url", new=AsyncMock(side_effect=fake_search)):
        await scheduler._crossref_sweep()

    assert reddit_entry.url not in seen_urls
    assert rss_entry.url in seen_urls


async def test_candidate_scan_treats_null_meta_as_unstamped(db_session):
    """Entries with meta=NULL (never touched by anything that writes
    meta) must still be treated as candidates — the SQL predicate
    must COALESCE, not silently exclude NULL rows (NULL ? 'key'
    evaluates to NULL in Postgres, which a bare WHERE treats as
    "no match")."""
    source = await make_source(db_session, "crossref_null_meta_source", type="rss")
    entry = await make_entry(db_session, source, "No meta at all", url="https://news.example.com/nullmeta")
    assert entry.meta is None

    seen_urls: list[str] = []

    async def fake_search(url):
        seen_urls.append(url)
        return None

    with patch.object(reddit_client, "is_disabled", lambda: False), \
         patch.object(reddit_client, "_crossref_cache", {"test": (0.0, [])}), \
         patch.object(reddit_client, "search_thread_by_url", new=AsyncMock(side_effect=fake_search)):
        await scheduler._crossref_sweep()

    assert entry.url in seen_urls
