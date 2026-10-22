# PR4b: Window parameters and by-ids projection

Backend track. Backend changes and pytest only; no frontend, flows or smoke states. The new
parameters do nothing until PR4c and PR4d send them. Two changes apply to old bundles at once:
For You leaves out paused feeds, and both list routes leave out the caller's hidden stories.

## What changed

- **`GET /api/entries`** (`backend/app/routes/entries.py`):
  - `max_age_hours` (1..8760) keeps stories with `fetched_at >= now − h`. The window is by
    arrival, so a story dated last week that landed today is in a 2-day window.
  - `per_source_cap` (1..200) keeps each feed's best N stories in the window, ranked by
    `composite_score desc, fetched_at desc, id desc` (D28). The tie-breakers make the cut the
    same on every call, because one poll's rows share a `fetched_at`. Without `max_age_hours`
    it is a 422 (`per_source_cap needs max_age_hours`).
  - `active_only` leaves out paused feeds.
  - **Hidden stories:** without `q`, the caller's `hidden_entries` are left out. The route
    gained `user = Depends(current_user)`, and the lookup is keyed by `resolve_user_id(user)`.
    Search (`q`) still finds a hidden story.
  - **Where the filters go.** Every filter is built once, as feed filters (category, source,
    `active_only`) and story filters (`q`, window, hidden). Each path puts them in its innermost
    select, so the outer selects built over subqueries can't drop them.
  - **Per-category path** (`per_category_limit` with no category, source or `q`):
    - With the cap, a `row_number()` per feed is computed and cut in an inner subquery
      (`capped`) before the per-category `row_number`, so a category still gets its full
      `per_category_limit` from what the cap leaves.
    - With a window, the per-category `row_number` orders by `fetched_at desc, published_at
      desc nulls last, id desc` instead of the score, so a new low-score story isn't cut. The
      response is newest arrival first too (see Deviations).
    - Without a window, the order is unchanged.
  - **Flat path** (category, source list, search, or a plain top-`limit`):
    - With a window, `ORDER BY fetched_at desc, published_at desc nulls last, id desc`, then
      `LIMIT`. A wide tab is cut by arrival, not to its top `limit` by score.
    - With the cap, each matching feed's best N come from a `LATERAL` top-N (see Deviations).
      It uses the same order as the per-category path's `row_number`.
  - Helpers: `_CARD_COLUMNS` / `_CARD_COLUMN_NAMES` (the card projection, unchanged from
    today's `columns` tuple), `_card_out` (the text-to-int coercion that was inline),
    `window_start`, `not_hidden`, `_arrival_order`, `_score_order`, `_best_in_source_order`
    and `_rank_in_source`.
- **Hidden ids** (`routes/preferences.py`): `hidden_entry_ids(session, user_id)` loads the row
  once and reads it through PATCH's own coercion (`_coerce_ids` then `_trim_ids`). It keeps only
  real ints (`type(x) is int`, so a JSON `true` is dropped) from 1 to 2**63-1, de-duplicated, and
  the newest 1000 by id. A non-list or malformed row reads as fewer ids, never as an error.
  `_is_entry_id` became `is_entry_id`, because by-ids now uses the same range check.
- **`not_hidden`** binds the ids as one `BIGINT[]` parameter: `entries.id <> ALL(:hidden_ids)`.
- **`GET /api/foryou`** (`routes/foryou.py`): the same `max_age_hours` window and hidden-story
  exclusion, with the same defensive read, now keyed by `resolve_user_id(user)`. It also has an
  unconditional `.where(Source.active.is_(True))` next to the historical-feed exclusion. All
  three filters run before the over-fetch, so they never use up candidate slots.
- **`GET /api/entries/by-ids`**:
  - It selects `_CARD_COLUMNS` plus `Source.name` and returns `EntryOut`. `EntryOut` is now
    `EntryListOut` plus `source_name: str`, the same shape as the frontend's `EntryOut extends
    Entry`. The full `meta` blob is gone; Settings' Hidden, Starred and Voted lists
    (`Settings.tsx:1657`, `:1730`, `:1815`) read only `id`, `title`, `url`, `source_name` and
    `published_at`.
  - Ids that aren't numbers or are outside 1..2**63-1 are dropped. Before, an id past the
    BIGINT range reached the driver and the call returned a 500.
  - The misleading "same column projection" comment is gone; the docstring now matches the
    route's gating (`_api_deps`).
- **`schemas.py`:** the old `EntryOut` (with `meta`) had no other user, so it was replaced, and
  `EntryListOut`'s docstring no longer points at it.
- **README.md:** the API table lists the new parameters, the hidden-story and paused-feed
  rules, and the by-ids route.
- No migration, no new index.

## Tests

- `test_entries_window.py` (24 tests, 36 cases):
  - no parameters: old stories and the score order stay, on both paths;
  - the window filters by arrival on both paths, and 8760 keeps a year;
  - a new low-score story is kept under the limit on the flat path and under
    `per_category_limit` on the per-category path; the windowed per-category response is
    newest first;
  - window and cap bounds (422);
  - the 422 for a cap without a window (flat, per-category, category);
  - the cap keeps a feed's best-scoring stories, not its newest (both paths);
  - 30 rows from one feed in one transaction (one `fetched_at`, tied scores): the cap keeps the
    25 best by score then id, and two calls return the same ids in the same order (both paths);
  - the cap runs before the per-category cut (25 + 15, not 30 + 10 then 35), and leaves other
    categories their slice;
  - the flat cap with a category filter, with a source list, and with search;
  - 8760 plus the cap returns 200 on all three shapes;
  - `active_only` on both paths, with and without the cap;
  - hidden stories left out on 6 shapes, never using up a category's slots, still found by
    search, keyed by the resolved user (another user's row doesn't apply; with the LAN bypass
    on, the bypass user's row does);
  - a malformed `hidden_entries` value (dict, string, null, a list of wrong shapes including
    `true`, `"1"`, `1.0`, `2**63`) returns 200 and hides nothing; a mixed row keeps its real ids;
    only the newest 1000 ids are read.
- `test_foryou_window.py` (7 tests, 14 cases): the window by arrival and its bounds; a paused
  feed's story inside the window is absent (no params, windowed, by category); hidden stories
  left out and not using up `limit`; another user's row doesn't apply; malformed rows return 200.
- `test_entries_by_ids_projection.py` (4 tests): a seeded row returns `image_path`,
  `image_url`, `cached_summary`, `reddit_thread_url`, `reddit_comment_count`, `audio_url`,
  `duration_seconds`, `transcript_url`, `story_cluster_id` and `source_name`, and no `meta`; the
  row equals `/api/entries`' row plus `source_name`; input order is kept and bad ids are dropped
  with a 200.
- Unchanged and green: `test_entries_per_category.py` (no window), `test_oidc_route_gating.py`,
  `test_preferences_delta.py`, `test_foryou_excludes_historical.py`.

## Verification

- Full backend pytest on `popping_test`: **13 failed, 948 passed**. The 13 failures diff clean
  against the DNS-only baseline list (`/root/popping-env/baseline.txt`). 948 = main's 895 + 53
  new cases. Run before and after the review fixes.
- `ruff check --select F` on every changed backend file: clean.
- No frontend files changed, so frontend lint, build, unit tests and smoke weren't needed. No
  backend test that pins frontend source was touched.
- **Mutation checks.** A script broke each guarded line, ran the named tests with `-x`,
  expected a failure, restored the file byte for byte, and checked it. The working-tree diff's
  hash was identical before and after. 26 of 26 were caught:

  | Broken line | Failing test |
  |---|---|
  | window filter dropped / on `published_at` | `test_window_filters_by_arrival_on_both_paths` |
  | no 422 for a cap without a window | `test_cap_without_a_window_is_a_422` |
  | per-category `row_number` by score when windowed | `test_per_category_window_cuts_by_arrival_…` |
  | per-category response by score when windowed | `test_per_category_window_cuts_by_arrival_…` |
  | flat cut by score when windowed | `test_flat_window_cuts_by_arrival_…` |
  | cap by newest instead of best | `test_cap_keeps_a_feeds_best_scoring_stories_not_its_newest` |
  | cap tie-break on `id` flipped | `test_cap_on_one_polls_rows_is_deterministic` |
  | per-category path ignores the cap | `test_per_category_cap_runs_before_the_category_cut` |
  | flat LATERAL without its `LIMIT` | `test_flat_cap_with_a_category_filter` |
  | flat LATERAL without the story filters / without the feed filters | `test_flat_cap_with_search`, `test_flat_cap_with_a_category_filter` |
  | `active_only` ignored | `test_active_only_leaves_out_paused_feeds_on_both_paths` |
  | hidden as `<> ANY` (the spec's literal) | `test_hidden_stories_dont_use_up_a_categorys_slots` |
  | hidden never excluded / also excluded from search | `test_hidden_stories_are_left_out`, `test_search_still_finds_a_hidden_story` |
  | hidden read always for `anonymous` | `test_hidden_stories_are_keyed_by_the_resolved_user` |
  | hidden read accepts bools / untrimmed / not coerced | `test_a_malformed_hidden_row_returns_200`, `test_only_the_newest_1000_hidden_ids_are_read` |
  | For You keeps paused feeds / ignores the window / keeps hidden / reads another user | the matching `test_foryou_window.py` test |
  | by-ids without the card columns / without the id range check | `test_a_seeded_row_returns_the_card_fields`, `test_input_order_and_bad_ids` |

- **Security checklist** (`/security-review` couldn't run: the worktree has no `origin` remote,
  so its `git diff origin/HEAD...` fails; this is the Rules checklist, done by hand):
  - No new routes. `/api/entries`, `/api/entries/by-ids` and `/api/foryou` stay on their
    routers inside `main.py`'s `_api_deps` loop; `test_oidc_route_gating.py` passes.
  - No state-changing route, and the GETs gain no side effects.
  - The one server read of client-written preference JSON (`hidden_entries`) is coerced and
    capped at 1000.
  - Every value is a bound parameter; the hidden ids are one typed `BIGINT[]`.
  - Every new parameter is range-checked, and the cap can't run without a window. by-ids still
    caps at 200 ids and now drops out-of-range ids instead of returning a 500.
  - The hidden lookup reads only the caller's own row (`resolve_user_id`). by-ids exposes less
    than before: no `meta`.
  - No new network I/O.

### EXPLAIN (ANALYZE, BUFFERS) on 400k rows

- **Setup:** a scratch database (`popping_explain`) on the local Postgres 16, migrated with
  `alembic upgrade head`, so it has production's indexes, including 0026/0028 and
  `ix_entries_source_score`, which `create_all` lacks.
  - 120 feeds in 12 categories, 5 of them paused. 400k entries whose arrivals span 540 days,
    40% of them in 6 busy feeds.
  - Every row has a 384-dim embedding and a ~300-character meta summary, so the heap is
    production-wide (988 MB with indexes). 1456 rows arrived in the last 48 h.
  - A 1000-id `hidden_entries` row. `VACUUM ANALYZE`.
- **Method:** the real route functions built the statements, with a session proxy that ran
  `EXPLAIN (ANALYZE, BUFFERS)` on each.
  - "Custom" inlines the bound values. That is the plan a parameterized query gets for its first
    5 runs, and afterwards while its generic plan's estimate is higher.
  - "Generic" is `PREPARE` + `force_generic_plan`.
  - Requests as PR4d will send them: the main poll is `per_category_limit=100, per_source_cap=25,
    active_only=true`. A wide tab is `category=cat_3` (10 feeds), `limit=200`, same cap. A custom
    tab is a 5-feed source list. For You is `limit=50`.
  - Postgres defaults (`jit=on`, `work_mem=4MB`, `shared_buffers=128MB`), 4 cores, warm cache.
    Database dropped afterwards.

  | Query | 48 h | 8760 h |
  |---|---|---|
  | per-category (main poll) | **24 ms** (generic 161 ms) | 1684 ms, of which JIT 1021 ms; 1184 ms with JIT off (generic 1027 ms) |
  | flat, category | **10.6 ms** (generic 69 ms) | **3.6 ms** (generic 3.5 ms) |
  | flat, source list | **6.0 ms** | **1.9 ms** |
  | For You | **2.0 ms** | **2.5 ms** |
  | today's main poll (per-category, no window), for comparison | 1641 ms (1300 ms with JIT off) | |

  - The generic plans' estimated costs are 13-28× their custom plans' (per-category 48 h:
    211 934 vs 7662; flat 48 h: 13 538 vs 1333). Under the default `plan_cache_mode = auto`,
    Postgres therefore keeps the custom plans.
  - **Per-category 48 h** (the hot path):
    - Bitmap scan on `ix_entries_fetched_at` (1446 rows), with `id <> ALL(<1000 ids>)` as a
      heap filter and a Memoize'd `sources_pkey` probe (`active IS TRUE`).
    - Then the per-feed `WindowAgg` (Run Condition `<= 25`, 1405 → 986 rows) and the
      per-category `WindowAgg` (Run Condition `<= 100`).
    - Buffers: shared hit=720 read=982.
  - **Flat category 48 h:** a nested loop over the 10 matching feeds. Each feed's LATERAL is a
    `BitmapAnd` of `ix_entries_fetched_at` and `ix_entries_source_id`, a top-N sort and `Limit
    25`. Then an outer sort by arrival and `Limit 200`. Buffers: shared hit=265 read=42.
  - **Flat category 8760 h:** the same loop, but each feed's LATERAL is an `Index Scan using
    ix_entries_source_score` (Index Cond `source_id = sources.id`, Filter on the window and the
    hidden ids). It is followed by an `Incremental Sort` on the tie-breakers and stops after 26
    rows per feed.
    - With the spec's `row_number()` subquery instead, the same request took **430 ms**: an
      incremental sort of all 30 959 window rows, 45 727 heap blocks read, and an external merge
      to disk. That is why the flat path uses `LATERAL` (Deviations).
  - **For You 48 h:** `Index Scan using ix_entries_composite_score_fetched_at` with the window
    as an Index Cond and the hidden ids as a filter. 204 rows read for the 200-row over-fetch.
  - **Per-category 8760 h misses the 150 ms target.**
    - It ranks every row in the year (a parallel bitmap heap scan of 270k rows, 99k blocks, an
      external sort).
    - No planned caller sends it: PR4d's main poll sends 48 h, and wider tabs, custom tabs and
      All go through the flat path.
    - It costs the same as today's unwindowed main poll on this table, so nothing gets slower.
    - A `LATERAL` per-category form was measured too: 24 ms at 8760 h, but 69 ms at 48 h,
      148 ms at 168 h and 411 ms at 720 h. It would make the hot 48 h poll 3× slower, so it
      wasn't used. See Open items.

- **VPS (read-only, optional).** Two `EXPLAIN (ANALYZE, BUFFERS)` runs against production,
  through the compose `postgres` service (default user and database `popping`):
  - **Per-category main poll, 48 h:** what PR4d will send every minute from every device
    (`per_category_limit=100, max_age_hours=48, per_source_cap=25, active_only=true`).
  - **Wide tab, window All (8760 h):** one category, `limit=200`, the same cap and
    `active_only`. The flat path's `LATERAL` form exists for this shape. For a wide tab at 48 h,
    change `'8760 hours'` to `'48 hours'`.
  - The two `SELECT`s are the route's own SQL. A script ran `list_entries` with a capturing
    session, compiled the statement it built, and inlined the values: the full card projection
    with its `meta ->>` extractions, the casts the driver sends, the per-feed and per-category
    `row_number`s or the `LATERAL`, and `id <> ALL(...)`.
  - Two values come from the database instead, through psql variables. `:'hidden'` is the
    most recently written `hidden_entries` row, read as the route reads it (whole-number ids
    from 1 to 2^63-1, no duplicates, the newest 1000). `:'cat'` is the category with the most
    active feeds. The window is `now() - interval` (the route binds the same instant as a
    parameter), so the command works on any day. An empty hidden list gives `'{}'`, where the
    route would leave the filter out; the plan is the same apart from that one filter.
  - Checked on a scratch database migrated to head (16 feeds, 3000 stories, a malformed
    hidden row next to an older one): without `EXPLAIN`, both `SELECT`s returned the same ids
    in the same order as `list_entries` (275 and 150 rows).

  ```
  cd /opt/popping && docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U popping -d popping <<'SQL'
  -- The hidden ids, read the way the route reads them: the most recently
  -- written hidden_entries row, whole-number ids from 1 to 2^63-1, no
  -- duplicates, the newest 1000.
  SELECT coalesce(array_agg(id), '{}') AS hidden FROM (
    SELECT DISTINCT x::text::numeric AS id
    FROM (SELECT value FROM user_preferences
          WHERE key = 'hidden_entries' AND jsonb_typeof(value) = 'array'
          ORDER BY updated_at DESC LIMIT 1) p
    CROSS JOIN jsonb_array_elements(p.value) AS x
    WHERE jsonb_typeof(x) = 'number' AND x::text ~ '^[0-9]+$'
      AND x::text::numeric BETWEEN 1 AND 9223372036854775807
    ORDER BY id DESC LIMIT 1000) h \gset
  -- The category with the most active feeds, for the wide-tab shape.
  SELECT category AS cat FROM sources WHERE active IS TRUE
  GROUP BY category ORDER BY count(*) DESC, category LIMIT 1 \gset
  -- Per-category main poll, 48 h.
  EXPLAIN (ANALYZE, BUFFERS) SELECT ranked.id, ranked.source_id, ranked.title, ranked.url, ranked.published_at, ranked.fetched_at, ranked.composite_score, ranked.personal_score, ranked.raw_score, ranked.cached_summary, ranked.image_url, ranked.image_path, ranked.story_cluster_id, ranked.reddit_thread_url, ranked.reddit_comment_count_text, ranked.audio_url, ranked.duration_seconds_text, ranked.transcript_url
  FROM (SELECT pool.id AS id, pool.source_id AS source_id, pool.title AS title, pool.url AS url, pool.published_at AS published_at, pool.fetched_at AS fetched_at, pool.composite_score AS composite_score, pool.personal_score AS personal_score, pool.raw_score AS raw_score, pool.cached_summary AS cached_summary, pool.image_url AS image_url, pool.image_path AS image_path, pool.story_cluster_id AS story_cluster_id, pool.reddit_thread_url AS reddit_thread_url, pool.reddit_comment_count_text AS reddit_comment_count_text, pool.audio_url AS audio_url, pool.duration_seconds_text AS duration_seconds_text, pool.transcript_url AS transcript_url, row_number() OVER (PARTITION BY pool.source_category ORDER BY pool.fetched_at DESC, pool.published_at DESC NULLS LAST, pool.id DESC) AS rn
  FROM (SELECT capped.id AS id, capped.source_id AS source_id, capped.title AS title, capped.url AS url, capped.published_at AS published_at, capped.fetched_at AS fetched_at, capped.composite_score AS composite_score, capped.personal_score AS personal_score, capped.raw_score AS raw_score, capped.cached_summary AS cached_summary, capped.image_url AS image_url, capped.image_path AS image_path, capped.story_cluster_id AS story_cluster_id, capped.reddit_thread_url AS reddit_thread_url, capped.reddit_comment_count_text AS reddit_comment_count_text, capped.audio_url AS audio_url, capped.duration_seconds_text AS duration_seconds_text, capped.transcript_url AS transcript_url, capped.source_category AS source_category
  FROM (SELECT entries.id AS id, entries.source_id AS source_id, entries.title AS title, entries.url AS url, entries.published_at AS published_at, entries.fetched_at AS fetched_at, entries.composite_score AS composite_score, entries.personal_score AS personal_score, entries.raw_score AS raw_score, entries.cached_summary AS cached_summary, entries.image_url AS image_url, entries.image_path AS image_path, entries.story_cluster_id AS story_cluster_id, entries.meta ->> 'reddit_thread_url'::VARCHAR AS reddit_thread_url, entries.meta ->> 'reddit_comment_count'::VARCHAR AS reddit_comment_count_text, entries.meta ->> 'audio_url'::VARCHAR AS audio_url, entries.meta ->> 'duration_seconds'::VARCHAR AS duration_seconds_text, entries.meta ->> 'transcript_url'::VARCHAR AS transcript_url, sources.category AS source_category, row_number() OVER (PARTITION BY entries.source_id ORDER BY entries.composite_score DESC, entries.fetched_at DESC, entries.id DESC) AS rn_src
  FROM entries JOIN sources ON entries.source_id = sources.id
  WHERE sources.active IS true AND entries.fetched_at >= (now() - interval '48 hours')::TIMESTAMP WITH TIME ZONE AND entries.id != ALL (:'hidden'::BIGINT[])) AS capped
  WHERE capped.rn_src <= 25::INTEGER) AS pool) AS ranked
  WHERE ranked.rn <= 100::INTEGER ORDER BY ranked.fetched_at DESC, ranked.published_at DESC NULLS LAST, ranked.id DESC;
  -- Wide tab (one category), window All (8760 h).
  EXPLAIN (ANALYZE, BUFFERS) SELECT best.id, best.source_id, best.title, best.url, best.published_at, best.fetched_at, best.composite_score, best.personal_score, best.raw_score, best.cached_summary, best.image_url, best.image_path, best.story_cluster_id, best.reddit_thread_url, best.reddit_comment_count_text, best.audio_url, best.duration_seconds_text, best.transcript_url
  FROM sources JOIN LATERAL (SELECT entries.id AS id, entries.source_id AS source_id, entries.title AS title, entries.url AS url, entries.published_at AS published_at, entries.fetched_at AS fetched_at, entries.composite_score AS composite_score, entries.personal_score AS personal_score, entries.raw_score AS raw_score, entries.cached_summary AS cached_summary, entries.image_url AS image_url, entries.image_path AS image_path, entries.story_cluster_id AS story_cluster_id, entries.meta ->> 'reddit_thread_url'::VARCHAR AS reddit_thread_url, entries.meta ->> 'reddit_comment_count'::VARCHAR AS reddit_comment_count_text, entries.meta ->> 'audio_url'::VARCHAR AS audio_url, entries.meta ->> 'duration_seconds'::VARCHAR AS duration_seconds_text, entries.meta ->> 'transcript_url'::VARCHAR AS transcript_url
  FROM entries
  WHERE entries.source_id = sources.id AND entries.fetched_at >= (now() - interval '8760 hours')::TIMESTAMP WITH TIME ZONE AND entries.id != ALL (:'hidden'::BIGINT[]) ORDER BY entries.composite_score DESC, entries.fetched_at DESC, entries.id DESC
   LIMIT 25::INTEGER) AS best ON true
  WHERE sources.category = :'cat'::VARCHAR AND sources.active IS true ORDER BY best.fetched_at DESC, best.published_at DESC NULLS LAST, best.id DESC
   LIMIT 200::INTEGER;
  SQL
  ```

  `EXPLAIN ANALYZE` of a `SELECT` runs it but writes nothing. The target is under 150 ms for
  each; locally on 400k rows they took 24 ms and 3.6 ms (the table above).

## Deviations from the spec

- **Hidden exclusion uses `<> ALL`, not `!= any_(...)`.** The spec's literal,
  `Entry.id != any_(bindparam('hidden', ...))`, renders `id <> ANY(array)`. That is true whenever
  the array holds any other id, so with two or more hidden ids it excludes nothing. `not_hidden`
  renders `entries.id <> ALL(:hidden_ids)`. The intent stands: one array parameter, not one per
  id. The mutation table shows the spec's form failing the two-id test.
- **The flat path's per-feed cap is a `LATERAL` top-N, not a `row_number()` subquery.** The
  result is identical: each feed's best N by `composite_score desc, fetched_at desc, id desc`,
  feed filters on the feed, story filters inside. The same tests pass on both forms. The
  difference is the plan: the `row_number` form ranked every row in the window, which took 430 ms
  for an All tab (8760 h) on 400k rows. The `LATERAL` form lets Postgres plan each feed on its
  own: a score-ordered index walk that stops at the cap for long windows (3.6 ms), and the window
  bitmap for short ones (10.6 ms at 48 h, against 11-16 ms for `row_number`). The per-category
  path keeps the spec's `row_number` form, because there the window scan is the cheaper plan at
  48 h (above).
- **The windowed per-category response is newest arrival first.** The spec changes only the
  per-category `row_number`'s order. The outer `ORDER BY` stayed on the score, which left the two
  paths answering one windowed request in different orders. Both now return arrival order when a
  window is set. The path returns every row it keeps, so this changes no set. Unwindowed, the
  order is unchanged.
- **`EntryOut` was redefined** (`EntryListOut` plus `source_name`) rather than adding a new
  schema. It had no other user, and the name now matches the frontend type it feeds.
- **by-ids drops out-of-range ids** (a 500 before). The spec didn't ask for this, but the route
  was being rewritten, and the same range check already guards the hidden-id read.
- **`is_entry_id`** in `preferences.py` lost its leading underscore, so by-ids can share it.

## Risks and data-compatibility notes

- **Revert is safe.** The parameters are additive and by-ids only gains fields (it loses `meta`,
  which no client reads). There is no migration.
- **Old bundles get two changes on deploy.** For You drops paused feeds' stories. Both list
  routes drop hidden stories, which old bundles already filter on the device.
- **One behaviour old bundles notice (accepted by the spec, fixed in PR4c):**
  - Undo of a hide after a poll, or Restore in Settings → Hidden, no longer brings the card
    back at once.
  - The old client only removes the id from its hidden set; the row has already left its
    `entries` state, so the card returns on the next poll (60 s) after the preference write
    lands.
  - PR4c re-inserts the cached entry on Undo.
- With more than 4 capped feeds in a tab, the 100-story cut by arrival can make that tab's
  effective window shorter than 2 days (the spec's risk).
- With OIDC on, `/api/entries` now decodes the session twice per request (the router's
  `require_user` and the route's `current_user`). Each decode is one sliding-expiry `UPDATE`.
  `/api/foryou` and the preference routes already do the same. With the LAN bypass on, each
  poll also logs one more `local-auth-bypass grant` INFO line.

## Open items for later units

- **PR4c:** by-ids now returns full cards (`EntryOut` = `Entry` + `source_name`). Point the
  `capture.cjs` by-ids mock at the same projection (no `meta`). Undo of a hide must re-insert the
  cached entry, because the server no longer returns hidden stories.
- **PR4d:** the windowed response on both paths is newest arrival first.
  - `per_source_cap` without `max_age_hours` is a 422.
  - `/api/foryou` takes `max_age_hours` only; paused feeds are always out.
  - `capture.cjs` should mirror the server: hidden stories out except under `q`, and arrival
    order when windowed.
- **Per-category path with a long window:** 1.2-1.7 s at 8760 h on 400k rows, the same as
  today's unwindowed poll. If a later unit ever sends a window wider than 48 h to the
  per-category path, give it the flat path's `LATERAL` form. An index on `(source_id,
  fetched_at)` would make both forms cheap at every window, but it needs a migration.
- **For You's projection** is still its own copy without `audio_url`, `duration_seconds`,
  `transcript_url` or `story_cluster_id`, so For You cards lack Listen, Summarize episode and
  Related coverage. `_CARD_COLUMNS` / `_card_out` can replace it (PR5a or PR7b, when cards
  change).
- **Double session decode:** `current_user` could cache its result on `request.state`, so the
  router dependency and the route share one decode (`auth/deps.py`, not frozen).
- **JIT:** on this table, today's unwindowed main poll spends 0.6-1.0 s of its 1.6 s in JIT.
  After PR4d the main poll sends 48 h and stays well under the JIT threshold.

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md`:

- [ ] #<pr> · Pause a feed that has a story in For You, then force-quit and reopen the app and open For You. Pass: none of that feed's stories are in For You (resume the feed afterwards).

## Review

`/code-review high` reported nine findings:

- **Fixed:**
  - The windowed paths answered in different orders. The per-category response is now newest
    first too, and a test and a mutation pin it.
  - The LATERAL comment overstated the index walk. It now says which plan each window gets.
  - The id-range check was duplicated. It is now one `is_entry_id`.
  - It pointed out that this notes file was missing; that was expected before commit.
- **Not changed, with reasons:**
  - Old bundles' Undo or Restore after a poll: accepted by the spec's Risks, fixed in PR4c.
  - LATERAL walking a quiet feed's whole history at 48 h: the measured plan uses the window
    bitmap at 48 h (10.6 ms), not the score walk.
  - The double session decode and the bypass log noise: the spec asks for
    `Depends(current_user)`, and `/api/foryou` and the preference routes already behave this
    way. Listed under Open items.
  - For You's own projection: outside this unit. Listed under Open items.
  - The malformed-row tests assume the first entry is id 1: the tests assert that
    precondition, and conftest's `TRUNCATE … RESTART IDENTITY` guarantees it.

`/security-review` could not run in this worktree (no `origin` remote); the Rules checklist
above was done by hand instead, with no findings.

### Review fixes (second commit)

One confirmed finding, fixed:

- **The VPS command covered only the flat path, and not as the route sends it.** It selected
  only `e.id`, so it skipped the card projection and the `meta ->>` extractions. It had no
  `id <> ALL(...)` filter, and it left out the per-category main poll, which every device will
  send each minute after PR4d. The VPS section now has one read-only psql run with two `EXPLAIN
  (ANALYZE, BUFFERS)`s, the per-category main poll at 48 h and a wide tab at 8760 h. Both are
  the SQL that `list_entries` compiles. The real hidden ids and the busiest category come from
  psql variables. The old flat command's "All-tab" label is gone: a category filter makes it a
  wide tab.
- **Checks:** the pasted command ran unchanged against a scratch database migrated to head,
  except for the psql connection: it ran with a hidden row, and again with no rows. Without
  `EXPLAIN`, its two `SELECT`s returned the same ids in the same order as the route. The four
  window and projection test files still pass (56 passed). No code or test changed.
