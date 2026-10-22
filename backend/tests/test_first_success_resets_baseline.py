"""A feed's baseline (``sources.created_at``) moves to the first fetch
that actually stores a story, and only then.

The baseline is what ``_is_first_fetch`` checks stored rows against,
and what the later-poll rules measure "older than the first fetch's
backfill" from. It has to be the exact ``now()`` of that poll's
transaction, the ``fetched_at`` its rows got, or the next poll would
be treated as a first fetch again."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from app import scheduler
from app.models import Entry
from factories import make_source
from ingest_harness import (
    age_feed,
    item,
    make_plugin,
    source_row,
)


async def _added_days_ago(db_session, plugin, days: int):
    """The feed's row, as if added ``days`` ago and never polled."""
    await make_source(db_session, plugin.name, category=plugin.category, url=plugin.url)
    await age_feed(db_session, plugin.name, hours=days * 24)
    return (await source_row(db_session, plugin.name)).created_at


async def test_first_fetch_that_stores_a_story_moves_the_baseline(db_session, offline_ingest):
    plugin = make_plugin("bl_first_store")
    added = await _added_days_ago(db_session, plugin, 5)
    plugin.items = [item(n, hours_ago=1 + n) for n in range(3)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 3

    row = await source_row(db_session, plugin.name)
    assert row.created_at > added
    fetched = await db_session.scalars(select(Entry.fetched_at).where(Entry.source_id == row.id))
    # Exactly the rows' fetched_at: same transaction, same now().
    assert set(fetched.all()) == {row.created_at}


async def test_empty_first_poll_leaves_the_baseline(db_session, offline_ingest):
    plugin = make_plugin("bl_empty_first")
    added = await _added_days_ago(db_session, plugin, 5)
    plugin.items = []
    summary = await scheduler._ingest(plugin)
    assert summary["error"] is None

    row = await source_row(db_session, plugin.name)
    assert row.created_at == added
    assert row.last_fetch_at is not None


async def test_first_poll_of_only_known_stories_leaves_the_baseline(db_session, offline_ingest):
    # Every story is already stored (under another feed): nothing is
    # inserted, so nothing would satisfy the baseline check.
    other = make_plugin("bl_other_feed")
    other.items = [item(n, hours_ago=1 + n) for n in range(3)]
    await scheduler._ingest(other)

    plugin = make_plugin("bl_only_known")
    added = await _added_days_ago(db_session, plugin, 5)
    plugin.items = list(other.items)
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 0 and summary["duplicates"] == 3

    row = await source_row(db_session, plugin.name)
    assert row.created_at == added


async def test_later_poll_leaves_the_baseline(db_session, offline_ingest):
    plugin = make_plugin("bl_later")
    plugin.items = [item(0, hours_ago=1)]
    await scheduler._ingest(plugin)
    baseline = (await source_row(db_session, plugin.name)).created_at

    plugin.items = [item(1, hours_ago=0.5), item(0, hours_ago=1)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 1
    assert (await source_row(db_session, plugin.name)).created_at == baseline


async def test_after_a_first_fetch_the_next_poll_is_not_a_first_fetch(db_session, offline_ingest):
    """The baseline written by the first fetch has to match its own rows:
    a second poll right after gets the later-poll rules (old backlog
    stays out instead of another newest-10)."""
    plugin = make_plugin("bl_next_poll")
    plugin.items = [item(n, hours_ago=1 + n * 24) for n in range(30)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 10

    second = await scheduler._ingest(plugin)
    assert second["inserted"] == 0
    assert second["duplicates"] == 10
    assert second["skipped"] == 20

    count = await db_session.scalar(
        select(func.count()).select_from(Entry).where(
            Entry.source_id == (await source_row(db_session, plugin.name)).id
        )
    )
    assert count == 10


async def test_resumed_first_fetch_moves_the_baseline(db_session, offline_ingest):
    plugin = make_plugin("bl_resumed")
    plugin.items = [item(0, hours_ago=1)]
    await scheduler._ingest(plugin)
    await age_feed(db_session, plugin.name, hours=10 * 24)
    old_baseline = (await source_row(db_session, plugin.name)).created_at

    plugin.items = [item(1, hours_ago=1)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 1
    row = await source_row(db_session, plugin.name)
    assert row.created_at > old_baseline
    assert row.created_at > dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
