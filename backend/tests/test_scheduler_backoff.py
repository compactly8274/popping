"""Tests for the source retry/backoff logic in ``app.scheduler``.

``_classify_error`` and the backoff math are the two pieces most likely
to silently regress (e.g. someone reordering the isinstance checks, or
the multiplier/cap constants drifting), so most of this file tests the
pure functions. The setup states at the end (``[no list]``,
``[reddit off]``) run ``_ingest`` against the test database: they are
recorded on the feed but are never failures, so they must never count
toward ``error_count``, back the job off or stop the feed (D27).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import scheduler
from app.config import settings
from app.reddit_client import RedditDisabledError
from app.sources.base import NoArticleListError, NotAFeedError
from app.sources.dynamic_reddit import DynamicRedditPlugin
from app.sources.dynamic_rss import DynamicRssPlugin
from factories import make_source
from ingest_harness import age_feed, item, make_plugin, source_row


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code} error", request=request, response=response)


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "blocked"),
        (402, "blocked"),  # RFD's Tollbit paywall gate
        (403, "blocked"),
        (404, "not found"),
        (500, "upstream error"),
        (503, "upstream error"),
        (418, "http error"),  # some other 4xx we don't special-case
    ],
)
def test_classify_error_http_status(code, expected):
    assert scheduler._classify_error(_http_status_error(code)) == expected


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom"),
        httpx.ConnectTimeout("boom"),
        httpx.ReadTimeout("boom"),
        httpx.WriteTimeout("boom"),
        httpx.PoolTimeout("boom"),
        httpx.ReadError("boom"),
        httpx.RemoteProtocolError("boom"),
        httpx.TooManyRedirects("boom"),
        # A plugin's own ``wait_for`` budget. There is no separate
        # timeout kind.
        asyncio.TimeoutError(),
    ],
)
def test_classify_error_network(exc):
    assert scheduler._classify_error(exc) == "network"


def test_classify_error_generic_exception_falls_back():
    assert scheduler._classify_error(ValueError("weird")) == "error"


@pytest.mark.parametrize(
    "exc,expected",
    [
        (NotAFeedError("could not be parsed as a feed"), "not a feed"),
        (NoArticleListError("no sitemap, no links"), "no list"),
        (RedditDisabledError("off"), "reddit off"),
    ],
)
def test_classify_error_named_kinds(exc, expected):
    assert scheduler._classify_error(exc) == expected


# One case per ``last_error`` prefix in the plan's table (PR9a), which
# PR9b's fix table keys on.
_EVERY_KIND = [
    (_http_status_error(403), "blocked"),
    (_http_status_error(404), "not found"),
    (_http_status_error(502), "upstream error"),
    (_http_status_error(429), "http error"),
    (httpx.ConnectTimeout("boom"), "network"),
    (NotAFeedError("parse"), "not a feed"),
    (NoArticleListError("none"), "no list"),
    (RedditDisabledError("off"), "reddit off"),
    (RuntimeError("weird"), "error"),
]


@pytest.mark.parametrize("exc,expected", _EVERY_KIND, ids=[k for _, k in _EVERY_KIND])
def test_every_kind_in_the_table(exc, expected):
    assert scheduler._classify_error(exc) == expected


def test_error_kinds_lists_exactly_the_kinds_classify_returns():
    assert set(scheduler.ERROR_KINDS) == {kind for _, kind in _EVERY_KIND}
    assert len(scheduler.ERROR_KINDS) == len(set(scheduler.ERROR_KINDS))
    assert scheduler.SETUP_STATE_KINDS == {"no list", "reddit off"}
    assert scheduler.SETUP_STATE_KINDS <= set(scheduler.ERROR_KINDS)


def test_not_a_feed_is_classified_before_the_generic_value_error():
    # ``NotAFeedError`` subclasses ``ValueError`` (the Test route's
    # parse_error) but keeps its own kind.
    assert issubclass(NotAFeedError, ValueError)
    assert scheduler._classify_error(NotAFeedError("x")) == "not a feed"


def test_backoff_doubles_and_caps():
    base = 900  # RFD's refresh_interval_seconds
    backoffs = [scheduler._backoff_seconds(base, n) for n in range(1, 10)]
    assert backoffs[:3] == [1800, 3600, 7200]
    # Strictly increasing until it hits the cap.
    for a, b in zip(backoffs, backoffs[1:]):
        assert b >= a
    # Never exceeds the cap, and the cap is actually reached for a
    # long enough failure streak (otherwise the cap constant is dead
    # code and a failing source would back off forever).
    assert all(b <= scheduler._BACKOFF_MAX_SECONDS for b in backoffs)
    assert backoffs[-1] == scheduler._BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------------------
# Setup states: recorded, never failures (D27)
# ---------------------------------------------------------------------------


@pytest.fixture
def reschedules(monkeypatch, offline_ingest):
    """Every interval change ``_ingest`` asks for, by plugin name."""
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        scheduler, "_reschedule_ingest_job", lambda plugin, seconds: calls.append((plugin.name, seconds))
    )
    return calls


async def test_no_list_six_times_on_an_old_feed_keeps_it_active(db_session, reschedules):
    """The old rule spared ``[no list]`` only within 24 hours of the
    feed's ``created_at``; an existing section feed would have been
    stopped within a day. Now it is a setup state whatever the age."""
    plugin = make_plugin("bo_no_list_old")
    await make_source(db_session, plugin.name, url=plugin.url)
    await age_feed(db_session, plugin.name, hours=24 * 30)

    async def _no_list(self):
        raise NoArticleListError("no sitemap and no matching links")

    plugin.fetch = _no_list
    for _ in range(scheduler._AUTO_DISABLE_THRESHOLD):
        summary = await scheduler._ingest(plugin)
        assert summary["error"].startswith("[no list]")

    row = await source_row(db_session, plugin.name)
    assert row.active is True
    assert row.error_count == 0
    assert row.last_error.startswith("[no list]")
    # Never backed off.
    assert reschedules == []


async def test_no_list_leaves_an_earlier_failure_streak_as_it_was(db_session, reschedules):
    plugin = make_plugin("bo_no_list_streak")
    await make_source(db_session, plugin.name, url=plugin.url, error_count=5)

    async def _no_list(self):
        raise NoArticleListError("none")

    plugin.fetch = _no_list
    for _ in range(3):
        await scheduler._ingest(plugin)

    row = await source_row(db_session, plugin.name)
    # Five failures, then setup states: the sixth failure never came.
    assert (row.active, row.error_count) == (True, 5)
    assert reschedules == []


async def test_reddit_off_never_touches_error_count(db_session, reschedules, monkeypatch):
    monkeypatch.setattr(settings, "reddit_hydra_url", "")
    monkeypatch.setattr(settings, "reddit_direct_disabled", True)
    row = await make_source(db_session, "bo_reddit_off", type="reddit", url="r/python", error_count=2)
    plugin = DynamicRedditPlugin(row)

    for _ in range(scheduler._AUTO_DISABLE_THRESHOLD + 1):
        summary = await scheduler._ingest(plugin)
        assert summary["error"].startswith("[reddit off]")

    after = await source_row(db_session, "bo_reddit_off")
    assert (after.active, after.error_count) == (True, 2)
    assert after.last_error.startswith("[reddit off]")
    assert reschedules == []


async def test_a_failure_still_counts_and_backs_off(db_session, reschedules):
    # The same path for an ordinary failure: counted, backed off, and
    # stopped at the threshold, as before.
    plugin = make_plugin("bo_failure")
    await make_source(db_session, plugin.name, url=plugin.url)

    async def _refused(self):
        raise httpx.ConnectError("refused")

    plugin.fetch = _refused
    for _ in range(scheduler._AUTO_DISABLE_THRESHOLD):
        await scheduler._ingest(plugin)

    row = await source_row(db_session, plugin.name)
    assert row.error_count == scheduler._AUTO_DISABLE_THRESHOLD
    assert row.active is False
    base = plugin.refresh_interval_seconds
    assert reschedules == [
        (plugin.name, scheduler._backoff_seconds(base, n))
        for n in range(1, scheduler._AUTO_DISABLE_THRESHOLD)
    ]


async def test_a_non_reddit_feed_is_unaffected_while_reddit_is_off(db_session, reschedules, monkeypatch):
    monkeypatch.setattr(settings, "reddit_hydra_url", "")
    monkeypatch.setattr(settings, "reddit_direct_disabled", True)
    row = await make_source(db_session, "bo_rss_reddit_off", url="https://rss.test/feed")
    plugin = DynamicRssPlugin(row)
    stories = [item(n, hours_ago=1 + n, host="rss.test") for n in range(2)]

    async def _fetch(self):
        return stories

    plugin.fetch = _fetch.__get__(plugin)
    summary = await scheduler._ingest(plugin)

    assert summary["error"] is None
    assert summary["inserted"] == 2
    after = await source_row(db_session, "bo_rss_reddit_off")
    assert after.last_error is None
