"""Re-pointing a feed starts it over from a first fetch, and a poll of
the old address that is still in flight stores nothing.

``update_source`` resets ``last_fetch_at`` and the baseline
(``created_at``) when ``url``, ``sitemap_url`` or ``link_pattern``
changes, so the new address brings its newest few stories plus the last
couple of days, not its whole back catalogue. ``_ingest``'s commit
session and ``update_source`` take the same row lock, so the two never
interleave: a change that lands mid-fetch discards that batch, and a
change that waits on a commit puts its baseline after that commit's
rows."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select, text

from app import scheduler
from app.db import SessionLocal
from app.models import Entry, Source
from app.sources.dynamic_rss import DynamicRssPlugin
from ingest_harness import (
    age_feed,
    entry_count,
    item,
    make_plugin,
    source_row,
)


@pytest.fixture(autouse=True)
def _no_live_scheduler(monkeypatch):
    # ``update_source`` returns right after its commit when no
    # scheduler is running, so no job is added or fetch started.
    monkeypatch.setattr(scheduler, "_scheduler", None)


async def _polled_feed(db_session, plugin, *, days_ago: int = 10):
    """A feed with stored stories, added and last polled long enough
    ago that only the reset can make its next poll a first fetch."""
    plugin.items = [item(n, hours_ago=1 + n) for n in range(3)]
    assert (await scheduler._ingest(plugin))["inserted"] == 3
    await age_feed(db_session, plugin.name, hours=days_ago * 24, last_fetch_hours=1)
    return await source_row(db_session, plugin.name)


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://moved.test/feed"},
        {"sitemap_url": "https://moved.test/sitemap.xml"},
        {"link_pattern": "/news/"},
    ],
    ids=["url", "sitemap_url", "link_pattern"],
)
async def test_repointing_resets_last_fetch_and_baseline(db_session, offline_ingest, change):
    plugin = make_plugin(f"rp_{next(iter(change))}")
    before = await _polled_feed(db_session, plugin)
    old_baseline = before.created_at

    async with SessionLocal() as session:
        row = await scheduler.update_source(session, before.id, **change)
    assert row.last_fetch_at is None
    assert row.created_at > old_baseline
    assert row.created_at > dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "rp_renamed"},
        {"refresh": 900},
        {"category": "tech"},
        {"custom_headers": {"User-Agent": "x"}},
        {"active": False},
    ],
    ids=["name", "refresh", "category", "headers", "active"],
)
async def test_other_edits_keep_last_fetch_and_baseline(db_session, offline_ingest, change):
    plugin = make_plugin(f"rp_keep_{next(iter(change))}")
    before = await _polled_feed(db_session, plugin)

    async with SessionLocal() as session:
        row = await scheduler.update_source(session, before.id, **change)
    assert row.last_fetch_at == before.last_fetch_at
    assert row.created_at == before.created_at


async def test_same_url_is_not_a_change(db_session, offline_ingest):
    plugin = make_plugin("rp_same_url")
    before = await _polled_feed(db_session, plugin)
    async with SessionLocal() as session:
        row = await scheduler.update_source(
            session, before.id, url=before.url, sitemap_url=None, link_pattern=None
        )
    assert row.last_fetch_at == before.last_fetch_at
    assert row.created_at == before.created_at


async def test_edit_from_a_stale_read_still_repoints_and_resets(db_session, offline_ingest):
    """The PATCH route loads the row before calling ``update_source``.
    If another edit moved the feed in between, the change has to be
    judged against the committed row, not that earlier read, or setting
    the address back to what the route saw would look like no change."""
    plugin = make_plugin("rp_stale_read")
    before = await _polled_feed(db_session, plugin)
    first_url, source_id = before.url, before.id

    stale = SessionLocal()
    try:
        pre = await stale.get(Source, source_id)
        assert pre.url == first_url
        async with SessionLocal() as other:
            await scheduler.update_source(other, source_id, url="https://moved.test/feed")
        # The moved feed gets polled, so this edit's own reset shows.
        await age_feed(db_session, plugin.name, hours=0, last_fetch_hours=1)
        row = await scheduler.update_source(stale, source_id, url=first_url)
    finally:
        await stale.close()

    assert row.url == first_url
    assert row.last_fetch_at is None
    stored = await source_row(db_session, plugin.name)
    assert stored.url == first_url
    assert stored.last_fetch_at is None


async def test_new_address_gets_a_first_fetch(db_session, offline_ingest):
    plugin = make_plugin("rp_first_fetch")
    before = await _polled_feed(db_session, plugin)
    async with SessionLocal() as session:
        await scheduler.update_source(session, before.id, url="https://moved.test/feed")

    # The job is rebuilt from the row after an edit; here the plugin
    # just follows the row's new URL.
    plugin.url = "https://moved.test/feed"
    # The new address's back catalogue: one story a day for 30 days.
    plugin.items = [item(100 + n, hours_ago=1 + n * 24) for n in range(30)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 10
    assert summary["skipped"] == 20


async def test_url_change_during_an_in_flight_poll_inserts_nothing(db_session, offline_ingest):
    plugin = make_plugin("rp_in_flight")
    before = await _polled_feed(db_session, plugin)

    class _Moving(plugin):
        async def fetch(self):
            # The user edits the address while the old one is being read.
            async with SessionLocal() as session:
                await scheduler.update_source(session, before.id, url="https://moved.test/feed")
            return [item(200 + n, hours_ago=1 + n) for n in range(5)]

    _Moving.name = plugin.name
    summary = await scheduler._ingest(_Moving)
    assert summary["inserted"] == 0
    assert offline_ingest == [f"https://feed.test/{n}" for n in range(3)]  # only the setup poll
    assert await entry_count(db_session, plugin.name) == 3

    # The bookkeeping was skipped too: the reset stands.
    row = await source_row(db_session, plugin.name)
    assert row.url == "https://moved.test/feed"
    assert row.last_fetch_at is None


async def test_sitemap_change_during_an_in_flight_poll_inserts_nothing(db_session, offline_ingest):
    plugin = make_plugin("rp_in_flight_sitemap")
    before = await _polled_feed(db_session, plugin)

    class _Moving(plugin):
        async def fetch(self):
            async with SessionLocal() as session:
                await scheduler.update_source(
                    session, before.id, sitemap_url="https://moved.test/sitemap.xml"
                )
            return [item(300 + n, hours_ago=1 + n) for n in range(5)]

    _Moving.name = plugin.name
    summary = await scheduler._ingest(_Moving)
    assert summary["inserted"] == 0
    assert await entry_count(db_session, plugin.name) == 3


async def test_change_that_waits_on_a_commit_lands_after_it(db_session, offline_ingest, monkeypatch):
    """The edit's transaction starts before the poll's commit session
    (the PATCH route reads the row first), then waits on the poll's row
    lock. Its new baseline must still come after the poll's rows, so
    the new address still gets a first fetch, and the poll's bookkeeping
    must not overwrite the reset."""
    plugin = make_plugin("rp_wait")

    async def plain_embed(norm):
        return None

    monkeypatch.setattr(scheduler, "_embed_text", plain_embed)
    before = await _polled_feed(db_session, plugin)

    in_commit = asyncio.Event()
    release = asyncio.Event()

    async def held_embed(norm):
        # Inside the poll's commit session, after its row lock.
        in_commit.set()
        await release.wait()
        return None

    edit_session = SessionLocal()
    try:
        # Start the edit's transaction now, before the poll's.
        await edit_session.execute(text("SELECT 1"))

        monkeypatch.setattr(scheduler, "_embed_text", held_embed)
        plugin.items = [item(400, hours_ago=0.5)] + plugin.items
        poll = asyncio.create_task(scheduler._ingest(plugin))
        await asyncio.wait_for(in_commit.wait(), timeout=10)

        edit = asyncio.create_task(
            scheduler.update_source(edit_session, before.id, url="https://moved.test/feed")
        )
        await asyncio.sleep(0.3)
        assert not edit.done(), "the edit should wait on the poll's row lock"

        release.set()
        summary = await asyncio.wait_for(poll, timeout=10)
        row = await asyncio.wait_for(edit, timeout=10)
    finally:
        release.set()
        await edit_session.close()

    assert summary["inserted"] == 1
    assert row.last_fetch_at is None
    newest = await db_session.scalar(
        select(func.max(Entry.fetched_at)).where(Entry.source_id == before.id)
    )
    assert row.created_at > newest

    # So the new address gets a first fetch.
    monkeypatch.setattr(scheduler, "_embed_text", plain_embed)
    plugin.url = "https://moved.test/feed"
    plugin.items = [item(500 + n, hours_ago=1 + n * 24) for n in range(30)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 10 and first["skipped"] == 20


async def test_edit_that_lands_first_makes_the_waiting_poll_discard(db_session, offline_ingest):
    """The other order: the edit holds the row lock when the poll's
    commit session asks for it. The poll waits, then sees the new
    address and stores nothing."""
    plugin = make_plugin("rp_edit_first")
    before = await _polled_feed(db_session, plugin)

    edit_session = SessionLocal()
    try:
        # Take the lock the way update_source does, and hold it.
        await edit_session.get(
            Source, before.id, with_for_update={"key_share": True}, populate_existing=True
        )
        plugin.items = [item(600 + n, hours_ago=1 + n) for n in range(4)]
        poll = asyncio.create_task(scheduler._ingest(plugin))
        await asyncio.sleep(0.3)
        assert not poll.done(), "the poll should wait on the edit's row lock"
        await scheduler.update_source(edit_session, before.id, url="https://moved.test/feed")
        summary = await asyncio.wait_for(poll, timeout=10)
    finally:
        await edit_session.close()

    assert summary["inserted"] == 0
    assert await entry_count(db_session, plugin.name) == 3
    assert (await source_row(db_session, plugin.name)).last_fetch_at is None


async def test_empty_first_poll_of_a_new_address_keeps_the_first_fetch(db_session, offline_ingest):
    """The new address's first poll comes back empty. That success sets
    ``last_fetch_at``, and the feed still holds the old address's rows,
    all fetched before the reset's baseline. Only the EXISTS's
    ``fetched_at >= created_at`` keeps the next poll with items a first
    fetch (newest 10 plus 2 days); without it the poll would run the
    later-poll rules and drop all but the last 2 days as backfill."""
    plugin = make_plugin("rp_empty_then_items")
    before = await _polled_feed(db_session, plugin)
    async with SessionLocal() as session:
        await scheduler.update_source(session, before.id, url="https://moved.test/feed")

    plugin.url = "https://moved.test/feed"
    plugin.items = []
    empty = await scheduler._ingest(plugin)
    assert empty["error"] is None and empty["inserted"] == 0
    assert (await source_row(db_session, plugin.name)).last_fetch_at is not None

    plugin.items = [item(700 + n, hours_ago=1 + n * 24) for n in range(30)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 10
    assert summary["skipped"] == 20


async def test_job_still_holding_the_old_plugin_after_the_edit_inserts_nothing(
    db_session, offline_ingest
):
    """The edit commits before the tick reads the row, but the job still
    holds the plugin built before the edit (``update_source`` replaces
    it only after its commit, and ``trigger_now`` builds from an earlier
    read). The row then already has the new address while the plugin
    fetches the old one, so the guard has to compare the plugin's own
    address, not a fresh read of the row."""
    async with SessionLocal() as session:
        row = Source(name="rp_dyn_stale_plugin", type="rss", category="news", url="https://old.test/feed")
        session.add(row)
        await session.commit()
        await session.refresh(row)
    source_id = row.id

    def build(source: Source, items: list[dict]) -> DynamicRssPlugin:
        plugin = DynamicRssPlugin(source)

        async def fetch() -> list[dict]:
            return list(items)

        plugin.fetch = fetch
        return plugin

    old_plugin_items = [item(n, hours_ago=1 + n, host="old.test") for n in range(3)]
    old_plugin = build(row, old_plugin_items)
    assert (await scheduler._ingest(old_plugin))["inserted"] == 3
    await age_feed(db_session, row.name, hours=10 * 24, last_fetch_hours=1)

    async with SessionLocal() as session:
        reset = await scheduler.update_source(session, source_id, url="https://moved.test/feed")
    reset_last, reset_baseline = reset.last_fetch_at, reset.created_at
    assert reset_last is None

    # The stale job's tick: the old address's newest stories.
    old_plugin_items[:] = [item(800 + n, hours_ago=1 + n * 24, host="old.test") for n in range(30)]
    summary = await scheduler._ingest(old_plugin)
    assert summary["inserted"] == 0
    assert await entry_count(db_session, row.name) == 3
    after = await source_row(db_session, row.name)
    assert after.last_fetch_at == reset_last
    assert after.created_at == reset_baseline

    # The job rebuilt from the edited row fetches the new address, and
    # that poll is the first fetch.
    fresh = build(after, [item(900 + n, hours_ago=1 + n * 24, host="moved.test") for n in range(30)])
    first = await scheduler._ingest(fresh)
    assert first["inserted"] == 10 and first["skipped"] == 20
