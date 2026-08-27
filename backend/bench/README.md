# PR #94 Benchmark Suite

Three layers of before/after verification for the `/foryou` performance
changes in PR #94. Start at layer 1 (cheapest); if those numbers confirm
the improvements, layers 2–3 prove the end-to-end win.

## Files

| File | What it measures | Time to run |
|---|---|---|
| `explain_queries.sql` | DB-level: index usage, buffer reads, query count | 2 min |
| `profile_foryou.py` | Python-side: in-process latency percentiles | 5 min |
| `locustfile.py` | End-to-end: HTTP latency under concurrency | 10 min |

## Quick start (5 minutes)

```bash
# 1. Apply migration 0026 (the composite index)
alembic upgrade head

# 2. DB level — paste into psql
psql $DATABASE_URL -f backend/bench/explain_queries.sql

# 3. Python level
cd backend
python -m bench.profile_foryou --n 100
```

## Full before/after comparison

You need two checkouts: the parent of PR #94 (`e85399e~1`) and `main`.

### Layer 1: Database (2 min)

```bash
# Before:
git checkout e85399e~1
psql $DATABASE_URL -f backend/bench/explain_queries.sql

# After:
git checkout main
alembic upgrade head
psql $DATABASE_URL -f backend/bench/explain_queries.sql
```

**P2 (composite index):** Look for `Index Scan using ix_entries_fetched_at_composite_score` with no `Sort` node in the after output.

**P1 (embedding drop):** Compare `Buffers: shared hit` — should drop by ~190 pages (1.5 MB).

### Layer 2: Python in-process (5 min)

```bash
cd backend

# Before:
git checkout e85399e~1
python -m bench.profile_foryou --n 100

# After:
git checkout main
python -m bench.profile_foryou --n 100
```

**Expected:** 3–10× lower mean latency. The old code did 500 `numpy.dot()` calls per request (~25 ms); the new code does 500 float multiplies (~0.05 ms).

### Layer 3: HTTP load test (10 min)

```bash
pip install locust

# Before:
git checkout e85399e~1 && docker compose up -d --build
locust -f backend/bench/locustfile.py -u 20 -r 5 -t 60s --headless \
    --csv=before --host=http://localhost:8000

# After:
git checkout main && docker compose up -d --build
locust -f backend/bench/locustfile.py -u 20 -r 5 -t 60s --headless \
    --csv=after --host=http://localhost:8000
```

Compare `before_stats.csv` and `after_stats.csv`. Look for:
- Lower p50/p95/p99 response time
- Higher RPS
- Flatter p99 (the old code's 500 numpy dot products spike under concurrency)

### Quick payload size check

```bash
# Before:
git checkout e85399e~1 && docker compose up -d --build
curl -s -w "size=%{size_download} time=%{time_total}\n" \
    "http://localhost:8000/api/foryou?limit=200" -o /dev/null

# After:
git checkout main && docker compose up -d --build
curl -s -w "size=%{size_download} time=%{time_total}\n" \
    "http://localhost:8000/api/foryou?limit=200" -o /dev/null
```

`size=` should drop by ~1.5 MB (the `Vector(384)` embedding column).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `POPPING_BENCH_TOKEN` | (empty) | Bearer token for locust (skip if `local_auth_bypass=true`) |
| `POPPING_BENCH_LIMIT` | `200` | Feed limit for locust (API max is 200) |
| `POPPING_POSTGRES_HOST` | `postgres` | DB host (override when running outside Docker) |

## What each PR #94 change addresses

| Change | Problem | Fix | Verified by |
|---|---|---|---|
| P1: Drop `embedding` from SELECT | 500 × 384-dim vectors (~1.5 MB) read per request; 500 numpy cosine sims in Python | Slim SELECT; rely on stored `composite_score` | `profile_foryou.py`, `explain_queries.sql` |
| P2: Composite index `(fetched_at, composite_score DESC)` | Brief selector does `WHERE fetched_at >= X ORDER BY composite_score DESC` — needs two indexes, does a sort | One composite index, no sort | `explain_queries.sql` |
| P3: JOIN Source in same query | Old code did a second `SELECT ... WHERE id IN (...)` to hydrate Source rows | JOIN Source in the candidate query | `pg_stat_statements` query in `explain_queries.sql` |