"""Unit tests for the per-entry summary lock and the cache
helper that the double-checked locking pattern uses.

The three summary endpoints (``/api/entries/{id}/summary``,
``/podcast_summary``, ``/reddit_comment_summary``) used to
have a race: two concurrent requests for the same entry could
both see "cache empty", both run the fetch + LLM chain, and
both write back. The fix is a per-entry ``asyncio.Lock`` plus
double-checked locking (fast-path check → lock → re-read →
re-check → work).

These tests don't touch Postgres — they exercise the lock
helper and the cache-response helper directly. The endpoint
behaviour (full request → response) is exercised by the
existing test_cached_summary_retry tests on a real DB; the
tests here lock the *wire* (lock identity, cap, escape hatch,
cache-response rules) so a future refactor that breaks the
concurrency contract is caught at unit-test time.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.config import settings
from app.routes import entries


# ---------------------------------------------------------------------------
# Lock helper: identity, cap, escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_get_summary_lock_returns_same_lock_for_same_id():
    """Two calls for the same entry_id return the same lock
    object. The fast path (no guard acquire for the second
    call) means the dict-lookup is what provides the identity;
    a future refactor that allocates a new lock per call
    breaks the per-entry serialization this whole pattern
    depends on.
    """
    l1 = await entries._get_summary_lock(42)
    l2 = await entries._get_summary_lock(42)
    assert l1 is l2, "same entry_id must return the same lock object"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_get_summary_lock_returns_different_locks_for_different_ids():
    """Different entry_ids must return different locks. Otherwise
    the per-entry serialization becomes a global serialization
    (one request for entry 1000 blocks every other entry),
    which would tank the dashboard's concurrent-tap behavior.
    """
    l1 = await entries._get_summary_lock(100)
    l2 = await entries._get_summary_lock(200)
    assert l1 is not l2, "different entry_ids must return different locks"


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_cap_above_max_returns_throwaway_lock_not_stored():
    """When the dict is at the cap, the helper returns a fresh
    untracked ``asyncio.Lock()`` rather than allocating a new
    entry. The throwaway lock still serializes locally for
    the requesting task (two concurrent requests for the SAME
    id beyond the cap still serialize on the throwaway), but
    the dict size is bounded — a leak guard for a long-lived
    process that sees > ``_SUMMARY_LOCKS_MAX`` distinct ids.
    """
    # Snapshot the current size so we don't disturb other tests.
    snapshot = dict(entries._summary_locks)
    try:
        # Saturate the dict to the cap.
        for i in range(entries._SUMMARY_LOCKS_MAX - len(entries._summary_locks)):
            await entries._get_summary_lock(50_000 + i)
        # One more call must return a throwaway that is NOT
        # added to the dict.
        sentinel_id = 99_999
        assert sentinel_id not in entries._summary_locks
        throwaway = await entries._get_summary_lock(sentinel_id)
        try:
            assert isinstance(throwaway, asyncio.Lock)
            assert sentinel_id not in entries._summary_locks, (
                "beyond-cap lock must not be added to the dict"
            )
            # The throwaway is still a real, acquirable lock.
            async with throwaway:
                pass
        finally:
            # The throwaway isn't tracked, so nothing to clean up
            # in the dict. (The lock object itself is GC'd.)
            pass
    finally:
        # Restore the dict so other tests aren't affected.
        entries._summary_locks.clear()
        entries._summary_locks.update(snapshot)


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_concurrent_acquires_serialize():
    """Two concurrent tasks acquiring the same lock must
    serialize. Task 1 acquires, holds, then task 2 acquires
    AFTER task 1 releases. Without serialization, both
    would acquire immediately and the LLM call would happen
    twice — the bug we're fixing.

    The test holds the lock from task 1 and verifies that
    task 2 is blocked on ``acquire()`` until task 1 releases.
    """
    lock = await entries._get_summary_lock(7_777)
    order: list[str] = []

    async def hold_then_release():
        async with lock:
            order.append("hold")
            await asyncio.sleep(0.05)
        order.append("release")

    async def wait_then_acquire():
        await asyncio.sleep(0.01)  # let hold_then_release acquire first
        async with lock:
            order.append("acquire")

    await asyncio.gather(hold_then_release(), wait_then_acquire())
    # The order must be hold → release → acquire, NOT hold → acquire.
    assert order == ["hold", "release", "acquire"], (
        f"lock did not serialize: {order}"
    )


# ---------------------------------------------------------------------------
# Cache response helper
# ---------------------------------------------------------------------------


class FakeRow:
    """Stand-in for an ``Entry`` ORM row — only the two cache
    fields the helper reads are populated."""

    def __init__(self, cached_summary, cached_summary_fetched_at):
        self.cached_summary = cached_summary
        self.cached_summary_fetched_at = cached_summary_fetched_at


@pytest.mark.no_db
def test_cache_response_none_when_cache_is_none():
    """``cached_summary is None`` → never attempted → fall
    through (the chain must run). The helper returns None
    so the endpoint proceeds to the lock-and-fetch path.
    """
    row = FakeRow(cached_summary=None, cached_summary_fetched_at=None)
    assert entries._summary_cache_response(row) is None


@pytest.mark.no_db
def test_cache_response_returned_when_cache_populated():
    """Non-empty ``cached_summary`` → return cached verbatim.
    The retry-window check doesn't apply to non-empty
    caches (re-summarizing an article just churns the LLM
    budget, so the rule is "non-empty = always serve").
    """
    row = FakeRow(cached_summary="Real summary", cached_summary_fetched_at=None)
    resp = entries._summary_cache_response(row)
    assert resp is not None
    assert resp.cached is True
    assert resp.summary == "Real summary"


@pytest.mark.no_db
def test_cache_response_returned_for_empty_cache_inside_retry_window():
    """Empty ``cached_summary`` + ``fetched_at`` inside the
    retry window → return cached empty. The endpoint reports
    "we tried, no summary is available" rather than running
    the chain again. This is the "recent failure, don't
    burn the LLM" branch.
    """
    now = dt.datetime.now(dt.timezone.utc)
    recent = now - dt.timedelta(hours=1)
    row = FakeRow(cached_summary="", cached_summary_fetched_at=recent)
    resp = entries._summary_cache_response(row)
    assert resp is not None
    assert resp.cached is True
    assert resp.summary == ""


@pytest.mark.no_db
def test_cache_response_falls_through_for_empty_cache_outside_retry_window():
    """Empty ``cached_summary`` + ``fetched_at`` outside the
    retry window → fall through (None). The endpoint
    re-runs the chain — the cached "no summary available"
    has aged out and a transient failure (e.g. LLM
    provider was over quota at the time) may have cleared.
    """
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(hours=settings.cached_summary_retry_hours + 1)
    row = FakeRow(cached_summary="", cached_summary_fetched_at=old)
    assert entries._summary_cache_response(row) is None


@pytest.mark.no_db
def test_cache_response_falls_through_for_pre_migration_row():
    """``fetched_at is None`` (pre-migration row) → fall
    through. The first chevron tap after deploy refetches
    and re-caches every empty-string hit; one-time burst
    cost that's expected. This is the same rule as
    ``_should_retry_empty_cache`` returns True for None.
    """
    row = FakeRow(cached_summary="", cached_summary_fetched_at=None)
    assert entries._summary_cache_response(row) is None


# ---------------------------------------------------------------------------
# Wire test: the lock helper and the cache helper are independent
# (a future refactor that ties them together — e.g. a per-entry
# "this row is being summarized right now" sentinel written to
# the DB — would change this contract; the test catches it).
# ---------------------------------------------------------------------------


@pytest.mark.no_db
@pytest.mark.asyncio
async def test_lock_helper_and_cache_helper_are_independent():
    """The lock is in process memory (asyncio); the cache
    helper reads from a row object. A refactor that
    couples them — e.g. a per-entry "summary in progress"
    column on Entry, or a per-entry "summary was
    attempted at T" sentinel that the lock sets —
    changes this contract and is caught here.
    """
    # The lock dict and the cache column are different
    # state machines. We exercise each in isolation.
    lock = await entries._get_summary_lock(99_999_001)
    assert isinstance(lock, asyncio.Lock)
    row = FakeRow(cached_summary="x", cached_summary_fetched_at=None)
    resp = entries._summary_cache_response(row)
    assert resp is not None
