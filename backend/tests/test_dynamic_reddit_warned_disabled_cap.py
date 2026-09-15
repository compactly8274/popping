"""Regression test for a bug found in a repo-wide audit:
``dynamic_reddit._warned_disabled`` was a plain ``set`` that only
ever grew via ``.add()`` — unlike ``_throttled_sources`` (actively
popped every tick) and ``reddit_client._crossref_cache`` (TTL-evicted
lazily), nothing ever removed an entry. A source repeatedly renamed
or deleted-and-recreated over a long-running process's lifetime would
accumulate stale names forever. FIFO-capped the same way
``generic_scrape.py``'s ``_extracted_urls`` is.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import reddit_client
from app.sources import dynamic_reddit
from app.sources.dynamic_reddit import (
    _MAX_WARNED_DISABLED,
    DynamicRedditPlugin,
    _warned_disabled,
)

pytestmark = pytest.mark.no_db


def _source_row(*, name: str, source_id: int = 1):
    return SimpleNamespace(
        id=source_id,
        name=name,
        type="reddit",
        category="news",
        url="r/python",
        refresh_interval_seconds=3600,
    )


@pytest.fixture(autouse=True)
def _isolated_warned_disabled():
    saved = dict(_warned_disabled)
    _warned_disabled.clear()
    yield
    _warned_disabled.clear()
    _warned_disabled.update(saved)


@pytest.mark.asyncio
async def test_warned_disabled_evicts_oldest_past_cap():
    with patch.object(reddit_client, "is_disabled", lambda: True):
        for i in range(_MAX_WARNED_DISABLED + 50):
            plugin = DynamicRedditPlugin(_source_row(name=f"reddit_src_{i}", source_id=i))
            await plugin.fetch()

    assert len(_warned_disabled) == _MAX_WARNED_DISABLED
    # The oldest 50 names were evicted; the most recent ones remain.
    assert "reddit_src_0" not in _warned_disabled
    assert f"reddit_src_{_MAX_WARNED_DISABLED + 49}" in _warned_disabled


@pytest.mark.asyncio
async def test_warned_disabled_only_warns_once_per_name(caplog):
    import logging

    with patch.object(reddit_client, "is_disabled", lambda: True):
        plugin = DynamicRedditPlugin(_source_row(name="reddit_repeat"))
        with caplog.at_level(logging.WARNING, logger="popping.sources.dynamic_reddit"):
            await plugin.fetch()
            await plugin.fetch()
            await plugin.fetch()

    warnings = [r for r in caplog.records if "skipped" in r.message]
    assert len(warnings) == 1
