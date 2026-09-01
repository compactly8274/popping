"""Tests for Reddit ingest throttling visibility and schedule stagger.

Pure unit tests: no database, no network, no scheduler event loop needed
except for one async test that builds an APScheduler and immediately shuts
it down. All marked ``no_db`` so they run without Postgres.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import reddit_client, scheduler
from app.sources.dynamic_reddit import DynamicRedditPlugin, _throttled_sources

pytestmark = pytest.mark.no_db


def _source_row(
    *,
    name: str,
    url: str = "r/python",
    interval: int = 3600,
    source_id: int = 1,
):
    return SimpleNamespace(
        id=source_id,
        name=name,
        type="reddit",
        category="news",
        url=url,
        refresh_interval_seconds=interval,
    )


def test_dynamic_source_offset_is_deterministic_and_bounded():
    """Same name + interval must always produce the same offset, and the
    offset must live inside the interval window.
    """
    offset = scheduler._dynamic_source_offset("reddit_technology", 360)
    assert offset == scheduler._dynamic_source_offset("reddit_technology", 360)
    assert 0 <= offset < 360


def test_three_same_interval_sources_get_distinct_offsets():
    """The three live Reddit subs should not collide at the same second."""
    names = ["reddit_technology", "reddit_worldnews", "reddit_science"]
    offsets = {scheduler._dynamic_source_offset(name, 360) for name in names}
    assert len(offsets) == 3


def test_offsets_stable_across_two_builds():
    """Restarting the scheduler must not reshuffle the anchors."""
    names = ["reddit_technology", "reddit_worldnews", "reddit_science"]
    first = [scheduler._dynamic_source_offset(name, 360) for name in names]
    second = [scheduler._dynamic_source_offset(name, 360) for name in names]
    assert first == second


@pytest.mark.asyncio
async def test_scheduler_spreads_dynamic_jobs():
    """``_add_or_replace_dynamic_job`` registers same-interval dynamic
    sources with distinct ``next_run_time`` anchors.
    """
    sched = AsyncIOScheduler(timezone="UTC")
    sched.start()
    try:
        rows = [
            _source_row(name="reddit_technology", interval=360, source_id=21),
            _source_row(name="reddit_worldnews", interval=360, source_id=22),
            _source_row(name="reddit_science", interval=360, source_id=23),
        ]
        for row in rows:
            scheduler._add_or_replace_dynamic_job(sched, row)

        job_ids = [scheduler._dynamic_job_id(row.id) for row in rows]
        next_runs = [sched.get_job(jid).next_run_time for jid in job_ids]
        assert all(n is not None for n in next_runs)
        # Drop microseconds; the spread should still be distinct at
        # second granularity, which is what matters for rate-limiting.
        distinct_seconds = {n.replace(microsecond=0) for n in next_runs}
        assert len(distinct_seconds) == 3
    finally:
        sched.shutdown()


@pytest.mark.asyncio
async def test_429_logs_warning_and_engages_cooldown(monkeypatch, caplog):
    """A 429 response must warn and set the one-cycle cooldown flag."""

    async def _fake_fetch(*args, **kwargs):
        raise reddit_client.RedditFetchThrottled(429)

    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch)
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    _throttled_sources.clear()

    plugin = DynamicRedditPlugin(_source_row(name="reddit_technology", url="r/technology"))
    with caplog.at_level(logging.WARNING, logger="popping.sources.dynamic_reddit"):
        result = await plugin.fetch()

    assert result == []
    assert "reddit feed throttled: source=reddit_technology status=429" in caplog.text
    assert _throttled_sources.get("reddit_technology") is True


@pytest.mark.asyncio
async def test_cooldown_skips_next_scheduled_poll(monkeypatch, caplog):
    """When the cooldown flag is set, the next tick skips the fetch."""
    calls = 0

    async def _fake_fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch)
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    _throttled_sources.clear()
    _throttled_sources["reddit_technology"] = True

    plugin = DynamicRedditPlugin(_source_row(name="reddit_technology", url="r/technology"))
    result = await plugin.fetch()

    assert result == []
    assert calls == 0
    assert "reddit_technology" not in _throttled_sources


@pytest.mark.asyncio
async def test_200_path_unchanged(monkeypatch, caplog):
    """A normal 200 response path must not warn or set cooldown."""

    async def _fake_fetch(*args, **kwargs):
        return [
            {
                "title": "A post",
                "url": "https://example.com/post",
                "permalink": "/r/technology/comments/abc/a_post/",
                "created_utc": 1_700_000_000.0,
            }
        ]

    monkeypatch.setattr(reddit_client, "fetch_subreddit", _fake_fetch)
    monkeypatch.setattr(reddit_client, "is_disabled", lambda: False)
    _throttled_sources.clear()

    plugin = DynamicRedditPlugin(_source_row(name="reddit_technology", url="r/technology"))
    with caplog.at_level(logging.WARNING, logger="popping.sources.dynamic_reddit"):
        result = await plugin.fetch()

    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/post"
    assert "reddit feed throttled" not in caplog.text
    assert "reddit_technology" not in _throttled_sources
