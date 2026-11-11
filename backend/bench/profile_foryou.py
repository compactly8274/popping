"""In-process benchmark for the /api/foryou hot path.

Calls ``foryou()`` directly (no HTTP layer) N times and reports
p50 / p95 / p99 / mean latency. Run against both checkouts — before
(parent of PR #94) and after (main) — to measure the Python-side win
from:

  - P1: dropping ``Entry.embedding`` (Vector(384)) from the SELECT
    and eliminating 500 numpy cosine-similarity recompute per request
  - P3: JOIN Source in the same query instead of a follow-up
    ``SELECT ... WHERE id IN (...)`` round-trip

Usage::

    cd backend

    # Before (parent of PR #94):
    git checkout e85399e~1
    python -m bench.profile_foryou --n 100

    # After (main):
    git checkout main
    python -m bench.profile_foryou --n 100

Environment overrides (when running outside Docker)::

    POPPING_POSTGRES_HOST   defaults to "postgres" (Docker service name)
    POPPING_POSTGRES_PORT   defaults to 5432
    POPPING_POSTGRES_USER   defaults to "popping"
    POPPING_POSTGRES_PASSWORD  defaults to "popping"
    POPPING_POSTGRES_DB     defaults to "popping"

Expected results:
    Before: ~30-40ms mean (2 queries + 500 numpy.dot() @ ~50µs each)
    After:  ~5-10ms mean  (1 query + 500 float multiplies @ ~0.1µs each)
    Look for a 3-10x drop in mean and a flat p99 (no numpy spike).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from typing import Sequence

# Ensure the app package is importable when running as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, engine  # noqa: E402
from app.routes.foryou import foryou  # noqa: E402


async def _run_once(limit: int) -> float:
    """Call ``foryou()`` once, return wall-clock time in ms."""
    async with SessionLocal() as session:
        # ``foryou`` signature: (session, user, limit, category).
        # ``user=None`` works because the route uses ``current_user``
        # which returns None for cookie-less / non-bypass callers,
        # and ``foryou`` doesn't gate on the user — it reads
        # UserProfile.id=1 unconditionally.
        t0 = time.perf_counter()
        await foryou(
            session=session,
            user=None,
            limit=limit,
            category=None,
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
    return elapsed


async def bench(n: int, limit: int, warmup: int) -> None:
    """Run N timed calls, discard ``warmup``, print percentile stats.

    Warmup is important: the first call hits cold caches (PostgreSQL
    buffer pool, SQLAlchemy compiled-statement cache, convergence
    cache). Without warmup the first iteration dominates p99 and
    makes the comparison noisy.
    """
    print(f"Benchmarking /api/foryou (limit={limit}, {n} iterations, "
          f"{warmup} warmup)...")

    # Warmup
    for _ in range(warmup):
        await _run_once(limit)

    # Timed
    times: list[float] = []
    for i in range(n):
        ms = await _run_once(limit)
        times.append(ms)
        # Progress every 10% for long runs
        if n >= 50 and (i + 1) % (n // 10) == 0:
            print(f"  ...{i + 1}/{n} done")

    times.sort()
    p50 = times[n // 2]
    p95 = times[int(n * 0.95)]
    p99 = times[int(n * 0.99)]
    mean = statistics.mean(times)
    stdev = statistics.stdev(times) if n > 1 else 0.0

    print()
    print(f"  p50   {p50:8.2f} ms")
    print(f"  p95   {p95:8.2f} ms")
    print(f"  p99   {p99:8.2f} ms")
    print(f"  mean  {mean:8.2f} ms")
    print(f"  stdev {stdev:8.2f} ms")
    print(f"  min   {times[0]:8.2f} ms")
    print(f"  max   {times[-1]:8.2f} ms")
    print()
    print("Compare with: the OTHER checkout's numbers.")
    print("Look for: lower mean (embedding + numpy elimination) and")
    print("          flatter p99 (no 500-element numpy.dot spike).")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark /api/foryou in-process",
    )
    parser.add_argument(
        "-n", "--iterations",
        type=int, default=100,
        help="Number of timed iterations (default: 100)",
    )
    parser.add_argument(
        "--limit",
        type=int, default=200,
        help="Feed limit, same as the API max (default: 200)",
    )
    parser.add_argument(
        "--warmup",
        type=int, default=5,
        help="Warmup iterations to discard (default: 5)",
    )
    args = parser.parse_args(argv)

    asyncio.run(bench(args.iterations, args.limit, args.warmup))

    # Clean up the engine pool
    asyncio.run(engine.dispose())


if __name__ == "__main__":
    main()