# PR1a: Scheduler defaults, extraction off the event loop, Reddit Test, History

Backend track. Backend changes and pytest only; no frontend, flows or smoke states.

## What changed

- **Scheduler job defaults** (`backend/app/scheduler.py`). `start_scheduler` builds
  `AsyncIOScheduler(timezone="UTC", job_defaults={"misfire_grace_time": 120, "coalesce": True})`
  (the dict is `_JOB_DEFAULTS`). Every job the scheduler holds gets it: class ingest, dynamic
  per-row ingest (including jobs `add_source`/`update_source` add later), `pref:recompute`,
  rescore, framing and the rest. Before, no `add_job` set a grace, so APScheduler 3.11's 1 s
  default skipped any run that started more than a second late. Jobs that pass their own
  `coalesce` keep it (all of them pass `True` today).
- **`brief:daily`** gets `misfire_grace_time=3600` (`_BRIEF_DAILY_MISFIRE_GRACE_SECONDS`).
- **Extraction off the event loop** (`backend/app/sources/generic_scrape.py`). `_extract_one`
  runs `trafilatura.bare_extraction` through `await asyncio.to_thread(...)`, the same way
  `feed_autodiscovery` runs `determine_feed`. This is the cause of the late runs; the grace only
  covers the symptom. It uses asyncio's default thread pool, which the app already uses; nothing
  new is started.
- **History** (`backend/app/routes/interactions.py`, `GET /api/interactions/recent`). The
  `type_list.append("dwell")` is gone. That append turned the no-filter case into
  `IN (dwell) AND NOT IN (dwell)`, so History was always empty. Now:
  - no `types`: every type except `dwell` and `view`;
  - explicit `types`: passed through, minus `dwell` (`?types=dwell` returns nothing).
  The same exclusion applies to the count query, so `total` matches.
- **Reddit Test** (`backend/app/routes/sources.py`, `POST /api/sources/test`, `type="reddit"`).
  The route imported `fetch_subreddit` from `app.sources.reddit`, which has none, so every
  subreddit Test failed with an ImportError reported as `unknown`. It now does
  `sub = normalize_subreddit(url)` and `reddit_client.fetch_subreddit(sub, limit=25)`:
  - `reddit_client.is_disabled()` → `unsupported_type` (no fetch);
  - `reddit_client.RedditFetchThrottled` → `network_error` with the upstream `status_code`,
    caught before the generic `except Exception`;
  - an empty listing → `not_found` (private, banned and 403 subs land here, because
    `fetch_subreddit` turns `httpx.HTTPError` and `ValueError` into `[]`);
  - a listing → `ok`, with the count and up to 3 sample titles, as for other types.

## Tests

- `backend/tests/test_ingest_misfire_grace.py` (no_db). Builds the real scheduler through
  `start_scheduler` with every optional job enabled (embedder stubbed as loaded, brief hour set)
  and a dynamic row registered by a stubbed startup walk. APScheduler only fills job defaults when
  a job reaches its job store at `start()`, so the test swaps in an `AsyncIOScheduler` subclass
  whose `start()` always starts paused: jobs get their defaults, nothing runs. Teardown calls
  `shutdown(wait=False)` and resets the module globals.
  - every built job has `coalesce=True` and, except `brief:daily`, `misfire_grace_time == 120`;
    the test first asserts the ingest, dynamic, `pref:recompute`, rescore, framing and
    `brief:daily` jobs are all present;
  - `brief:daily` has 3600;
  - a dynamic job added after start gets the defaults;
  - extraction does not block the loop: a 0.3 s `time.sleep` stands in for `bare_extraction`
    while a 10 ms ticker runs; the ticker must get at least 5 ticks in. On the loop it gets 0.
    (The spec allowed a live blocked-loop test with a private scheduler; this direct test of the
    cause is simpler and deterministic, so I used it instead.)
- `backend/tests/test_interactions_history_types.py` (db). Seeds `never`, `bookmark`, `dwell`
  and `view` for one story: no types → `{never, bookmark}` (total 2); no types with
  `group_by=entry` → one row; `?types=view` → views; `?types=view,never` → both; `?types=dwell`
  → none (total 0).
- `backend/tests/test_source_test_reddit.py` (no_db). Monkeypatches `reddit_client.fetch_subreddit`
  and `is_disabled` (as `test_reddit_throttle_cooldown.py` does) and the route's `check_url_safe`
  (no DNS needed). Covers disabled (and that no fetch happens), throttled (`network_error`, 429),
  empty (`not_found`) and a listing (`ok`, 5 items, 3 titles, called with `("python", "hot", 25)`
  for `https://www.reddit.com/r/Python/hot`).
- The existing `generic_scrape` tests stay green: they monkeypatch `trafilatura.bare_extraction`,
  which `to_thread` still looks up at call time.

## Verification

- Full backend pytest on local Postgres (`popping_test`): **13 failed, 732 passed**. The 13
  failures are exactly the DNS-only baseline list in `/root/popping-env/baseline.txt` (diffed);
  732 = the baseline's 719 + the 13 new tests.
- No frontend files changed, so frontend lint, build, unit tests and smoke were not needed.
- Mutation checks (each guarded line broken, the named test run, then the file restored from a
  copy and compared byte for byte):

  | Broken line | Failing test(s) |
  |---|---|
  | `job_defaults=_JOB_DEFAULTS` dropped from `AsyncIOScheduler(...)` | `test_every_built_job_gets_the_grace_and_coalesce`, `test_a_dynamic_job_added_after_start_gets_the_defaults` |
  | `misfire_grace_time=_BRIEF_DAILY_MISFIRE_GRACE_SECONDS` dropped | `test_brief_daily_gets_an_hour_of_grace` |
  | `asyncio.to_thread(...)` back to a direct `bare_extraction` call | `test_page_extraction_does_not_block_the_event_loop` (ticks=0) |
  | old `type_list.append("dwell")` restored | both no-types History tests |
  | exclusion always `{dwell, view}` | `test_explicit_view_returns_views`, `test_explicit_types_pass_through` |
  | exclusion always `{dwell}` | both no-types History tests |
  | no exclusion when types are given | `test_explicit_dwell_returns_nothing` |
  | `except RedditFetchThrottled` changed to another exception | `test_throttled_is_a_network_error` |
  | `is_disabled()` check skipped | `test_disabled_client_is_unsupported` |
  | empty-listing check skipped, URL passed instead of the name | `test_empty_listing_is_not_found`, `test_listing_is_ok_and_passes_the_subreddit_name` |

- Security checklist: no new routes. The Test route still runs `check_url_safe` before the fetch,
  and the Reddit fetch goes through `reddit_client` only, as the Rules require. History reads
  only the caller's own rows, as before.

## Deviations from the spec

- The spec says `brief:daily`'s grace lets "a busy or restarting server" still send the day's
  brief. The job store is in memory, so a restart re-adds the cron job with its next fire time
  computed from the restart; a restart after the scheduled hour still skips to the next day. The
  grace covers lateness within one process (busy loop, slow wakeup). The code comment says so.
  Catching up after a restart would need a "last brief sent" check at startup, which is not in
  this unit.
- The extraction guard test is a direct ticker test instead of a live scheduler test (see Tests).
- The Reddit Test's `custom_headers` are validated as before but not used: `reddit_client`
  sends its own headers, same as the scheduled ingest. The old code passed them to a function
  that didn't exist.

## Open items for later units

- **PR1c:** Settings → History's Read group (`Settings.tsx:1289-1295`) is now always empty,
  because views are excluded by default. PR1c drops that group. The default list also includes
  types History doesn't group (`thumb_up`, `thumb_down`, `click` and others), so a page of 50
  can hold fewer visible rows than 50. PR1c or PR12a may want to pass `types=never,bookmark`.
- **PR10b:** `score_page` should run inside the same `asyncio.to_thread` call as
  `bare_extraction`.
- Not in any unit yet: `app/article_extract.extract_text` (the on-demand summary) still runs
  `trafilatura.extract` on the event loop. It runs once per tap, not per poll, but it is the
  same kind of stall.
- Device checks: this PR adds two lines under Pending in `docs/DEVICE_CHECKS.md` (History lists
  hides and saves; the subreddit Test shows a count and titles). Their `#<pr>` and `sha-<short>`
  are placeholders until the PR number and the squash-merge sha are known; fill them in then,
  and run the checks only after that merge commit's main-branch build is green.

## Review fixes

- The branch was cut from 4be8766, one commit before H0 (#26, c02ddaa) merged. It is now rebased
  onto origin/main (c02ddaa) with a plain `git rebase origin/main`; no conflicts. The README.md:85
  change the first commit carried is H0's and is no longer in this diff.
- After the rebase, full backend pytest on `popping_test`: 13 failed, 732 passed; the failures
  diff clean against the DNS-only baseline list.
- The device-check lines moved from these notes into `docs/DEVICE_CHECKS.md` under Pending,
  replacing "_No units merged yet._", as D21 and the Rules require.
- Commit trailers were left as the session's attribution settings write them; the review asked
  for a trailer without the model name. No CLAUDE.md or other owner-written instruction in this
  session overrides those settings, so the owner can drop or reword that line in the
  squash-merge body if wanted. The code, comments and these notes name no model.
