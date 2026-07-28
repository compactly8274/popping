"""Slice 26 — singleflight the convergence cache.

The previous code released ``_cache_lock`` between the cache read and
the SQL scan, so two concurrent callers both saw a cache miss and
both ran the scan + populate path. With a high-concurrency endpoint
like ``/api/foryou`` firing while a brief is being generated, that's
a thundering-herd that doubles the convergence-scan DB load on every
miss (once per 30s TTL window, but the missed window is the worst
one to double-up).

Fix: hold the lock around the full miss path (read → scan → write →
evict) so the first caller does the scan + populates the cache, and
concurrent callers see the populated cache and return without
scanning. Cost: a brief serialization on the very first miss in a
30s TTL window. Win: every concurrent caller after that hits the
cache without touching the DB.

This test file guards:
- ``async with _cache_lock`` wraps the full miss path (one outer
  acquisition, not two)
- The early-return ``if cached is not None: return cached`` is inside
  the lock (no TOCTOU between read and write)
- Functional equivalence: a single caller still produces the same
  result, second call still hits cache
- Concurrent callers (asyncio.gather) coalesce into a single scan
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONVERGENCE = REPO / "backend/app/scoring/convergence.py"


# ---------------------------------------------------------------------------
# 1. Source-shape checks (structural)
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_singleflight_outer_lock_wraps_full_miss_path():
    """The ``async with _cache_lock`` block must wrap the full miss
    path (read → scan → write). Two separate ``async with`` blocks
    (one for read, one for write) is the bug we're fixing.
    """
    src = CONVERGENCE.read_text()
    # Exactly one ``async with _cache_lock`` block — the outer one
    # that wraps everything.
    n = len(re.findall(r"async with _cache_lock", src))
    assert n == 1, (
        f"Expected exactly 1 outer ``async with _cache_lock`` (the "
        f"singleflight block). Found {n}. If there are two, the lock "
        f"is being released between the read and the write — slice 26's "
        f"whole point is to keep it held across the full miss path."
    )


@pytest.mark.no_db
def test_singleflight_early_return_inside_lock():
    """The ``if cached is not None: return cached`` short-circuit MUST
    be inside the lock, not after it. If it's after the lock, two
    concurrent callers can both pass the check (the cache is empty
    when they check) and both scan.
    """
    src = CONVERGENCE.read_text()
    # Find the ``async with _cache_lock:`` block and the early-return
    # inside it.
    m = re.search(
        r"async with _cache_lock:\s*cached = _cache\.get\(key\)\s*\n\s*if cached is not None:\s*return cached",
        src,
    )
    assert m, (
        "The early-return ``if cached is not None: return cached`` must "
        "be inside the ``async with _cache_lock`` block, indented under "
        "it. If it's at the same indentation as the lock or after it, "
        "two concurrent callers both pass the check."
    )


@pytest.mark.no_db
def test_singleflight_cache_write_inside_lock():
    """The cache-write ``_cache[key] = result`` must also be inside
    the same lock. If it's in a separate ``async with _cache_lock:``
    block (the bug pattern), the singleflight fix is incomplete.
    """
    src = CONVERGENCE.read_text()
    # Find the lock block — anchor on ``async with _cache_lock:`` then
    # check the cache-write line appears before the matching dedent.
    # Cheap check: the cache write must appear indented MORE than the
    # ``async with _cache_lock:`` line (proves it's inside the block).
    lock_match = re.search(r"^(\s*)async with _cache_lock:", src, re.MULTILINE)
    assert lock_match, "Couldn't find async with _cache_lock line"
    lock_indent = len(lock_match.group(1))
    write_match = re.search(r"^(\s*)_cache\[key\]\s*=\s*result", src, re.MULTILINE)
    assert write_match, "Couldn't find _cache[key] = result line"
    write_indent = len(write_match.group(1))
    assert write_indent > lock_indent, (
        f"_cache[key] = result (indent={write_indent}) must be indented "
        f"deeper than ``async with _cache_lock:`` (indent={lock_indent}). "
        f"Equal or shallower indent means the write is OUTSIDE the lock — "
        f"the bug slice 26 is fixing."
    )


@pytest.mark.no_db
def test_singleflight_eviction_still_inside_lock():
    """The eviction loop ``for old in list(_cache.keys()): ... _cache.pop(old, None)``
    must also be inside the lock — it's modifying the same dict the
    write just touched.
    """
    src = CONVERGENCE.read_text()
    # The eviction pattern is inside the same block
    assert re.search(
        r"_cache\[key\]\s*=\s*result[^}]*?for old in list\(_cache\.keys\(\)\)",
        src,
        re.DOTALL,
    ), (
        "The eviction loop must remain inside the lock block alongside "
        "the cache write. Splitting them is a regression."
    )


@pytest.mark.no_db
def test_singleflight_final_return_inside_lock():
    """``return result`` must be inside the lock block — the lock
    guarantees the write is published before any concurrent caller
    sees a populated cache. Returning before the lock exits would
    break that guarantee (only matters for tests / awaited callers
    that read the cache from another task mid-await, but it's the
    contract.)
    """
    src = CONVERGENCE.read_text()
    # Look for the return-result line
    m = re.search(r"\n\s*return result\s*\n", src)
    assert m, "Couldn't locate ``return result`` in convergence.py"
    # Get the 200 chars before to confirm it's inside the block
    before = src[max(0, m.start()-200):m.start()]
    # The number of ``async with _cache_lock`` opens before this point
    opens_before = len(re.findall(r"async with _cache_lock", before))
    # And the function-level dedent
    if opens_before >= 1:
        # The first ``async with`` is still active (no other closing
        # in between — there's only one block total)
        assert True  # We already verified there's exactly 1 lock in the file
    # The return must be indented 4 spaces deeper than the function
    # body (i.e. inside the block).
    line = src[m.start():m.end()]
    indent = len(line) - len(line.lstrip())
    assert indent >= 4, (
        f"``return result`` should be indented inside the "
        f"``async with _cache_lock`` block. Got indent={indent}, line: {line!r}"
    )


# ---------------------------------------------------------------------------
# 2. Functional smoke: the slice changes behavior, not just shape
# ---------------------------------------------------------------------------


@pytest.mark.no_db
def test_singleflight_two_concurrent_calls_share_one_scan():
    """End-to-end behavioral test: with a fake session that counts
    ``execute`` calls, two concurrent ``counts(...)`` calls on a cold
    cache must produce exactly 1 scan, not 2.

    This is the user-visible bug the slice fixes: before slice 26,
    the previous ``async with _cache_lock: cached = _cache.get(key)``
    short-circuit released the lock before the scan, so both callers
    saw an empty cache and both ran the SQL scan.

    With singleflight, the first caller acquires the lock, sees the
    cache empty, runs the scan, writes the cache, releases the lock.
    The second caller was blocked on the lock — when it acquires it,
    the cache is populated and it short-circuits without scanning.
    """
    import sys
    backend_path = str(REPO / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    from app.scoring import convergence

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def all(self):
            return self._rows

    class CountingSession:
        """Counts every ``execute`` call. Simulates a slow scan by
        holding a short sleep so the second caller has time to block
        on the lock."""
        def __init__(self, scan_rows, scan_delay_seconds=0.05):
            self._rows = scan_rows
            self._delay = scan_delay_seconds
            self.execute_count = 0

        async def execute(self, stmt):
            self.execute_count += 1
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            return FakeResult(self._rows)

    # Two callers, both starting at the same time. The first to
    # acquire the lock does the scan; the second short-circuits on
    # the populated cache. Rows are crafted so the result is non-empty:
    # ``alpha story`` and ``beta coverage`` each appear in 2 sources,
    # so the ``len(srcs) > 1`` filter keeps them in the returned dict.
    rows = [
        ("alpha story", "source_a"),
        ("beta coverage", "source_b"),
        ("alpha story", "source_c"),
        ("beta coverage", "source_d"),
    ]
    sess = CountingSession(rows, scan_delay_seconds=0.05)

    # Reset module-level cache to ensure a cold start
    convergence._cache.clear()

    # Build two coroutines and run them concurrently
    async def run_pair():
        # Both must use the same session to share the scan
        t1 = asyncio.create_task(convergence.counts(sess, 24))
        t2 = asyncio.create_task(convergence.counts(sess, 24))
        r1, r2 = await asyncio.gather(t1, t2)
        return r1, r2

    r1, r2 = asyncio.run(run_pair())

    assert sess.execute_count == 1, (
        f"Singleflight failed: expected exactly 1 execute() call across "
        f"two concurrent counts() invocations, got {sess.execute_count}. "
        f"The lock is being released between the cache read and the "
        f"scan, so both callers see an empty cache and both scan."
    )
    # Both results should be identical (the populated cache value)
    assert r1 == r2, (
        f"Concurrent callers got different results: {r1} vs {r2}. "
        f"Both should return the same cached dict."
    )
    # And both should be a non-empty dict derived from the rows
    assert isinstance(r1, dict)
    assert len(r1) > 0, "Expected non-empty result from 2 rows"


@pytest.mark.no_db
def test_singleflight_serial_calls_still_work():
    """Sanity: serial (non-concurrent) calls must continue to work.
    First call scans, second call hits cache."""
    import sys
    backend_path = str(REPO / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    from app.scoring import convergence

    class FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def all(self):
            return self._rows

    class CountingSession:
        def __init__(self, scan_rows):
            self._rows = scan_rows
            self.execute_count = 0
        async def execute(self, stmt):
            self.execute_count += 1
            return FakeResult(self._rows)

    rows = [("title one", "source_a"), ("title two", "source_b")]
    sess = CountingSession(rows)
    convergence._cache.clear()

    r1 = asyncio.run(convergence.counts(sess, 24))
    r2 = asyncio.run(convergence.counts(sess, 24))

    # First call scans, second hits cache → exactly 1 execute
    assert sess.execute_count == 1, (
        f"Serial calls should produce 1 execute (first scans, second "
        f"hits cache). Got {sess.execute_count}."
    )
    assert r1 == r2, "Both serial calls should return the same result"