# Architecture Notes: In-Memory State & Single-Worker Constraint

## Overview

Popping's backend is designed for **single-process deployment**: one
`uvicorn` worker, one `AsyncIOScheduler` instance, one in-process
embedder. This document explains why, what state is process-local,
and what to change if you ever need multi-worker scaling.

## Process-Local State

The following in-memory state is **not shared across workers** and
will silently break if you run `uvicorn --workers > 1`:

| Location | Variable | Purpose |
|---|---|---|
| `app/scheduler.py` | `_scheduler` | The APScheduler instance — owns all cron/interval jobs |
| `app/scheduler.py` | `_brief_generator` | The BriefGenerator singleton |
| `app/sources/github_releases.py` | `_etag_cache` | Per-repo ETag cache for GitHub API 304s |
| `app/sources/dynamic_reddit.py` | `_warned_disabled` | One-shot warning dedup for disabled Reddit rows |
| `app/sources/generic_scrape.py` | `GenericScrapePlugin._extracted_urls` | Per-source URL dedup (bounded FIFO, 5000 entries) |
| `app/scoring/convergence.py` | process-level cache | 30s TTL convergence counts cache |
| `app/runtime_settings.py` | `_cache` | In-process app_settings cache |
| `app/request_state.py` | `_notifier` | Module-level notifier singleton |

## What Breaks

With N workers:
- **Scheduler fires N×**: each worker has its own `AsyncIOScheduler`,
  so every job runs N times per tick. Ingests duplicate (silently —
  the DB's `ON CONFLICT DO NOTHING` catches it), but brief generation,
  convergence alerts, and CVE notifications fire N times.
- **ETag cache is per-worker**: only the worker that saw the 304
  benefits from it; others re-fetch the full body.
- **`_extracted_urls` is per-worker**: each worker re-attempts URLs
  the DB already has (silently no-op'd by `ON CONFLICT DO NOTHING`).
- **Convergence cache is per-worker**: N redundant SQL queries per
  30s window instead of 1.
- **`_warned_disabled` is per-worker**: N× the warning log lines.

## How to Fix (Future)

If multi-worker is needed:
1. Swap `AsyncIOScheduler` for `RedisScheduler` (APScheduler supports
   this) so only one worker runs each job tick.
2. Move `_etag_cache` and `_extracted_urls` to Redis (or a DB table).
3. Move the convergence cache to Redis with a shared TTL.
4. The notifier singleton is already built from env on startup — no
   change needed, each worker builds its own (that's fine).

## Current Stance

The Dockerfile and `docker-compose.yml` are configured for a single
worker. The `--reload` flag in dev mode also implies a single worker.
Do NOT add `--workers N` to the uvicorn command without addressing
the above.