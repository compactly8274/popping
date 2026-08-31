-- ─────────────────────────────────────────────────────────────────────
-- EXPLAIN queries for PR #94 before/after verification
-- ─────────────────────────────────────────────────────────────────────
--
-- Run each EXPLAIN (ANALYZE, BUFFERS) against both checkouts:
--
--   Before: git checkout e85399e~1   (parent of PR #94)
--   After:  git checkout main       (with migration 0026 applied)
--
-- What to look for is noted under each query.
--
-- ─────────────────────────────────────────────────────────────────────


-- ═══════════════════════════════════════════════════════════════════════
-- P2: composite index (fetched_at, composite_score DESC)
-- ═══════════════════════════════════════════════════════════════════════
--
-- This index helps the BRIEF selectors (_select_entries and
-- _select_entries_by_slug), NOT /foryou directly — /foryou has no
-- fetched_at filter, so it uses the existing composite_score index.
--
-- The brief selector query (from brief.py _select_entries):
--   WHERE fetched_at >= :since ORDER BY composite_score DESC LIMIT 500

EXPLAIN (ANALYZE, BUFFERS)
SELECT e.id, e.composite_score, e.title, e.source_id, s.name, s.category
FROM entries e
JOIN sources s ON e.source_id = s.id
WHERE e.fetched_at >= now() - interval '24 hours'
  AND s.name NOT IN ('wikipedia_on_this_day')
ORDER BY e.composite_score DESC
LIMIT 500;

-- BEFORE (no composite index):
--   Index Scan on ix_entries_fetched_at_desc → Filter → Sort node
--   (external merge or quicksort) on composite_score
--
-- AFTER (with ix_entries_fetched_at_composite_score):
--   Index Scan using ix_entries_fetched_at_composite_score
--   → NO Sort node → lower Buffers: shared hit


-- Same query with a smaller LIMIT (slug selector, LIMIT 30):

EXPLAIN (ANALYZE, BUFFERS)
SELECT e.id, e.composite_score, e.title, e.source_id, s.name, s.category
FROM entries e
JOIN sources s ON e.source_id = s.id
WHERE e.fetched_at >= now() - interval '24 hours'
  AND s.name NOT IN ('wikipedia_on_this_day')
ORDER BY e.composite_score DESC
LIMIT 30;


-- ═══════════════════════════════════════════════════════════════════════
-- P1: embedding column removal (wire payload)
-- ═══════════════════════════════════════════════════════════════════════
--
-- The /foryou query (from foryou.py). No fetched_at filter — just
-- ORDER BY composite_score DESC. Compare the "before" (with
-- e.embedding) and "after" (without).
--
-- Run the BEFORE version first (checkout e85399e~1), then the AFTER
-- version (checkout main). Compare the Buffers: line.

-- BEFORE (with embedding — the old code pulled Vector(384) per row):

EXPLAIN (ANALYZE, BUFFERS)
SELECT e.id, e.source_id, e.title, e.url, e.published_at, e.fetched_at,
       e.composite_score, e.personal_score, e.raw_score,
       e.image_url, e.image_path, e.cached_summary,
       e.meta->>'reddit_thread_url',
       e.meta->>'reddit_comment_count',
       e.embedding,
       s.category, s.source_weight
FROM entries e
JOIN sources s ON e.source_id = s.id
ORDER BY e.composite_score DESC, e.published_at DESC NULLS LAST
LIMIT 500;

-- AFTER (without embedding — the new slim SELECT):

EXPLAIN (ANALYZE, BUFFERS)
SELECT e.id, e.source_id, e.title, e.url, e.published_at, e.fetched_at,
       e.composite_score, e.personal_score, e.raw_score,
       e.image_url, e.image_path, e.cached_summary,
       e.meta->>'reddit_thread_url',
       e.meta->>'reddit_comment_count',
       s.category, s.source_weight
FROM entries e
JOIN sources s ON e.source_id = s.id
ORDER BY e.composite_score DESC, e.published_at DESC NULLS LAST
LIMIT 500;

-- EXPECTED:
--   Buffers: shared hit drops by ~190 pages (8KB × 190 ≈ 1.5MB)
--   Execution Time drops (less data to read and serialize)
--   Sort node (if present) has lower Memory: used


-- ═══════════════════════════════════════════════════════════════════════
-- P3: JOIN vs second round-trip (query count)
-- ═══════════════════════════════════════════════════════════════════════
--
-- This doesn't show in a single EXPLAIN — it's about query COUNT.
-- The old code did: SELECT entries ... then SELECT sources WHERE id IN (...)
-- The new code does: SELECT entries JOIN sources (one query)
--
-- Option A: pg_stat_statements (requires the extension to be enabled)
-- Run your benchmark (locust or profile_foryou.py), then check:

SELECT query, calls, total_exec_time, mean_exec_time, rows
FROM pg_stat_statements
WHERE query LIKE '%entries%'
  AND (query LIKE '%sources%'
       OR query LIKE '%source_id%')
ORDER BY total_exec_time DESC
LIMIT 10;

-- BEFORE: two distinct query shapes (entries SELECT + sources IN)
-- AFTER:  one query shape (entries JOIN sources)
-- The "calls" column for the sources-IN query should drop to 0 after.