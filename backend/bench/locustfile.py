"""Locust load test for /api/foryou — PR #94 before/after benchmark.

Hit the For You feed with configurable concurrency to capture
p50 / p95 / p99 latency and RPS. The old code pulled 500 × 384-dim
embedding vectors per request and did 500 numpy cosine similarities
in Python; the new code drops the embedding from the SELECT and
relies on the stored composite_score. Under load the old code's p99
should spike; after, it should stay flat.

Requires locust ≥ 2.0::

    pip install locust

Usage (see module docstring in the bench/ README for the full recipe)::

    locust -f backend/bench/locustfile.py \\
        -u 20 -r 5 -t 60s --headless \\
        --csv=before --host=http://localhost:8000

Configuration via environment:

    POPPING_BENCH_TOKEN   Bearer token (omit if local_auth_bypass=true)
    POPPING_BENCH_LIMIT   Feed limit (default 200, API max 200)

The token is optional because the default single-user deploy has
``local_auth_bypass=true`` with loopback-only CIDRs — locust hits
localhost, which qualifies. If you're testing against a remote
host or OIDC is on, set the token.
"""

from __future__ import annotations

import os

from locust import FastHttpUser, between, task


_TOKEN = os.environ.get("POPPING_BENCH_TOKEN", "")
_LIMIT = int(os.environ.get("POPPING_BENCH_LIMIT", "200"))

_HEADERS = {}
if _TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_TOKEN}"


class Reader(FastHttpUser):
    """Single-endpoint load test for /api/foryou.

    Uses ``FastHttpUser`` (uvloop + httpx) instead of plain ``HttpUser``
    (requests/urllib3) because the payload difference we're measuring
    (~1.5 MB of embedding data) is I/O-bound, and the fast transport
    isolates server time from client transport overhead.

    Wait time between requests is short (0.5–1 s) to keep throughput
    high without burning CPU on the locust side.
    """

    wait_time = between(0.5, 1.0)

    @task
    def foryou(self) -> None:
        """GET /api/foryou?limit=N — the hot path we're benchmarking.

        ``limit=200`` (the API max) exercises the full over-fetch path
        (``min(max(limit * 4, 200), 500)`` → 500 candidates), which is
        where the old code paid the embedding + numpy cost.
        """
        self.client.get(
            f"/api/foryou?limit={_LIMIT}",
            headers=_HEADERS,
            name="/api/foryou",
        )