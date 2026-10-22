# PR4a: Ingest rules, brief guard, On this day

Backend track. Backend changes and pytest only; no frontend, flows or smoke states.

## What changed

- **`app/ingest_rules.py` (new, pure).** `select_items_for_ingest(items, *, known, first_fetch,
  baseline_at, now, keep_first=10, backfill_h=48, max_age_d=30, per_poll=50, max_undated=2) →
  (kept, counts)`. No I/O, no clock.
  - First fetch: the feed's newest `keep_first` (dated newest first, then undated in feed order),
    plus every item dated within `backfill_h`. The newest are ranked over the whole listing,
    already-stored items included: those use up rank slots but aren't stored again. So a resumed
    feed, or one whose recent stories another feed already stored, doesn't fill the slots with
    older back catalogue.
  - Later polls: drop items dated before `baseline_at − backfill_h` (`before_baseline`) or before
    `now − max_age_d` (`too_old`). An undated item is kept only above the first known URL in feed
    order, at most `max_undated` per poll, topmost first (`undated`).
  - Then `per_poll` caps the new items (`per_poll`): undated survivors take their slots first,
    then dated ones newest first. The overflow is dated, stays unknown, and the next poll stores
    it. An undated item cut here would be lost, because once the dated items above it are stored
    it sits below a known URL.
  - Known URLs and repeats within the same listing are duplicates, not drops. `kept` is in feed
    order, so insert order is unchanged.
  - `parse_tunables` reads the six `ingest.*` knobs and falls back to the default when a value is
    missing, not a number or out of range, like `brief._resolve_window_hours`. Ranges:
    `first_fetch_keep` 1–200, `backfill_hours` 1–720, `max_age_days` 1–3650, `max_per_poll`
    1–1000, `resume_gap_hours` 48–8760, `max_undated_per_poll` 0–100. The 48 h floor on the
    resume gap is there because a feed can poll as slowly as every 24 h (`_REFRESH_MAX`); a gap at
    or under that would make every poll of a slow feed a first fetch.
- **`scheduler._ingest`.**
  - `_ingest_tunables()` reads the knobs with `runtime_settings.get_many`. They have no env field
    (`config.py` is frozen), so an unset key reads as None, isn't cached, and gets its default.
  - The commit session runs `_load_profile` first, because it commits when it creates the profile
    row. It then loads the feed's row `FOR NO KEY UPDATE` (with `populate_existing`), so it holds
    a row lock until its commit. The entry inserts' foreign-key `KEY SHARE` locks don't conflict
    with it.
  - **In-flight guard.** The address being fetched is `(url, sitemap_url, link_pattern)`. A
    row-driven plugin records it when it is built (`SourcePlugin.fetch_target`, set from the row
    by `base.row_fetch_target` in all five dynamic plugins), because its job keeps that instance
    until `update_source` replaces it after the edit's commit. A class-driven built-in has no
    `fetch_target` (its address can't be edited), so the first session's row read is the
    snapshot. If the locked row in the commit session differs, the batch is discarded and the
    bookkeeping skipped.
  - The known-URL query runs first, then `_is_first_fetch`, then the rules. Only kept items reach
    the embedder.
    - `_is_first_fetch` is true when `last_fetch_at` is NULL, or when it is more than
      `resume_gap_hours` ago.
    - Otherwise it is true when `NOT EXISTS (entries WHERE source_id = id AND fetched_at >=
      created_at)`, a single EXISTS on the `source_id` index.
  - **Baseline.** A first-fetch poll that inserts at least one row sets `created_at = now()`.
    That is the transaction start time, the same value its rows get as `fetched_at`, so the
    EXISTS holds from the next poll on. An empty first fetch, or one whose items were all known,
    leaves `created_at` alone.
  - `summary["skipped"]` is the number of rows dropped by the rules. `summary["duplicates"]` is
    known URLs and repeats, plus insert conflicts as before. A skip logs one info line with the
    count per reason. `IngestResult.skipped: int = 0` was added.
- **`update_source`.**
  - It loads the row with the same `FOR NO KEY UPDATE` lock plus `populate_existing`.
  - When `url`, `sitemap_url` or `link_pattern` changes, it sets `last_fetch_at = None` and
    `created_at = clock_timestamp()`.
- **Brief guard.** `brief._not_backfill(h)` is `published_at IS NULL OR published_at >=
  fetched_at − h`. It is added to `_select_entries` and to `_select_entries_by_slug` (the
  convergence alert path). `h` is `ingest.backfill_hours`, read through the same
  `runtime_settings.get_many` + `parse_tunables` path.
- **On this day (D17).**
  - `wikipedia_on_this_day` keeps `selected` only, at most 5 a day. It skips a second anniversary
    that links the same article, so that one doesn't use up a slot. Its class category is
    `on_this_day`.
  - Each URL carries the date: `#otd-YYYY-MM-DD`. The fallback URLs already have a `#slug`
    fragment, so for them the date is appended as `-otd-YYYY-MM-DD`.
  - `start_scheduler` calls `_move_builtin_categories()` before it adds any job. The function
    moves the existing row from `news` to `on_this_day` once, only while the row is still in
    `news`. It records `builtin_category_moved:wikipedia_on_this_day` in `app_settings` with
    `ON CONFLICT DO NOTHING`, and writes the marker even when the row doesn't exist, because a row
    created later gets the class's new category. A failure is logged, and the move retries on the
    next start.
  - `/api/foryou` excludes `brief._HISTORICAL_SOURCE_SLUGS` unconditionally, including with
    `?category=on_this_day`.
  - `scoring/recency._HALF_LIFE_HOURS['on_this_day'] = 6.0`.
- **README.md:** the `ingest.*` runtime knobs, the brief guard, and the On this day row in the
  sources table.

## Tests

All new tests are pytest. `tests/ingest_harness.py` holds the shared scaffolding: a fake
class-driven plugin, `age_feed`, which moves a feed's `created_at`, rows' `fetched_at` and
`last_fetch_at` back, and `install_offline_ingest`, which stubs the embedder, rescheduling and the
favicon/thumbnail/og:image passes. `conftest.py` exposes it as the `offline_ingest` fixture.

- `test_ingest_rules.py` (no_db, 29 tests): every rule and reason, ranking by date rather than
  feed order, undated last, the undated cap and its knob, per-poll after the date rules and on a
  first fetch, duplicates vs drops, naive and ISO dates, tunable parsing and fallback. Added in
  the review fixes: a resumed first fetch (dated and undated, with 0 or 4 new posts) that stores
  only the new posts; a new feed whose newest stories another feed stored; the per-poll cap by
  date on a listing in oldest-first order (later poll and first fetch); undated items getting
  per-poll slots before 60 dated ones, with all 62 landing over two polls.
- `test_ingest_backfill_cap.py` (db): the spec matrix through `_ingest`:
  - an empty success, then items 3 days later: 16 = max(10, the 16 inside 2 days) kept, 24
    skipped. The last success is set to 1 h ago, so only the EXISTS makes this a first fetch;
  - first fetch with only 2 recent stories: exactly the newest 10;
  - resumed after 21 days: 10 kept, 53 skipped;
  - the resume-gap boundary: 71 h gives later-poll rules, 73 h gives a first fetch;
  - a new item dated 5 days back on an hourly feed is kept;
  - 80 new dated items land 50 + 30 over two polls;
  - a first fetch over the cap (70 stories inside 2 days) lands 50 + 20;
  - an undated 60-item feed gives 10, then 0 for the same listing, then 2 of 5 new;
  - a weekly blog resumed after 28 days, dated and undated, with 0 or 4 new posts, stores only
    the new posts, not the 6 old ones below the 10 its first fetch stored;
  - the `ingest.first_fetch_keep` knob is honoured;
  - `IngestResult.skipped`;
  - every summary satisfies `fetched == inserted + duplicates + skipped`.
- `test_first_success_resets_baseline.py`:
  - a first fetch that stores rows moves `created_at`, to exactly the rows' `fetched_at`;
  - an empty first poll leaves it, and so does a first poll of only known URLs;
  - a later poll leaves it;
  - the poll right after a first fetch gets the later-poll rules;
  - a resumed first fetch moves it.
- `test_source_url_change_resets_first_fetch.py`:
  - `url`, `sitemap_url` and `link_pattern` each reset both fields;
  - name, refresh, category, headers and pause don't, and neither does the same URL;
  - the new address gets a first fetch;
  - a URL or sitemap change during an in-flight poll inserts nothing and keeps the reset;
  - an edit whose transaction started before the poll's, and which then waits on the poll's lock,
    lands its baseline after the poll's rows;
  - an edit that holds the lock first makes the waiting poll discard;
  - an edit made from a stale read (the PATCH route's earlier `session.get`) still re-points and
    resets;
  - a new address whose first poll is empty still gets a first fetch on the next poll with
    items (10 kept, 20 skipped), although the feed holds the old address's rows;
  - a job still holding the `DynamicRssPlugin` built before an edit that has already committed
    stores nothing and leaves the reset's `last_fetch_at` and `created_at`; a plugin built from
    the edited row then gets the first fetch.
- `test_brief_published_guard.py`:
  - a date-only story stamped midnight UTC stays;
  - an RSS story published before `since` and fetched after it stays;
  - a 30-day-old backfill story is excluded;
  - an undated story stays;
  - `window_hours=1` keeps fresh stories;
  - the boundary follows `ingest.backfill_hours`;
  - the convergence path has the same guard.
- `test_wikipedia_selected.py`:
  - selected only, at most 5, the same-article skip, and dated URLs, including the fallback
    fragment;
  - the class category;
  - the same article fetched on two dates inserts twice, and a second fetch on the same day gives
    only duplicates.
- `test_builtin_category_move.py`:
  - the move runs once and only from `news`, and a user's later `news` stays;
  - a user's own category stays;
  - with no row, the marker is written and a new row gets `on_this_day`;
  - `start_scheduler` runs the move (paused scheduler);
  - `half_life_hours('on_this_day') == 6`.
- `test_foryou_excludes_historical.py`: an On this day story never reaches For You, even when it
  is scored far higher, when asked for by category, or when the row is still in `news`.
- **Rewritten:** `test_ingest_skips_known_urls.py` uses relative dates and asserts `skipped`. It
  also gained a case: an old backlog item is skipped before the embed step.
- **Touched:** `test_ingest_misfire_grace.py` (PR1a's, no_db) stubs `_move_builtin_categories`,
  so the scheduler build there doesn't write to the DB.

## Verification

- Full backend pytest on local Postgres (`popping_test`): **13 failed, 799 passed**. The 13 are
  exactly the DNS-only baseline list in `/root/popping-env/baseline.txt` (diffed). 799 = PR1a's
  732 + 67 new or changed here. Re-run after the rebase onto main (H1 and PR2a merged); see the
  end of this file.
- The lock, in-flight and backfill files ran 5 times in a row: 34/34 each time.
- `ruff check --select F` on every changed file: clean.
- No frontend files changed, so frontend lint, build, unit tests and smoke were not needed. No
  backend test that pins frontend source was touched.
- **Mutation checks.** A script broke each guarded line, ran the named test(s), expected a
  failure, then restored the file and checked it byte for byte. The working-tree diff was
  identical before and after. All 37 mutations were caught:

  | Broken line | Failing test |
  |---|---|
  | first fetch without the last-2-days rule | `test_first_fetch_keeps_everything_from_the_last_2_days_beyond_10` |
  | first fetch without the newest-N rule | `test_first_fetch_keeps_the_newest_10_plus_the_last_2_days` |
  | no baseline window on later polls | `test_later_poll_drops_items_dated_before_the_baseline_window` |
  | no max age | `test_later_poll_drops_items_older_than_max_age` |
  | undated ignores the first known URL | `test_later_poll_drops_undated_backlog_below_the_first_known_url` |
  | no undated cap / oldest undated first | `test_later_poll_keeps_at_most_2_undated_above_the_first_known_url` |
  | no per-poll cap / oldest first | `test_per_poll_cap_keeps_the_newest_and_leaves_the_rest_for_later` |
  | resume-gap floor removed | `test_tunables_parse_and_fall_back_when_out_of_range` |
  | no resume gap | `test_feed_resumed_after_21_days_gets_at_most_10_plus_2_days` |
  | the whole EXISTS check dropped (`last_fetch_at` alone decides) | `test_empty_success_then_items_3_days_later_get_a_first_fetch` |
  | baseline never moves | `test_first_fetch_that_stores_a_story_moves_the_baseline` |
  | baseline moves on an empty first fetch | `test_empty_first_poll_leaves_the_baseline` |
  | baseline from `clock_timestamp()` instead of the rows' `now()` | `test_first_fetch_that_stores_a_story_moves_the_baseline` |
  | no in-flight address check / URL only | `test_url_change_…`, `test_sitemap_change_during_an_in_flight_poll_inserts_nothing` |
  | ingest commit session without the row lock | `test_change_that_waits_on_a_commit_lands_after_it` |
  | `update_source` without the reset | `test_repointing_resets_last_fetch_and_baseline[url]` |
  | `update_source` reset with `now()` | `test_change_that_waits_on_a_commit_lands_after_it` |
  | `update_source` without the lock, or without `populate_existing` | `test_edit_from_a_stale_read_still_repoints_and_resets` |
  | brief guard off the digest path / off the convergence path | `test_daily_digest_…`, `test_convergence_path_has_the_same_guard` |
  | brief guard against now instead of `fetched_at` | `test_daily_digest_keeps_news_and_drops_backfill` |
  | brief guard drops undated | `test_daily_digest_keeps_news_and_drops_backfill` |
  | brief ignores the knob | `test_guard_boundary_follows_backfill_hours` |
  | For You without the exclusion | `test_on_this_day_never_reaches_for_you` |
  | On this day: events again / no cap / no date in URL / class category `news` | `test_selected_only_…`, `test_same_article_on_two_dates_inserts_twice`, `test_class_category_is_on_this_day` |
  | move ignores the user's category / runs every start / not called at start | `test_leaves_a_category_the_user_picked`, `test_moves_a_news_row_once`, `test_start_scheduler_runs_the_move` |
  | no `on_this_day` half-life | `test_on_this_day_half_life_is_6_hours` |
  | no `IngestResult.skipped` | `test_manual_ingest_result_carries_skipped` |

  On the first run, two mutations survived, and both were fixed in the tests:
  - The "3 days later" test also crossed the 72 h resume gap, so it didn't isolate the EXISTS.
    The test now sets the last success to 1 h ago.
  - The `update_source` lock wasn't pinned. Without it, Postgres re-evaluates the UPDATE's
    `clock_timestamp()` after the row-lock wait, so the timing test alone can't tell. What the
    explicit lock with `populate_existing` adds is that change detection reads the committed row
    rather than the PATCH route's earlier read. The stale-read test now pins that.
- **Security checklist:** no new routes. `/api/foryou` gains a filter only. No new network I/O:
  the Wikipedia fetch is unchanged (same client and SSRF hook). Tunables are server-side
  `app_settings` rows, parsed and range-checked. No client-written JSON is read.

## Deviations from the spec

- **The in-flight guard compares `url`, `sitemap_url` and `link_pattern`, not just `url`.** The
  spec's `update_source` treats all three as re-pointing the feed. Without the wider check, a
  sitemap change during a poll would commit the old sitemap's batch and its bookkeeping over the
  reset. PATCH `type` (PR11a) can join the tuple when it lands.
- **Row locks.** The spec asks for the guard but doesn't say how to close the race between the
  check and the commit. The commit session and `update_source` both take `FOR NO KEY UPDATE` on
  the feed's row, and the reset uses `clock_timestamp()`, not `now()`. The PATCH route's
  transaction starts before it calls `update_source`, so `now()` could predate a concurrent
  poll's rows and silently cancel the reset. As a side effect, a PATCH to a feed waits for that
  feed's in-flight commit session (embedding included) to finish.
- **The baseline moves on rows inserted, not items kept.** The spec says "keeps at least one
  item". If every kept item lost an insert race (`ON CONFLICT DO NOTHING`), nothing would satisfy
  the EXISTS, so the baseline moves only when a row actually lands.
- **On this day** also skips a second selected item that links the same article, so the day keeps
  5 distinct stories. The date is appended to an existing fragment rather than adding a second
  `#`.
- **`test_ingest_backfill_cap.py`** holds the end-to-end matrix and `test_ingest_rules.py` the
  pure cases. The spec listed the same bullets under both names.
- **Which undated items win the cap:** the topmost (newest) ones, matching "newest first"
  elsewhere. Undated items beyond the undated cap in one poll are lost, because the next poll sees
  them below the newly stored ones. That is D12's cap as written. Keeping the ones nearest the
  known boundary instead would let a burst drain over later polls, at the cost of showing the
  newest ones late. The undated items within the cap are never deferred by the per-poll cap:
  they take their slots before the dated ones.

## Risks and data-compatibility notes

- **Revert is safe.** There is no migration. The old code ignores `skipped` and the
  `builtin_category_moved:` marker. A reverted backend puts nothing back in `news`: the moved row
  keeps `on_this_day` until edited.
- After a deploy, every feed that hasn't had a successful poll in 72 h gets a first fetch on its
  next success. That includes a server that was down for 3 days or more (D6).
- Website feeds still drain undated sitemap pages at up to 10 a poll (`generic_scrape.py:42`),
  and the plugin marks them extracted. So beyond the 2-undated cap they are lost until PR10b
  applies the cap inside the plugin. This is the spec's known risk.
- The On this day stories already stored move to the new tab with the row, and stay there until
  they leave the 2-day window.
- A per-poll cap set below the undated cap (`ingest.max_per_poll` under
  `ingest.max_undated_per_poll`, both non-default) would still cut undated survivors, and those
  are lost the same way as undated items beyond the undated cap.

## Open items for later units

- **PR4b:** For You's paused-feed filter goes next to the new historical exclusion in
  `routes/foryou.py`.
- **PR9a / PR6a:** both touch `scheduler._ingest` after this. Its commit session now holds the
  feed's row lock from its first query to its commit, so new bookkeeping belongs inside that
  session.
- **PR10b:** reuse `ingest_rules.IngestTunables.max_undated_per_poll` (`ingest.max_undated_per_poll`)
  inside `generic_scrape`, and leave pages past the cap unmarked in `_extracted_urls`.
- **PR11a:** when PATCH `type` lands, add `type` to `base.row_fetch_target`, to the class-plugin
  snapshot in `_ingest`, and to the reset in `update_source`.
- **Frontend (PR4d / PR7b):** the new category renders as "On This Day" through `categoryLabel`.
  The D15 vocabulary sweep may want "On this day".

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md`:

- [ ] #<pr> · Add a feed with a long back catalogue (a podcast or a blog with 50+ posts) and wait for its first check. Pass: its tab gains at most 10 stories plus any from the last 2 days.
- [ ] #<pr> · A day or more after the deploy, open the On This Day tab. Pass: the stories that arrived in the last 24 hours number at most 10 (at most 5 per UTC day, which turns at 5 pm Pacific), and none of them are in For You. Stories fetched before the deploy don't count.
- [ ] #<pr> · The day after adding that feed, open Brief. Pass: no highlight is one of the new feed's older stories.

## Rebase

- Cut from d74b159 (PR1a). Before committing, main had moved to 1d45413: PR2a (#28) and H1 (#29)
  had merged. `git rebase main` ran with no conflicts. PR2a's `schemas.py` changes sit apart from
  `IngestResult`.
- After the rebase, full backend pytest on `popping_test` gave **13 failed, 881 passed**. The 13
  failures diff clean against the DNS-only baseline list. 881 = 799 + PR2a's 82.

## Review fixes

`/code-review xhigh` confirmed six findings. All six were fixed; none was skipped.

1. **A resumed or overlapping first fetch pulled back old stories (major).** The first fetch
   ranked "newest 10" over unknown items only. On a resume (a pause, an auto-disable, or an
   outage of 72 h or more, which turns every feed's next poll into a first fetch at once), the
   unknown set also holds the back catalogue the earlier first fetch skipped, so those stories
   filled the slots the stored ones held and landed as new. Undated feeds did the same, and so
   did a new feed whose newest stories another feed had stored. `select_items_for_ingest` now
   ranks the whole deduped listing, known items included: they use up rank slots but aren't
   stored. A brand-new feed is unchanged. Tests: the pure resume cases (dated and undated, 0 or 4
   new posts) and the overlap case in `test_ingest_rules.py`, and the 28-day weekly-blog resume
   through `_ingest` in `test_ingest_backfill_cap.py`.
2. **The in-flight guard read the address from the database, not from the plugin that fetches
   (minor).** A job keeps its plugin instance until `update_source` replaces it after the edit's
   commit, and `trigger_now` builds from an earlier read. An edit that committed between the
   plugin's build and the snapshot read made the snapshot match the new address while the old
   one was fetched, and that batch then ran as the new address's first fetch. Each dynamic plugin
   now records `fetch_target = base.row_fetch_target(row)`, the full `(url, sitemap_url,
   link_pattern)` tuple of the row it was built from, whatever its type, so an RSS row with a
   `sitemap_url` still matches itself. `_ingest` uses it, and keeps the row snapshot for
   class-driven built-ins. Test: `test_job_still_holding_the_old_plugin_after_the_edit_inserts_nothing`.
3. **The On this day device check would fail on deploy day (minor).** Deploy day's stories from
   the old code (up to 50 `events` a day) move into the new tab with the row, and the startup tick
   runs on the same UTC date. The check now counts only stories from the last 24 hours, a day or
   more after the deploy: at most 10, because the 12 h polls can straddle a UTC midnight and each
   UTC day brings up to 5.
4. **The EXISTS's `fetched_at >= created_at` clause had no test (minor).** Without it, a feed
   re-pointed to an address whose first poll is empty keeps the old address's rows, so the next
   poll with items ran the later-poll rules. With the reset's new baseline, those rules drop
   everything older than 2 days as backfill: the feed under-ingests (2 of the new address's 30
   one-a-day stories instead of 10). It does not flood. Test:
   `test_empty_first_poll_of_a_new_address_keeps_the_first_fetch`.
5. **The per-poll cap's newest-first order wasn't pinned (minor).** Every per-poll test listed
   items newest first, so a cap in feed order passed. New pure tests list 70 items oldest first
   and assert the 50 newest are kept, for a later poll and a first fetch.
6. **Undated stories cut by the per-poll cap were lost (minor).** They ranked after every dated
   item, so a burst of 50 or more dated stories cut them, and the next poll dropped them as below
   a known URL. Undated survivors (at most `max_undated`) now take their per-poll slots first,
   and dated ones fill the rest, newest first. A cut dated story keeps its date and lands on a
   later poll. Test: `test_undated_items_get_per_poll_slots_before_dated_ones` (60 dated plus 2
   undated, all 62 landing over two polls).

### Verification

- Full backend pytest on `popping_test`: **13 failed, 895 passed**. The 13 diff clean against the
  DNS-only baseline list. 895 = 881 + 14 new test cases.
- The four ingest files (66 tests) ran 5 times in a row, all passing each time.
- `ruff check --select F` on every changed file: clean.
- **Mutation checks** (same script method as above; the working-tree diff was identical before
  and after):

  | Broken line | Failing test |
  |---|---|
  | first fetch ranks new items only | the 4 pure resume cases, the overlap case, the 4 `test_resumed_feed_stores_only_its_new_posts_…` cases |
  | guard compares the row snapshot, not the plugin's `fetch_target` | `test_job_still_holding_the_old_plugin_after_the_edit_inserts_nothing` |
  | EXISTS without the `fetched_at >= created_at` predicate | `test_empty_first_poll_of_a_new_address_keeps_the_first_fetch` |
  | the whole EXISTS check dropped | `test_empty_success_then_items_3_days_later_get_a_first_fetch`, `test_empty_first_poll_of_a_new_address_keeps_the_first_fetch` |
  | per-poll cap in feed order | `test_per_poll_cap_ranks_by_date_not_feed_order`, `test_per_poll_cap_ranks_by_date_on_a_first_fetch_too` |
  | undated ranked after dated in the cap | `test_undated_items_get_per_poll_slots_before_dated_ones` |
