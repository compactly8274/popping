"""The ingest rules end to end, through ``scheduler._ingest`` and the
test database: a new feed, a feed that first works days after it was
added, a feed resumed after weeks away, and the per-poll and undated
caps never flood a tab and never lose a story they shouldn't.

The pure rules are pinned in ``test_ingest_rules.py``; these tests pin
the scheduler's first-fetch detection, its resume gap and its
bookkeeping around them."""

from __future__ import annotations

import pytest

from app import runtime_settings, scheduler
from ingest_harness import (
    accounted,
    age_feed,
    entry_count,
    item,
    make_plugin,
    source_row,
)


async def test_empty_success_then_items_3_days_later_get_a_first_fetch(db_session, offline_ingest):
    plugin = make_plugin("bf_empty_then_items")
    plugin.items = []
    first = await scheduler._ingest(plugin)
    assert first["error"] is None and first["inserted"] == 0
    row = await source_row(db_session, plugin.name)
    # A zero-item success still counts as a success.
    assert row.last_fetch_at is not None

    # Three days of polls that all came back empty: added 3 days ago,
    # last (empty) success an hour ago, so only "nothing stored since
    # the baseline" makes this a first fetch, not the resume gap.
    await age_feed(db_session, plugin.name, hours=72, last_fetch_hours=1)
    # 40 stories, one every 3 h: 16 inside the last 2 days, 24 older.
    plugin.items = [item(n, hours_ago=1 + n * 3) for n in range(40)]
    second = await scheduler._ingest(plugin)
    assert second["inserted"] == 16
    assert second["skipped"] == 24
    assert accounted(second)
    assert await entry_count(db_session, plugin.name) == 16


async def test_first_fetch_with_little_recent_keeps_the_newest_10(db_session, offline_ingest):
    plugin = make_plugin("bf_newest_10")
    # 30 stories, one a day: only 2 inside the last 2 days.
    plugin.items = [item(n, hours_ago=1 + n * 24) for n in range(30)]
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 10
    assert summary["skipped"] == 20
    assert accounted(summary)
    stored = set(offline_ingest)
    assert stored == {f"https://feed.test/{n}" for n in range(10)}


async def test_feed_resumed_after_21_days_gets_at_most_10_plus_2_days(db_session, offline_ingest):
    plugin = make_plugin("bf_resumed")
    plugin.items = [item(n, hours_ago=1 + n) for n in range(3)]
    assert (await scheduler._ingest(plugin))["inserted"] == 3
    # 21 days without a successful poll (paused, or failing in backoff).
    await age_feed(db_session, plugin.name, hours=21 * 24)

    # Everything published while it was away: one story every 8 h for
    # 21 days (63 stories), 6 of them inside the last 2 days.
    plugin.items = [item(100 + n, hours_ago=1 + n * 8) for n in range(63)]
    summary = await scheduler._ingest(plugin)
    recent = sum(1 for n in range(63) if 1 + n * 8 <= 48)
    assert summary["inserted"] == max(10, recent) == 10
    assert summary["skipped"] == 63 - 10
    assert accounted(summary)


async def _resume_after_28_days(db_session, plugin, *, dated: bool, new_posts: int):
    """A weekly blog that lists 20 posts. Its first fetch stores the
    newest 10; then it goes 28 days without a successful poll (a pause,
    an auto-disable, a server outage). Returns the resumed poll's
    summary, for a listing of ``new_posts`` new posts plus the 16
    newest old ones."""

    def weekly(n: int, weeks_ago: int, shift_h: float = 0) -> dict:
        return item(n, hours_ago=None if not dated else 1 + shift_h + weeks_ago * 7 * 24)

    plugin.items = [weekly(n, n) for n in range(20)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 10
    await age_feed(db_session, plugin.name, hours=28 * 24)

    old = [weekly(n, n, shift_h=28 * 24) for n in range(16)]
    new = [weekly(100 + n, n) for n in range(new_posts)]
    plugin.items = new + old
    return await scheduler._ingest(plugin)


@pytest.mark.parametrize("new_posts", [0, 4])
@pytest.mark.parametrize("dated", [True, False], ids=["dated", "undated"])
async def test_resumed_feed_stores_only_its_new_posts_not_the_skipped_back_catalogue(
    db_session, offline_ingest, dated, new_posts
):
    plugin = make_plugin(f"bf_resume_deep_{'d' if dated else 'u'}{new_posts}")
    summary = await _resume_after_28_days(db_session, plugin, dated=dated, new_posts=new_posts)
    assert summary["inserted"] == new_posts
    assert summary["duplicates"] == 10
    assert summary["skipped"] == 6  # p10-p15, below the stored posts
    assert accounted(summary)
    assert await entry_count(db_session, plugin.name) == 10 + new_posts
    assert set(offline_ingest[10:]) == {f"https://feed.test/{100 + n}" for n in range(new_posts)}


async def test_resume_gap_boundary(db_session, offline_ingest):
    """Under ``ingest.resume_gap_hours`` (72) since the last success
    the later-poll rules apply; over it, a first fetch."""
    plugin = make_plugin("bf_gap")
    plugin.items = [item(0, hours_ago=1)]
    await scheduler._ingest(plugin)
    # Added 40 days ago, last success 71 h ago: a later poll, so a
    # story from 20 days ago (inside 30 days, after the baseline) lands.
    await age_feed(db_session, plugin.name, hours=40 * 24, last_fetch_hours=71)
    old_but_new = [item(1, hours_ago=20 * 24)] + [item(10 + n, hours_ago=25 * 24 + n) for n in range(15)]
    plugin.items = old_but_new
    later = await scheduler._ingest(plugin)
    assert later["inserted"] == 16 and later["skipped"] == 0

    # Same feed, last success 73 h ago: a first fetch keeps the newest 10.
    await age_feed(db_session, plugin.name, hours=0, last_fetch_hours=73)
    plugin.items = [item(200 + n, hours_ago=5 * 24 + n) for n in range(15)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 10 and first["skipped"] == 5
    assert accounted(first)


async def test_new_item_dated_5_days_back_on_an_hourly_feed_is_kept(db_session, offline_ingest):
    plugin = make_plugin("bf_late_item")
    plugin.items = [item(n, hours_ago=1 + n) for n in range(3)]
    await scheduler._ingest(plugin)
    # Added 10 days ago, polled an hour ago.
    await age_feed(db_session, plugin.name, hours=10 * 24, last_fetch_hours=1)
    late = item(50, hours_ago=5 * 24)
    plugin.items = [late] + plugin.items
    summary = await scheduler._ingest(plugin)
    assert summary["inserted"] == 1
    assert summary["skipped"] == 0
    assert summary["duplicates"] == 3
    assert offline_ingest[-1] == late["url"]


async def test_80_new_items_land_over_two_polls(db_session, offline_ingest):
    plugin = make_plugin("bf_burst")
    plugin.items = [item(0, hours_ago=30)]
    await scheduler._ingest(plugin)
    await age_feed(db_session, plugin.name, hours=10 * 24, last_fetch_hours=1)

    burst = [item(100 + n, hours_ago=1 + n * 0.25) for n in range(80)]
    plugin.items = burst + [item(0, hours_ago=30)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 50
    assert first["skipped"] == 30
    assert accounted(first)
    second = await scheduler._ingest(plugin)
    assert second["inserted"] == 30
    assert second["skipped"] == 0
    assert accounted(second)
    assert await entry_count(db_session, plugin.name) == 1 + 80


async def test_first_fetch_over_the_per_poll_cap_lands_the_rest_next_poll(db_session, offline_ingest):
    """A busy feed's first fetch has more than 50 stories inside 2 days:
    50 land, and the other 20 land on the next poll instead of being
    treated as backfill."""
    plugin = make_plugin("bf_busy_first")
    plugin.items = [item(n, hours_ago=1 + n * 0.5) for n in range(70)]
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 50 and first["skipped"] == 20
    second = await scheduler._ingest(plugin)
    assert second["inserted"] == 20 and second["skipped"] == 0
    assert await entry_count(db_session, plugin.name) == 70


async def test_undated_60_item_feed_gives_10_then_at_most_2_per_poll(db_session, offline_ingest):
    plugin = make_plugin("bf_undated")
    listing = [item(n, hours_ago=None) for n in range(60)]
    plugin.items = listing
    first = await scheduler._ingest(plugin)
    assert first["inserted"] == 10 and first["skipped"] == 50
    assert accounted(first)

    # The same listing again: nothing new above the stored stories.
    again = await scheduler._ingest(plugin)
    assert again["inserted"] == 0
    assert again["duplicates"] == 10 and again["skipped"] == 50
    assert accounted(again)

    # Five new undated stories at the top: two land.
    fresh = [item(100 + n, hours_ago=None) for n in range(5)]
    plugin.items = fresh + listing
    third = await scheduler._ingest(plugin)
    assert third["inserted"] == 2
    assert third["skipped"] == 3 + 50
    assert accounted(third)
    assert offline_ingest[-2:] == [fresh[0]["url"], fresh[1]["url"]]
    assert await entry_count(db_session, plugin.name) == 12


async def test_tunables_are_read_from_runtime_settings(db_session, offline_ingest):
    plugin = make_plugin("bf_tunable")
    try:
        await runtime_settings.set("ingest.first_fetch_keep", "3")
        plugin.items = [item(n, hours_ago=1 + n * 24) for n in range(10)]
        summary = await scheduler._ingest(plugin)
        assert summary["inserted"] == 3 and summary["skipped"] == 7
    finally:
        runtime_settings.invalidate_all()


async def test_manual_ingest_result_carries_skipped():
    from app.schemas import IngestResult

    out = IngestResult(source="x", fetched=5, inserted=1, duplicates=1, skipped=3)
    assert out.skipped == 3
    # An older summary without the key still validates.
    assert IngestResult(source="x", fetched=0, inserted=0, duplicates=0).skipped == 0
