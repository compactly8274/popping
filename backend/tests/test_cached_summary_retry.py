"""Unit tests for the empty-string ``cached_summary`` retry-window
logic in ``app.routes.entries._should_retry_empty_cache`` (slice
10b). Marked ``@pytest.mark.no_db`` so the conftest's session-
scope ``_schema`` fixture doesn't force a Postgres connect.

Pure function tests: no DB, no HTTP, no asyncio. The retry
decision is the entire behavioral contract the route cares
about; if any branch regresses, an empty-summary entry
either stays empty forever (regression) or gets re-fetched
on every tap (also a regression — LLM budget burn).

Covers:
  - Pre-migration row (fetched_at is None) — must retry.
  - Empty cache within the retry window — must NOT retry.
  - Empty cache exactly at the window boundary.
  - Empty cache past the retry window — must retry.
  - Naive (tzinfo-less) fetched_at — defensive handling.
  - The route's call-site shape (``not _should_retry_empty_cache``).
"""

from __future__ import annotations

import datetime as dt

import pytest

pytestmark = pytest.mark.no_db

from app.routes.entries import _should_retry_empty_cache


_NOW = dt.datetime(2026, 7, 27, 0, 0, 0, tzinfo=dt.timezone.utc)


def _hours_ago(hours: float, base: dt.datetime = _NOW) -> dt.datetime:
    """Helper: return a timestamp ``hours`` before ``base`` (default
    _NOW). Returns a tz-aware UTC datetime so the production
    happy path is exercised by default.
    """
    return base - dt.timedelta(hours=hours)


def test_pre_migration_row_with_none_fetched_at_retries():
    """A row that pre-dates the migration (no ``fetched_at``) must
    retry. This is the first chevron tap after deploy for any
    empty-string cache that's been around since before the
    upgrade — one-time burst cost that's expected to refresh
    stale caches.
    """
    assert _should_retry_empty_cache(
        fetched_at=None,
        retry_hours=24.0,
        now=_NOW,
    ) is True


def test_fresh_empty_cache_within_window_does_not_retry():
    """An empty cache that's still inside the 24h retry window
    should NOT retry — return the cached empty string without
    burning the LLM budget. This is the common case: the
    chevron tap right after ingest where the LLM/transcript
    failed.
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(1.0),  # 1 hour ago
        retry_hours=24.0,
        now=_NOW,
    ) is False


def test_empty_cache_exactly_at_window_boundary_retries():
    """The boundary is ``retry_hours``, not strictly-less-than. An
    empty cache from exactly ``retry_hours`` ago is treated as
    just-past-fresh — the comparison is ``>=``. This pins the
    off-by-one direction (operators setting retry_hours exactly
    equal to the cache age get the "retry" branch, which is the
    safer default for a transient failure).
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(24.0),
        retry_hours=24.0,
        now=_NOW,
    ) is True


def test_empty_cache_just_under_window_does_not_retry():
    """Just before the window: still fresh, don't retry. Pinned
    next to the boundary test above so a future off-by-one
    refactor of the comparison flips exactly one assertion.
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(23.999),
        retry_hours=24.0,
        now=_NOW,
    ) is False


def test_old_empty_cache_past_window_retries():
    """An empty cache older than the window retries. After
    24h has passed, a transient failure (the source's article
    URL was 404, the LLM provider was over-quota) is unlikely
    to still be in effect.
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(25.0),  # 25 hours ago, just past
        retry_hours=24.0,
        now=_NOW,
    ) is True


def test_very_old_empty_cache_retries():
    """A 30-day-old empty cache also retries. Sanity: there's no
    upper cap on retry age. (The alternative — capping at 2x
    retry_hours, for example — would needlessly surprise an
    operator who set retry_hours=24 and then went on vacation.)
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(24 * 30),  # 30 days
        retry_hours=24.0,
        now=_NOW,
    ) is True


def test_naive_fetched_at_is_handled_without_crash():
    """Defensive: a ``fetched_at`` value with no tzinfo (some
    MySQL drivers drop it; a future driver swap shouldn't
    silently break the retry window) is interpreted as UTC and
    the comparison still works.

    In practice Postgres + asyncpg preserves tzinfo on TIMESTAMPTZ
    columns, so this is a "should not regress" test rather than
    a "this happens" test. The defensive code is small enough
    to be worth the cost.
    """
    naive = (_NOW - dt.timedelta(hours=25)).replace(tzinfo=None)
    assert _should_retry_empty_cache(
        fetched_at=naive,
        retry_hours=24.0,
        now=_NOW,
    ) is True


def test_naive_fetched_at_within_window_does_not_retry():
    """Companion to the above: a naive ``fetched_at`` within the
    window is also handled correctly.
    """
    naive = (_NOW - dt.timedelta(hours=1)).replace(tzinfo=None)
    assert _should_retry_empty_cache(
        fetched_at=naive,
        retry_hours=24.0,
        now=_NOW,
    ) is False


def test_now_defaults_to_utc_now():
    """When ``now`` is not passed, the helper uses
    ``datetime.now(UTC)``. A recent ``fetched_at`` (1s ago) with
    no explicit ``now`` must NOT retry; an old one (1h ago)
    must retry. This pins the implicit-now behavior so a
    future refactor doesn't accidentally use ``datetime.now()``
    (naive local time, which would skew the comparison by
    several hours in non-UTC deployments).
    """
    import time
    recent = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)
    assert _should_retry_empty_cache(recent, retry_hours=24.0) is False
    assert _should_retry_empty_cache(old, retry_hours=24.0) is True
    # Touch time to silence the "unused import" linter — the
    # test relies on real wall-clock time, not a constant, so
    # the tiny sleep here makes the intent explicit.
    time.sleep(0.001)


def test_custom_retry_hours_short_window():
    """A short retry window (e.g. ``retry_hours=0.5`` for
    aggressive testing in a CI scenario) still works — an
    empty cache from 1 hour ago retries, one from 0.1 hours
    ago doesn't.
    """
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(1.0),
        retry_hours=0.5,
        now=_NOW,
    ) is True
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(0.1),
        retry_hours=0.5,
        now=_NOW,
    ) is False


def test_custom_retry_hours_long_window():
    """A long retry window (e.g. ``retry_hours=168`` for "only
    retry once a week") still works. Pinned: the helper is just
    a multiplication of the window in seconds, no implicit
    cap.
    """
    # 100 hours ago, well inside a 1-week window
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(100.0),
        retry_hours=168.0,
        now=_NOW,
    ) is False
    # 200 hours ago, past a 1-week window
    assert _should_retry_empty_cache(
        fetched_at=_hours_ago(200.0),
        retry_hours=168.0,
        now=_NOW,
    ) is True


def test_route_call_site_shape_returns_correct_default():
    """The route's call site is::

        if row.cached_summary != "" or not _should_retry_empty_cache(
            row.cached_summary_fetched_at,
            retry_hours=settings.cached_summary_retry_hours,
        ):
            return EntrySummaryOut(summary=row.cached_summary, cached=True)

    I.e. a True return means "fall through to refetch". This
    test mirrors that decision: for each combination of
    (cached_summary value, fetched_at age, retry_hours), the
    result of ``cached_summary != "" or not _should_retry_empty_cache(...)``
    is what the route uses. Locking in the boolean-table here
    so a future refactor that inverts the negation catches it.
    """
    # Helper that mirrors the route's expression.
    def route_decision(cached_summary: str, fetched_at, retry_hours: float) -> bool:
        # True = "return cached" (skip the fetch chain)
        # False = "fall through to the fetch chain"
        return cached_summary != "" or not _should_retry_empty_cache(
            fetched_at, retry_hours=retry_hours, now=_NOW
        )

    # Non-empty cached_summary: always skip the chain (no re-summarize).
    assert route_decision("a real summary", _hours_ago(1.0), 24.0) is True
    assert route_decision("a real summary", _hours_ago(1000.0), 24.0) is True

    # Empty cached_summary, fresh (within window): skip the chain.
    assert route_decision("", _hours_ago(1.0), 24.0) is True

    # Empty cached_summary, old (past window): re-run the chain.
    assert route_decision("", _hours_ago(25.0), 24.0) is False

    # Empty cached_summary, no fetched_at (pre-migration): re-run.
    assert route_decision("", None, 24.0) is False

    # Empty cached_summary, exactly at the window boundary: retry.
    assert route_decision("", _hours_ago(24.0), 24.0) is False

    # Empty cached_summary, just under the boundary: skip.
    assert route_decision("", _hours_ago(23.999), 24.0) is True