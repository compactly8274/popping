"""Locust load test for Popping's read/query surface — PR #95 benchmarks.

Hits the dashboard's read endpoints with configurable concurrency to
capture p50 / p95 / p99 latency and RPS per endpoint shape. Started as
a single-endpoint file for the /api/foryou before/after comparison in
PR #94; this revision extends it to the whole query surface the
dashboard exercises, because "query performance" is broader than one
endpoint:

    /api/entries                       plain top-N (the dashboard grid,
                                       hit on every refresh)
    /api/entries?category=X            one-column view
    /api/entries?source=a&source=b     multi-source column config
    /api/entries?q=term                ILIKE search across title +
                                       meta.summary — the most expensive
                                       predicate on the read path (no
                                       FTS index; one side is a JSONB
                                       ``->>`` extraction)
    /api/entries?per_category_limit=N  ROW_NUMBER() OVER (PARTITION BY
                                       category ORDER BY ...) windowed
                                       query (fair-slice mode)
    /api/foryou                        personal top-N — PR #94's
                                       optimized path
    /api/sources                       source list — startup path; also
                                       used by this file's own
                                       self-configuration (see below)

Every task names its request explicitly (``name=``) so the CSV output
keeps one stats row per endpoint shape — ``before_stats.csv`` vs
``after_stats.csv`` then compares p50/p95/p99 per endpoint instead of
blending everything into one row.

All the endpoint shapes above exist on both the "before" checkout
(``e85399e~1``) and ``main``, so a full-surface run is apples-to-apples
across the comparison. For the strict single-endpoint /api/foryou run
the README's layer-3 recipe describes, set ``POPPING_BENCH_MODE=foryou``
— that mode reproduces the original file's behavior (one task, one
request name, one CSV row).

Requires locust >= 2.0::

    pip install locust

Usage::

    # Full query-surface benchmark (default mode):
    locust -f backend/bench/locustfile.py -u 20 -r 5 -t 60s --headless \\
        --csv=after --host=http://localhost:8000

    # PR #94 /api/foryou-only before/after (the original recipe):
    POPPING_BENCH_MODE=foryou locust -f backend/bench/locustfile.py \\
        -u 20 -r 5 -t 60s --headless --csv=before --host=http://localhost:8000

Configuration via environment:

    POPPING_BENCH_TOKEN               Bearer token (omit when
                                      ``local_auth_bypass=true`` covers
                                      the benchmark client)
    POPPING_BENCH_LIMIT               Feed limit (default 200). Clamped
                                      per endpoint: /api/foryou maxes at
                                      200, /api/entries at 500 — one
                                      shared value can't 422 either.
    POPPING_BENCH_MODE                all (default) | queries | foryou —
                                      selects which tasks run
    POPPING_BENCH_SEARCH_TERM         ``?q=`` term for the search task
                                      (default "ai"). Keep it free of
                                      ``%`` / ``_`` / ``\\``: those hit
                                      the LIKE-escape bug that existed
                                      on the "before" checkout (fixed in
                                      PR #96) and would match nothing
                                      there, skewing the comparison.
    POPPING_BENCH_PER_CATEGORY_LIMIT  ``per_category_limit`` for the
                                      windowed-query task (default 10;
                                      API max 200)
    POPPING_BENCH_CATEGORIES          comma-separated categories; skips
                                      /api/sources discovery and uses
                                      these for the category task
    POPPING_BENCH_SOURCES             comma-separated source names; same
                                      idea for the multi-source task

Self-configuration: each simulated user GETs /api/sources once at
startup and adopts the returned category / source names for the filter
tasks, so the benchmark adapts to whatever sources the target
deployment actually has. Hardcoded names would simply match nothing
on a deployment without those sources and quietly turn the filter
tasks into cheap empty-result queries. Falls back to the documented
built-in names (news/tech/vulns categories; bbc_news/hn_top sources)
when discovery fails.

Auth: the default single-user deploy runs ``local_auth_bypass=true``
with loopback-only CIDRs — locust from localhost qualifies, no token
needed. Against a remote host or an OIDC deployment, set
``POPPING_BENCH_TOKEN`` (sent as ``Authorization: Bearer …``).
"""

from __future__ import annotations

import json
import logging
import os
import random
from urllib.parse import quote

from locust import FastHttpUser, between, task

logger = logging.getLogger("popping.bench")

_TOKEN = os.environ.get("POPPING_BENCH_TOKEN", "")
_LIMIT = int(os.environ.get("POPPING_BENCH_LIMIT", "200"))
_MODE = os.environ.get("POPPING_BENCH_MODE", "all").strip().lower()
_SEARCH_TERM = os.environ.get("POPPING_BENCH_SEARCH_TERM", "ai")
_PER_CATEGORY_LIMIT = int(os.environ.get("POPPING_BENCH_PER_CATEGORY_LIMIT", "10"))

# One POPPING_BENCH_LIMIT feeds both feed endpoints, but their ``le=``
# caps differ (/api/foryou: 200, /api/entries: 500). Clamp per endpoint
# rather than let a shared 500 turn into 422s on the foryou task — and
# note that with FastHttpUser a 422 would otherwise be counted as a
# *successful* request (see the DashboardReader docstring).
_FORYOU_LIMIT = max(1, min(_LIMIT, 200))
_ENTRIES_LIMIT = max(1, min(_LIMIT, 500))

_HEADERS: dict[str, str] = {}
if _TOKEN:
    _HEADERS["Authorization"] = f"Bearer {_TOKEN}"

_VALID_MODES = ("all", "queries", "foryou")
if _MODE not in _VALID_MODES:
    raise SystemExit(f"POPPING_BENCH_MODE must be one of {_VALID_MODES}, got {_MODE!r}")


def _csv_env(name: str) -> list[str] | None:
    """Comma-separated env var → list, or None when unset/empty."""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return None
    return [v.strip() for v in raw.split(",") if v.strip()]


# Explicit overrides win over discovery; both fall back to the built-in
# names when absent. See the module docstring for why discovery matters.
_ENV_CATEGORIES = _csv_env("POPPING_BENCH_CATEGORIES")
_ENV_SOURCES = _csv_env("POPPING_BENCH_SOURCES")

_FALLBACK_CATEGORIES = ["news", "tech", "vulns"]
_FALLBACK_SOURCES = ["bbc_news", "hn_top"]


def task_if(weight: int, modes: tuple[str, ...]):
    """``@task(weight)`` that only applies in the selected benchmark mode.

    ``_MODE`` is fixed at import time, so this statically prunes the
    class's task list — no runtime branching inside task bodies, and
    locust's weighted scheduling works exactly as with plain ``@task``.
    """

    def decorator(fn):
        if _MODE in modes:
            return task(weight)(fn)
        return fn

    return decorator


class DashboardReader(FastHttpUser):
    """One simulated dashboard client cycling the read endpoints.

    Uses ``FastHttpUser`` (uvloop + httpx) instead of plain ``HttpUser``
    (requests/urllib3) to keep client transport overhead out of the
    server-side numbers being measured. Wait time (0.5–1 s) matches the
    original single-endpoint version of this file so runs stay
    comparable across revisions.

    Response validation: every request uses ``catch_response=True`` and
    fails the sample on non-200 / non-JSON / unexpected shape. This is
    not optional polish — FastHttpUser does NOT auto-fail on HTTP
    4xx/5xx the way HttpUser does, so without explicit checks a 422
    (e.g. a bad ``limit``) would be counted as a successful request
    and quietly corrupt the before/after comparison.
    """

    wait_time = between(0.5, 1.0)

    # Per-user filter targets. Class-level defaults are the documented
    # built-ins; on_start REBINDS these as instance attributes after
    # discovery (assignment, not mutation — the shared class lists are
    # never modified).
    categories: list[str] = _ENV_CATEGORIES or list(_FALLBACK_CATEGORIES)
    sources: list[str] = _ENV_SOURCES or list(_FALLBACK_SOURCES)

    def on_start(self) -> None:
        if _ENV_CATEGORIES and _ENV_SOURCES:
            # Fully pinned via env — skip the discovery request.
            return
        self._discover()

    def _discover(self) -> None:
        """Fetch /api/sources once and adopt its real categories /
        source names for the filter tasks. The request is named
        separately from the benchmark task (``[discovery]``) so the
        one-time startup cost doesn't pollute the /api/sources stats row.
        """
        try:
            with self.client.get(
                "/api/sources",
                name="/api/sources [discovery]",
                headers=_HEADERS,
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code} (expected 200)")
                    return
                rows = json.loads(resp.content)
                if not isinstance(rows, list):
                    resp.failure("expected JSON list")
                    return
                cats = sorted({r["category"] for r in rows if r.get("category")})
                names = [r["name"] for r in rows if r.get("name")]
                if not _ENV_CATEGORIES and cats:
                    self.categories = cats
                if not _ENV_SOURCES and names:
                    self.sources = names
                logger.info(
                    "bench: discovered %d categories, %d sources",
                    len(self.categories), len(self.sources),
                )
        except Exception:
            # Best-effort: the fallback names keep the filtered tasks
            # running even when /api/sources is unreachable — every
            # other request will fail loudly anyway if the host is wrong.
            logger.warning("bench: /api/sources discovery failed; using fallbacks")

    def _get_list(self, path: str, name: str, *, allow_empty: bool = True) -> None:
        """GET ``path``; fail the sample unless it returns HTTP 200 with
        a JSON list body.

        ``allow_empty=False`` additionally fails a ``[]`` response — for
        the unfiltered headline endpoints an empty list means the run is
        pointed at the wrong host or an unpopulated feed, and a stream
        of "successful" 1 ms empty responses would fake a great
        benchmark. The filtered variants may legitimately match nothing,
        so they allow ``[]``.
        """
        with self.client.get(
            path, name=name, headers=_HEADERS, catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code} (expected 200)")
                return
            if not allow_empty and resp.content == b"[]":
                resp.failure("empty [] — wrong host or unpopulated feed?")
                return
            try:
                data = json.loads(resp.content)
            except ValueError:
                resp.failure("response is not JSON")
                return
            if not isinstance(data, list):
                resp.failure(f"expected JSON list, got {type(data).__name__}")

    # --- tasks -------------------------------------------------------------

    @task_if(3, ("all", "foryou"))
    def foryou(self) -> None:
        """GET /api/foryou?limit=N — PR #94's optimized path.

        ``limit=200`` (the API max) exercises the full over-fetch
        (``min(max(limit * 4, 200), 500)`` → 500 candidates), which is
        where the pre-#94 code paid 500 embedding reads + numpy cosines
        per request.
        """
        self._get_list(
            f"/api/foryou?limit={_FORYOU_LIMIT}",
            "/api/foryou",
            allow_empty=False,
        )

    @task_if(3, ("all", "queries"))
    def entries_default(self) -> None:
        """GET /api/entries?limit=N — the dashboard grid on refresh."""
        self._get_list(
            f"/api/entries?limit={_ENTRIES_LIMIT}",
            "/api/entries [default]",
            allow_empty=False,
        )

    @task_if(1, ("all", "queries"))
    def entries_category(self) -> None:
        """GET /api/entries?category=X — one-column view."""
        category = quote(random.choice(self.categories), safe="")
        self._get_list(
            f"/api/entries?category={category}&limit={_ENTRIES_LIMIT}",
            "/api/entries [category]",
        )

    @task_if(1, ("all", "queries"))
    def entries_sources(self) -> None:
        """GET /api/entries with 2-3 repeated ``source=`` params — the
        column-config view. Repeated params (not one comma-joined one)
        because that's the shape the route actually accepts
        (``list[str] | None = Query(...)``)."""
        k = min(3, len(self.sources))
        names = random.sample(self.sources, k)
        params = "&".join(f"source={quote(n, safe='')}" for n in names)
        self._get_list(
            f"/api/entries?{params}&limit={_ENTRIES_LIMIT}",
            "/api/entries [sources]",
        )

    @task_if(1, ("all", "queries"))
    def entries_search(self) -> None:
        """GET /api/entries?q=term — ILIKE across title + meta.summary.

        The most expensive predicate on the read path (unanchored
        double-sided ILIKE over two columns, one of them a JSONB
        ``->>`` extraction). ``limit=50`` matches the endpoint's
        ``_SEARCH_LIMIT_CAP`` — larger values are clamped server-side
        anyway.
        """
        term = quote(_SEARCH_TERM, safe="")
        self._get_list(
            f"/api/entries?q={term}&limit=50",
            "/api/entries [search]",
        )

    @task_if(1, ("all", "queries"))
    def entries_per_category(self) -> None:
        """GET /api/entries?per_category_limit=N — the fair-slice
        windowed query. Only honored when category/source/q are all
        absent, which is exactly how it's requested here."""
        self._get_list(
            f"/api/entries?per_category_limit={_PER_CATEGORY_LIMIT}",
            "/api/entries [per-category]",
        )

    @task_if(1, ("all", "queries"))
    def sources(self) -> None:
        """GET /api/sources — the startup path. Not entirely trivial:
        the handler runs a per-source net-vote GROUP BY over the
        interactions table alongside the row scan."""
        self._get_list("/api/sources", "/api/sources", allow_empty=False)