# PR6a: queue_check and the check-now route

Backend track. Backend changes and pytest only; no frontend, flows or smoke states. Inert until
PR6b calls the route.

## What changed

- **`scheduler.queue_check(ids, force=False)`** (new). Pull to refresh asks for up to 20 feeds to
  be checked soon. Nothing is fetched in the request: each feed's ingest job (`ingest:<name>` for a
  built-in, `ingest:dynamic:<id>` otherwise, through the new `_source_job_id`, which
  `delete_source` now uses too) is moved up, so the fetch runs as the job under the usual
  `max_instances=1`, `coalesce`, backoff, auto-disable and ingest rules. Ids are deduplicated in
  order and capped at 20. Each feed lands in exactly one list, checked in this order:
  1. `paused`: no row, an inactive row, no scheduler, no job, or a paused job (`next_run_time`
     None).
  2. `queued_ids`: a run that is still fetching (it hasn't stamped its result yet). It is left
     alone, because APScheduler drops a run that comes due while one executes.
  3. `cooling_down`: a run that has stamped its result but is still in its thumbnail and og:image
     passes. It was checked just now, and it can't be moved up either. Also a job with a run that
     finished while `queue_check` was reading the rows (`_ingest_finished` moved since the
     snapshot taken before the first await): the row can predate that run's result, so it was
     checked just now and the job stays where the run left it.
  4. `cooling_down`: queued by check-now within the cooldown (`_last_queued`), or with a
     successful fetch (`last_fetch_at`) within the cooldown. The cooldown is
     `refresh.cooldown_seconds` (default 600, `[60, 86400]`, read through
     `runtime_settings.get_many` and the shared `ingest_rules.parse_int_knob`), or 60 s with
     `force`.
  5. `backing_off`: without `force`, `error_count > 0` and the job's `next_run_time` still in the
     future (the backed-off interval). `force` skips this gate.
  6. Reddit in direct mode (`reddit_client.is_direct_mode()`: no proxy URL, direct mode on): a
     subreddit fetched within its own refresh interval (`force` included), and every subreddit
     after the first one this call moves, go to `cooling_down`.
  7. Otherwise queued: `next_run_time = min(next_run_time, now + i × 3 s)`, with `i` counting the
     feeds moved so far. A job due sooner is never postponed.
- **Response:** `{queued, cooling_down, paused, backing_off, queued_ids, queued_at,
  expected_done_s}`. `queued` is `len(queued_ids)`. The other three are id lists, so PR9b can tell
  whether its one id came back as `cooling_down` or `paused`. `queued_at` is the server's UTC
  clock, taken after the call's awaits and before any job moves. `expected_done_s` is 0 when
  nothing was queued. Otherwise it is the Reddit wait (75 s per running subreddit, plus the time
  until the token bucket has served every subreddit check-now has queued, `_reddit_calls_done_at`)
  plus 3 s per other queued feed plus 20. For one call that is the spec's `reddit_count × 75 +
  count × 3 + 20`.
- **Run tracking in `_ingest`.** `_ingest` is now a thin wrapper around `_ingest_once` (the old
  body, unchanged apart from one call). It counts each run, scheduled or manual, in
  `_ingest_running` until it returns, and in `_ingest_unrecorded` until it stamps its result. A
  successful run marks itself recorded just before it takes the `last_fetch_at` timestamp.
  Every other path is marked by the wrapper as it returns: a failure right after recording its
  error, and a deleted, paused or re-pointed feed without a write. Both are `Counter`s, so a manual
  `/api/ingest` run overlapping a scheduled one is counted correctly. A third `Counter`,
  `_ingest_finished`, counts every run as it returns and is never counted down (one entry per job
  id that has run).
- **`POST /api/sources/check-now`** on the sources router, behind `_write_deps` and main.py's
  `_api_deps`. The body is `CheckNowRequest`: `source_ids` is a `conlist` of strict ints in
  `[1, 2^31 − 1]` with length 1 to 20, `force` is a `StrictBool` defaulting to false, and unknown
  fields are refused. The response model is `CheckNowResult`. The cap is `CHECK_NOW_MAX_IDS` in
  `schemas.py`, and the scheduler takes it from there.
- **`reddit_client`:** `is_direct_mode()` and `direct_call_interval_seconds()` (public, so the
  scheduler doesn't read `_DIRECT_RPS`).
- **`ingest_rules._parse_int` → `parse_int_knob`** (public, with a docstring), shared with the
  cooldown knob.
- **Docs:** `ARCHITECTURE.md` lists `_last_queued`, `_reddit_calls_done_at`, `_ingest_running`,
  `_ingest_unrecorded` and `_ingest_finished` as process-local state, with what breaks under several workers.
  `README.md` gains the route in the API table and `refresh.cooldown_seconds` in the runtime
  knobs.

## Tests

`tests/test_check_now_queue.py`, 49 tests (46 in the first commit, 3 added by the review fixes). They use a real `AsyncIOScheduler` started paused and
installed as the app's scheduler. Where a job has to run, the test awaits `_ingest` itself, the
way the scheduler would.

- Queueing: 3 s stagger and `expected_done_s`; a due job and a job due in 1 s are not postponed;
  repeated ids are taken once and the list is capped at 20.
- Backoff: a backing-off feed is not modified, and six pulls in a row (cooldown cleared between
  them, as if 10 minutes passed), each followed by running whatever is due, leave it active with
  `error_count` 1. `force` queues it, and the run's failure is recorded and backed off as usual. A
  failing feed whose job is already due is queued.
- Running jobs: a run blocked in its fetch counts as queued and isn't modified, and its success
  lands after `queued_at`. A run that finishes between the row read and the decision (the test
  runs the whole `_ingest` inside `AsyncSession.scalars`, after the real SELECT): a failing one
  comes back `cooling_down` with its job still at the backed-off time, nothing due and
  `error_count` 1; a successful one comes back `cooling_down` with its job unmoved. A run blocked in its og:image pass (with the force cooldown patched to
  0, so only the run tracking decides) is `cooling_down` and isn't modified. A check made inside
  the success commit is either `cooling_down` or queued with `queued_at <= last_fetch_at`. A
  failed run leaves both counters empty.
- Cooldown: queued within the cooldown, fetched within it, force's 60 s, the knob, bad knob
  values (`10`, `999999`, `soon`, unset) give 600, and stale `_last_queued` entries are pruned.
- Paused: inactive, unknown id, no job, a paused job (left paused), no scheduler, and a pause
  committed just before the call.
- A built-in row uses its `ingest:<name>` class job.
- Reddit direct mode: one subreddit per call, with the rest `cooling_down` and unmoved, and
  `expected_done_s` 75 + 3 + 20. The next pull takes the next subreddit and reports about 150 + 20
  (it waits behind the first in the token bucket). A subreddit fetched 12 minutes ago (past the
  cooldown, inside its 15-minute interval) is `cooling_down`, and so is one fetched 2 minutes ago
  under `force` (past force's 60 s, inside its interval), with its job unmoved. Proxy mode queues every subreddit. A
  running subreddit counts as queued and doesn't use up the call.
- Route: every kind in one response, with the exact key set and an aware `queued_at` inside the
  call. `force` defaults to false, and `force: true` is passed through. 422 for 21 ids, an empty
  list, a missing body, `"5"`, `1.0`, `true`, 0, 2^31, `force: "yes"`, `force: 1`, an extra field,
  and a bare array. 20 ids return 200. A GET has no side effect: it falls through to
  `GET /sources/{id}` and gets a 422. The route's and the scheduler's caps are the same 20.

## Verification

- Full backend pytest on `popping_test`: **13 failed, 941 passed**. The 13 diff clean against the
  DNS-only list in `/root/popping-env/baseline.txt`. 941 = main's 895 + 46 new. Re-run after the
  rebase; see the end of this file.
- `tests/test_check_now_queue.py` ran 5 times in a row: 46/46 each time.
- `ruff check --select F` on every changed file: clean.
- No frontend files changed, so frontend lint, build, unit tests and smoke were not needed. No
  backend test that pins frontend source was touched. The source pins on `_ingest` in
  `test_ingest_bugfixes.py` and `test_slice27_deferred_perf.py` still match.
- **Mutation checks.** A script broke each guarded line, ran the named test(s), expected a
  failure, and restored the file. `git diff` was byte-identical before and after (the new test file
  was intent-to-add, so it was covered too). All 39 mutations were caught, and so were the 4
  added with the review fixes (each restored from a copy of the file and checked with `cmp`):

  | Broken line | Failing test |
  |---|---|
  | backoff gate removed | `test_a_backing_off_feed_is_left_alone_and_six_pulls_never_disable_it` |
  | gate ignores `force` | `test_force_still_queues_a_backing_off_feed` |
  | gate ignores `next_run_time` | `test_a_failing_feed_whose_job_is_due_is_queued` |
  | a due job postponed (always modify) | `test_a_job_already_due_is_never_postponed` |
  | no stagger | `test_queues_each_feed_3_seconds_after_the_last` |
  | running (unrecorded) job not counted as queued | `test_a_running_job_counts_as_queued_and_is_not_modified` |
  | recorded-but-running job not special | `test_a_run_that_recorded_its_result_counts_as_checked` |
  | success never marks recorded | `test_a_run_that_recorded_its_result_counts_as_checked` |
  | marked after the commit instead of before the stamp | `test_a_check_during_the_success_commit_still_sees_it_land` |
  | running count never cleared | `test_a_failed_run_is_cleared_from_the_running_counts` |
  | unrecorded count never cleared on failure | `test_a_failed_run_is_cleared_from_the_running_counts` |
  | no one-subreddit-per-call limit | `test_direct_mode_queues_one_reddit_feed_per_call` |
  | no subreddit interval rule | `test_direct_mode_a_subreddit_fetched_within_its_interval_is_cooling_down` |
  | Reddit rules in proxy mode | `test_proxy_mode_queues_every_reddit_feed` |
  | a running subreddit uses up the call | `test_a_running_reddit_job_does_not_use_up_the_call` |
  | running subreddit / queued subreddit left out of `expected_done_s` | `test_a_running_reddit_job_does_not_use_up_the_call`, `test_direct_mode_queues_one_reddit_feed_per_call` |
  | token-bucket wait not accumulated across calls | `test_direct_mode_queues_one_reddit_feed_per_call` |
  | no settle time; nonzero `expected_done_s` with nothing queued | `test_queues_each_feed_3_seconds_after_the_last`, `test_a_backing_off_feed_…` |
  | no queued cooldown; no fetched cooldown; queue doesn't record the cooldown | `test_a_feed_queued_within_the_cooldown_is_cooling_down`, `test_a_feed_fetched_within_the_cooldown_is_cooling_down` |
  | `force` keeps the long cooldown | `test_force_shortens_the_cooldown_to_a_minute` |
  | knob ignored; knob not range-checked | `test_the_cooldown_follows_refresh_cooldown_seconds`, `test_a_bad_cooldown_value_uses_the_default` |
  | no prune | `test_old_cooldown_entries_are_dropped` |
  | inactive rows queued; a paused job treated as live | `test_inactive_unknown_and_jobless_feeds_are_paused`, `test_queue_check_reads_rows_as_committed` |
  | built-in mapped to the dynamic job id | `test_a_built_in_feed_uses_its_class_job` |
  | no cap; no dedupe | `test_repeated_and_extra_ids_are_taken_once_and_capped` |
  | schema cap 21; empty list allowed; non-strict ids; unbounded ids; non-strict `force`; extra fields allowed | `test_route_refuses_bad_bodies`, `test_the_route_and_the_scheduler_share_the_cap` |
  | route ignores `force` | `test_route_passes_force` |
  | review fixes: finished-run check removed | `test_a_run_that_fails_during_the_row_read_keeps_its_backoff`, `test_a_run_that_succeeds_during_the_row_read_is_not_fetched_again` |
  | review fixes: `_ingest_finished` never incremented | the same two |
  | review fixes: snapshot taken after the row read instead of before it | the same two |
  | review fixes: `force` lifts the subreddit interval rule again | `test_direct_mode_force_keeps_the_subreddit_interval_rule` |

- **Security review.** `/security-review` couldn't run here (no `origin` remote: `git diff
  origin/HEAD...` fails). The plan's fallback, and this unit's review step under D32, is one
  separate security-lens review agent with the Rules security checklist on `git diff main...HEAD`.
  That agent ran in the review pass, alongside a scheduler lens and a spec lens. It found one
  real issue: a row read before `queue_check`'s own await could let a feed that had just failed
  skip its backoff. A skeptic confirmed it, and it is fixed in "Review fixes" below. The list
  below is the implementer's own pass over the same checklist:
  - The route is on the sources router, inside main.py's `_api_deps` loop and behind
    `_write_deps`. `test_oidc_route_gating.py` probes every route, this one included, and passes.
  - It changes state only through a Pydantic JSON body: strict, bounded ints, at most 20, a strict
    bool, no extra fields. No GET has a side effect.
  - There is no network I/O in the request and no new fetch path. Feeds are fetched by their
    existing jobs and plugins, with the SSRF hooks unchanged.
  - No client-written preference JSON is read.
  - Upstream load is bounded per feed by the cooldown (at least 60 s with `force`). Reddit gets at
    most one new call per request, and the token bucket still serializes the calls.
    `/api/ingest/{name}` already allows an immediate, uncooled fetch with the same auth, so
    `force` adds no new exposure.
  - `_last_queued` only stores ids of feeds that have jobs, and it is pruned to the cooldown on
    every call. Logs carry counts only.
  - Nothing found.

## Deviations from the spec

- **Successful fetches count toward the cooldown, not only check-now's own queues.** D10 reads
  "10-minute cooldown per feed". A feed that polled two minutes ago, by schedule or by an earlier
  pull, has just been checked, and PR6b's "Checked 4 min ago" reads the same `last_fetch_at`. It
  also matters for correctness: a run past its commit can't be moved up, and without this rule
  such a feed would be reported queued and never run. `_last_queued` is still kept, as the spec
  says, and it covers queues whose run hasn't finished yet, as well as failing feeds.
- **A running job that has already stamped its result is `cooling_down`, not queued.** The spec
  counts every running job as queued. A run in its thumbnail or og:image pass (which can take tens
  of seconds) already wrote a `last_fetch_at` from before the pull, so a caller waiting for
  `last_fetch_at >= queued_at` would wait until it timed out. A run still fetching is queued, as
  the spec says.
- **Subreddits held back by the one-per-call limit are reported as `cooling_down`.** The response
  has no "deferred" list. It is Reddit's call budget that is cooling down, and the next pull takes
  the next subreddit. `force` lifts neither the one-per-call limit nor the "fetched within its own
  interval" rule (see Review fixes).
- **`expected_done_s` includes the token-bucket wait across calls.** Two pulls 5 s apart that
  each queue a subreddit would otherwise both say 95 s, while the second subreddit's call can't
  start until about 150 s. For a single call the number is the spec's.
- **`queued`, `cooling_down`, `paused` and `backing_off`:** `queued` is a count (`queued_ids`
  holds the ids), and the other three are id lists. The spec names the fields without types, and
  PR9b needs to look up its one id.
- **Run tracking** uses two counters (running, unrecorded) instead of one in-flight set, for the
  two running cases above and for overlapping manual and scheduled runs. `_ingest` became a
  wrapper around `_ingest_once` so the tracking always unwinds. The body is unchanged apart from
  the one `mark_recorded()` call.
- **Unknown ids are `paused`**, with "no job", rather than a 404. The tab's list can hold a feed
  that was deleted on another device, and one stale id shouldn't fail the whole pull.

## Known limits

- **A repeated failure can't be detected by the caller.** The completion rule is `last_fetch_at >=
  queued_at` or a changed `last_error`. A failure with the same error string as before changes
  neither, so the caller waits until its last refresh. PR9a's `last_attempt_at` fixes this.
- **After a restart, a failing dynamic feed reads as `backing_off` for up to one normal
  interval.** The startup walk registers jobs at the base interval and doesn't restore the
  backoff, so its `next_run_time` is in the future without being a real backoff. The gate errs on
  the safe side, because it never runs a failing feed early. PR9a's `last_attempt_at` makes the
  window exact.
- **A run already fetching when the pull arrives counts as queued,** so its result can miss
  stories published in the seconds or minutes since it started, which matters most for a slow
  website or podcast feed. This is the spec's choice. The next poll picks them up.

## Risks and data-compatibility notes

- **Revert is safe.** The route is additive and nothing calls it until PR6b. There is no migration
  and no stored data. `_ingest`'s behaviour is unchanged apart from the counters.
- The counters and the cooldown are process-local (ARCHITECTURE.md). With more than one worker, a
  pull that lands on another worker skips `_last_queued`, but the `last_fetch_at` half still
  holds.

## Open items for later units

- **PR6b:** `api.checkNow(ids, force?)` posts `{source_ids, force}`. Wait only on `queued_ids`. A
  feed is done when `last_fetch_at >= queued_at` or `last_error` changed. `queued_at` is ISO 8601
  with an offset. `capture.cjs`'s mock should return all seven keys, with the three lists as id
  arrays and `queued` as a number. More than 20 ids is a 422, so slice before posting.
- **PR9a:** the backoff gate can switch to `last_attempt_at + backoff(error_count)` instead of
  the job's `next_run_time`, which also fixes the after-restart case above. The success path's
  `mark_recorded()` sits just before the `last_fetch_at` stamp. Put the `last_attempt_at` write
  next to it, so a check that saw the run as unrecorded always sees `last_attempt_at >=
  queued_at`. The error path is marked by the wrapper after its commit, which already holds.
- **PR9b:** `checkNow([id], true)` returns that id in exactly one list. `cooling_down` with
  `force` means a successful fetch or a check-now queue within the last 60 s, a run that
  stamped its result moments ago, or a run that finished while the call read the rows. In Reddit
  direct mode it also means a subreddit fetched within its own refresh interval.
- **For the plan owner (PR9b, Reddit direct mode):** a forced Check now on a subreddit fetched
  within its own interval returns `cooling_down`, as the spec's Reddit bullet says, so on a
  healthy subreddit it mostly reads `cooling_down` until its next scheduled poll. The first commit
  lifted that rule under `force`. Review found that this undoes `dynamic_reddit`'s 429 skip: a run
  after a 429 counts as a fetch, and a forced check would spend the one-cycle skip early and bring
  the next reddit.com call forward. Proxy mode is unaffected. If PR9b needs Check now to fetch a
  subreddit in direct mode, the plan needs a decision on how it treats a throttled subreddit.

## Device checks

None. The route has no caller until PR6b, and Gate 2 covers pull to refresh on the device. Nothing
was added to `docs/DEVICE_CHECKS.md`.

## Rebase

- Cut from 4f019bc (PR4a). Before committing, main had moved to ed956b4: PR4b (#31) had merged.
  `git rebase main` ran with no conflicts. PR4b's `schemas.py` and `README.md` changes sit apart
  from this unit's.
- After the rebase, full backend pytest on `popping_test` gave **13 failed, 994 passed**. The 13
  diff clean against the DNS-only baseline list. 994 = main's 948 + the 46 new tests here.
  `ruff check --select F` on the changed files is still clean.

## Review fixes

The second commit, "PR6a: review fixes", handles the four confirmed review findings.

- **A run that finished while `queue_check` read the rows could get past the backoff gate**
  (two findings, the same race). `queue_check` reads the rows across awaits and only then checks
  the run counters. A failing run could finish in that window: its job was already backed off,
  both counters were empty again, and the stale row still said `error_count` 0, so the gate let
  the pull move the job to now. A run that succeeded in the window was fetched again straight
  away. Fix: `_ingest`'s `finally` now also counts the run in `_ingest_finished`, and
  `queue_check` snapshots that counter before its first await. A job whose count moved is
  `cooling_down` (it was checked just now) and isn't moved. Tests:
  `test_a_run_that_fails_during_the_row_read_keeps_its_backoff` and
  `test_a_run_that_succeeds_during_the_row_read_is_not_fetched_again`.
- **`force` lifted the Reddit direct-mode "fetched within its own interval" rule.** The spec gives
  `force` two exceptions (the backoff gate, and a 60 s cooldown), not this one, and lifting it let a
  forced check spend `dynamic_reddit`'s one-cycle 429 skip early. Fix: the rule now applies under
  `force` too, as the spec says. The PR9b tension is recorded under "Open items" for the plan
  owner. Test: `test_direct_mode_force_keeps_the_subreddit_interval_rule`.
- **The security review.** The separate security-lens review agent ran in the review pass. Its
  one finding, the stale row snapshot, is the backoff race fixed above. The Verification section
  says so.

Checks after the fixes:

- Full backend pytest on `popping_test`: **13 failed, 997 passed**. The 13 diff clean against the
  DNS-only baseline list. 997 = 994 + the 3 new tests.
- `tests/test_check_now_queue.py` ran 5 times in a row: 49/49 each time.
- `ruff check --select F` on the changed Python files: clean.
- Mutation checks for the new lines (4, all caught) are in the table above.
- No frontend files changed.
