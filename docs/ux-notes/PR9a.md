# PR9a: Migration 0031 and feed health backend

Backend track. The only migration in the plan. Backend changes and pytest only; no frontend,
flows or smoke states. Nothing changes on screen until PR9b. The owner merges this PR after
reading the rollback note below, which also goes in the PR body (D32).

The unit was written in two sessions. The first wrote the code, the migration and the tests and
stopped before its review. The second reviewed that work against the spec, changed one query
(the list's story counts, see Deviations), re-ran EXPLAIN on a new synthetic table, ran the
mutation checks and the full pytest, and finished these notes and the Device checks lines. A
third pass fixed the findings of the review agents (see Review fixes).

## Plan (written before the code)

### Anchors checked against main 520ee43

- Head is `0030_source_display_name.py` (`down_revision = "0029"`), so 0031 revises 0030.
- `_ingest` is a wrapper around `_ingest_once` (PR6a). The success path calls `mark_recorded()`
  just before it stamps `last_fetch_at`; the failure path is marked by the wrapper after it
  returns. The failure path looks the row up by name and records every exception the same way:
  `error_count + 1`, auto-disable at `_AUTO_DISABLE_THRESHOLD = 6`, otherwise a doubled interval.
- `_classify_error` returns blocked / not found / upstream error / http error / network / error.
  Network covers only four httpx classes.
- `fetch_rss` raises "malformed entries" only when `bozo` is set or the content type isn't
  feed-like. Checked with the installed feedparser: an ordinary `text/html` page parses with
  `bozo=False`, zero entries and `version=''`, so today it is a zero-item success. That is the
  "A zero-item fetch counts as success" root cause for RSS feeds pointed at a web page.
- nvd, cisa_kev and wikipedia_on_this_day `return []` on a fetch error and on a non-JSON body.
  github_releases returns `[]` per repo on a 304, a non-200 or an exception.
- `trigger_now` builds a fresh `_plugin_for(row)` for a dynamic row, so a manual run loses
  `GenericScrapePlugin._extracted_urls` and the job's `fetch_target`.
- `SourceOut` is built by the list (fills `net_vote_score`), `GET /sources/{id}`, POST, PATCH
  and the three `/auto` branches. Only the list fills a computed field.
- `queue_check`'s backoff gate reads the job's `next_run_time` (PR6a).
- `IngestResult` has no `status`.
- `test_slice23_defusedxml.py:105` pins the single `ET.fromstring` in `rss.py`.
- `backend/Dockerfile:48` is `alembic upgrade head && uvicorn`, as the plan's rollback says.

### Migration text

```python
revision = "0031"
down_revision = "0030"

def upgrade() -> None:
    op.add_column("sources", sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sources", sa.Column("last_item_count", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("last_new_count", sa.Integer(), nullable=True))

def downgrade() -> None:
    op.drop_column("sources", "last_new_count")
    op.drop_column("sources", "last_item_count")
    op.drop_column("sources", "last_attempt_at")
```

A nullable column with no default is a catalog-only change in Postgres 16: no table rewrite.
The migration runs at container start, before uvicorn, so nothing else holds a lock on
`sources`.

## What changed

- **Migration `0031_source_health.py`** (`down_revision = "0030"`, the only new head): adds
  `sources.last_attempt_at` (timestamptz), `last_item_count` (int) and `last_new_count` (int). All
  three are nullable with no default and no backfill. The downgrade drops the three and nothing
  else. `models.py` gains the three `Optional` fields.
- **`_ingest` bookkeeping** (`scheduler.py`):
  - `_stamp_attempt(source, mark_recorded)` is the one place `last_attempt_at` is written. Every
    path that records an outcome calls it inside the transaction that writes the outcome: a
    success (where `last_fetch_at` gets the same instant), a failure, and the setup states. It
    marks the run recorded first, so a Check now that saw the run in flight has a `queued_at` at
    or before the stamp (the PR6a rule, now for failures too).
  - A success also writes `last_item_count` (what the plugin listed, before normalising and the
    ingest rules cut it) and `last_new_count` (stories inserted). A failure leaves the last
    success's counts alone.
  - A paused feed returns `summary["status"] = "paused"` and writes nothing. `IngestResult` gains
    `status: Optional[str]`, so `POST /api/ingest/{name}` carries it.
  - The failure path now loads the row by id under the same `FOR NO KEY UPDATE` lock as the
    commit session and applies PR4a's in-flight address guard: an error from an address the feed
    no longer has is not recorded (no `error_count`, no backoff, no stamp).
- **Setup states (D27):** `SETUP_STATE_KINDS = {"no list", "reddit off"}`. They write
  `last_error` and `last_attempt_at` and nothing else: no `error_count`, no backoff, no
  auto-disable, whatever the feed's age. They log one INFO line when the feed enters the state
  and DEBUG while it stays, with no traceback.
- **Reddit off:** for a `reddit` row, `_ingest` reads `reddit_client.is_disabled()` in its first
  session, next to the favicon gate, so a Reddit-off feed never asks reddit.com for its favicon.
  After the paused check (a paused feed still says `paused`) and before the fetch, it raises
  `RedditDisabledError` (new, in `reddit_client.py`), recorded as `[reddit off]`. Other feed
  types never look at the setting.
- **Favicon gate:** the hourly favicon retry is measured from the feed's last check of any
  outcome (`_last_check_of_current_address`): the newer of `last_attempt_at` and `last_fetch_at`.
  A `last_attempt_at` from before the feed's baseline (`created_at`, which an address change
  moves to the time of the edit) belongs to the old address and is ignored, so a new URL, whose
  cached favicon the edit cleared, still gets its favicon on its first check, as on main.
  Measured from `last_fetch_at` alone, a feed that never succeeds (a setup state is never backed
  off or stopped) retried its favicon on every tick.
- **Error kinds:** `_classify_error` gains `not a feed` (`NotAFeedError`), `no list`
  (`NoArticleListError`) and `reddit off`. `network` now covers every httpx timeout and transport
  error (`TimeoutException`, `NetworkError`, `RemoteProtocolError`, `ProxyError`,
  `TooManyRedirects`) and `asyncio.TimeoutError`. `ERROR_KINDS` lists every kind it can return,
  in the plan's table order, for PR9b.
- **`app/sources/base.py`:** `NotAFeedError(ValueError)` and `NoArticleListError` (defined here,
  raised from PR10b).
- **`fetch_rss`:** a 200 response with zero entries and no feed format recognised by the one
  `feedparser.parse` (empty `version`: a web page, XHTML, JSON, an empty body) raises
  `NotAFeedError` with "could not be parsed as a feed" in the message, so the Test route still
  reports `parse_error`. A valid empty RSS 2.0, RSS without a version, Atom or RDF feed is still
  `[]`. A recognised feed whose entries are broken keeps the old "malformed entries"
  `ValueError` (`[error]`). The single `ET.fromstring` is unchanged.
- **Built-ins raise:** `nvd`, `cisa_kev` and `wikipedia_on_this_day` log and re-raise a fetch
  error or a non-JSON body. `github_releases` still returns `[]` for one repo's 304 or failure
  and raises the first repo's error only when every repo failed (a 304 is not a failure); a
  non-200 is recorded as an `HTTPStatusError`, so a 403 rate limit reads `[blocked]`.
  `dynamic_reddit`'s throttle path is unchanged.
- **`SourceOut`** gains `last_attempt_at`, `last_item_count`, `last_new_count` (off the row) and
  `stories_24h`, `stories_7d`, `newest_story_at`, `has_stories` (computed).
  `routes/sources._health_counts(session, ids)` is one round trip: a 7-day `GROUP BY source_id`
  with `count(*) FILTER (fetched_at >= now - 24 h)`, `count(*)` and `max(fetched_at)`, outer
  joined to `sources`, plus a correlated `EXISTS` on `entries.source_id` for `has_stories`.
  `GET /sources/{id}`, POST, PATCH and all three `/auto` branches pass the one id (`WHERE
  source_id IN (:id)` in the 7-day scan); the list passes `None`, meaning every feed, so its scan
  has no id filter. All of them return it through `_source_out` / `_one_source_out`.
  `net_vote_score` keeps today's handling (list only).
- **`queue_check`'s backoff gate** reads `last_attempt_at`: a failing feed is backing off while
  its job isn't due and `now < last_attempt_at + _backoff_seconds(base, error_count)`, `base`
  being the built-in class's interval or the row's. A row with no `last_attempt_at` (checked
  before 0031) keeps PR6a's rule (the job's `next_run_time`). `_backoff_seconds` is shared with
  the failure path. `CheckNowResult`'s completion rule is now `last_attempt_at >= queued_at`.
- **`trigger_now`** runs the plugin instance the row's scheduler job holds, so a manual fetch
  keeps the job's `fetch_target` and per-instance state (`GenericScrapePlugin._extracted_urls`).
  `_plugin_for(row)` only when there is no job (a paused feed, or no scheduler). The comment in
  `routes/ingest.py` that described the old dispatch says so.
- **Docs:** `README.md` API rows, a "Feed health" subsection (the fields, the kinds table and the
  setup states), and the plugin guide now says `fetch` raises on failure. `ARCHITECTURE.md` is
  unchanged: no new process-local state. `docs/DEVICE_CHECKS.md` gains this unit's lines.

## Tests

New (72):
- `test_migration_0031_source_health.py` (2): the no_db head check (`ScriptDirectory` heads are
  `['0031']`, its `down_revision` is `'0030'`, nothing else branches off 0030) and the db cycle.
  The cycle creates a scratch database, runs `CREATE EXTENSION vector`, then `alembic upgrade
  head`, `downgrade 0030` and `upgrade head` with `python -m alembic` in a subprocess (as the
  container does), checks the three columns' type, nullability and lack of default through
  `information_schema`, that the downgrade drops only them (a row survives), and that the
  re-upgrade gives an existing row NULLs. It drops the database `WITH (FORCE)` in a `finally`.
- `test_source_health_fields.py` (15): the stamp is at or after the run's start on success,
  failure, `[no list]` and `[reddit off]` (without calling the Reddit fetch); success writes the
  counts (15 listed, 10 kept by the first-fetch rule) and `last_attempt_at == last_fetch_at`; a
  later poll of known stories counts 0 new; an empty success counts 0; a failure keeps the last
  counts; a first check that fails leaves them NULL; a paused feed writes nothing; a failure
  from an address the feed no longer has records nothing, while an ordinary dynamic failure is
  recorded. The favicon gate, with a `fetch_favicon` stub that counts (`offline_ingest` stubs it
  silently): a Reddit-off feed checked four times never asks for its favicon; a paused Reddit
  feed still says `paused` while Reddit is off and asks for nothing; a `[no list]` feed checked
  three times tries once, and again once its last check is an hour old; a failing feed whose last
  success was two hours ago tries once, not on every check; a feed moved to a new URL a moment
  after a check tries the new URL on its next check, then not again.
- `test_rss_not_a_feed.py` (12): HTML with and without `bozo`, XHTML, JSON and an empty body
  raise `NotAFeedError` with "parse" and classify as `not a feed`; valid empty RSS 2.0, RSS
  without a version, Atom and RDF are `[]`; a feed with items is unchanged; a truncated feed
  keeps the malformed error (`[error]`); `_test_source_impl` reports a web page as
  `parse_error`.
- `test_sources_story_counts.py` (10): the list, `GET /sources/{id}` (and that it matches the
  list), PATCH (also an empty PATCH), POST and `/auto` (a YouTube feed already followed) carry
  the counts; a feed with no stories; a feed with only stories older than a week counts only
  toward `has_stories` and has a null `newest_story_at`; the 24 h and 7 d edges; counts are per
  feed; the health columns come off the row on GET and the list.
- `test_ingest_paused_status.py` (5): `_ingest` and `POST /api/ingest/{name}` say `paused`; an
  active feed has no status; `IngestResult` keeps the field.
- `test_builtin_fetchers_raise.py` (28): nvd, cisa_kev and wikipedia_on_this_day re-raise 402,
  403, 404, 429 and 503 (each labelled by `_classify_error`), a connect error and a non-JSON
  body; github_releases leaves out one failing repo, returns `[]` when every repo is 304 or one
  is 304 and one fails, and raises the first repo's error (403 → `blocked`, connect errors →
  `network`, garbage → `ValueError`) only when every repo failed.

Extended:
- `test_scheduler_backoff.py` (+24): every network variant including `asyncio.TimeoutError`;
  the three new kinds; one case per prefix in the table; `ERROR_KINDS` is exactly the set
  `_classify_error` returns and holds `SETUP_STATE_KINDS`; `_backoff_seconds` doubles and caps;
  a 30-day-old feed that hits `[no list]` six times stays active with `error_count` 0 and is
  never rescheduled; `[no list]` leaves an earlier five-failure streak at 5; `[reddit off]`
  seven times never touches `error_count` 2; an ordinary failure still counts, backs off (the
  exact intervals) and stops at the sixth; an RSS feed is unaffected while Reddit is off.
- `test_trigger_now_accepts_dynamic_sources.py` (+2): the job's plugin is reused and
  `_plugin_for` isn't called; with no job, `_plugin_for` builds one.
- `test_check_now_queue.py` (+5, 3 strengthened): inside the backoff measured from
  `last_attempt_at` is backing off; a backoff that ran out before a restart (job 50 min out,
  last failure 3 h ago) is queued and moved up; a pre-0031 row falls back to the job; a
  built-in's class interval decides, not the row's; a check during the failure commit sees the
  stamp land (or reports the feed just checked); the forced failure, the running job and the
  success-commit race now also assert `last_attempt_at`.

## Verification

- **Backend pytest** (database `popping_test`, worktree `PYTHONPATH`), after rebasing onto main
  da8d2a0 (PR10a) and after the review fixes: 1238 passed, 13 failed. The 13 failures are
  exactly the DNS-only baseline list (`/root/popping-env/baseline.txt`); nothing else fails.
  Main collects 1148 tests; this unit adds 103 (72 in new files, 31 in the three extended ones).
  The PR9a files alone: 172 passed. Before the review fixes the same run gave 1233 passed, and
  before the rebase 1095, each with the same 13.
- **Frontend:** not touched. No frontend file, flow or smoke state changes; the backend tests
  that pin frontend source run in the pytest above.
- **Mutation checks:** each guarded line broken, its test run, the file restored byte for byte
  from a copy and compared, then `git status` checked. All 42 failed on the rule they guard
  (the last five rows are from the review fixes).

  | Guarded line | Test | Failure |
  |---|---|---|
  | 0031 `down_revision = "0030"` → `"0029"` | head check | heads `['0030', '0031']` |
  | `last_item_count` → `NOT NULL DEFAULT 0` | db cycle | column tuple differs |
  | downgrade no longer drops `last_new_count` | db cycle | column still there after downgrade |
  | `if not setup_state:` → `if True:` | `[no list]` six times on an old feed | feed stopped |
  | the same | `[reddit off]` seven times | feed stopped (later runs say paused) |
  | Reddit-off check → `False` | Reddit off stamp | the Reddit fetch was called |
  | Reddit-off check for every feed type | RSS while Reddit is off | RSS feed got `[reddit off]` |
  | failure-path `_stamp_attempt` removed | failure stamp | stamp before the run's start |
  | failure path stamps only non-setup states | `[no list]` stamp | `last_attempt_at` is NULL |
  | failure marked recorded after its commit | failure-commit race | `last_attempt_at < queued_at` |
  | success stamps `last_fetch_at` separately | success stamp | the two stamps differ |
  | `last_item_count = len(kept)` | success counts | 10 ≠ 15 |
  | `last_new_count = fetched` | later poll of known stories | 4 ≠ 0 |
  | failure address guard → `False` | re-pointed failure | stamp recorded |
  | paused `status` removed | paused summary | `KeyError: 'status'` |
  | `IngestResult.status` removed | ingest route | `KeyError: 'status'` |
  | `NotAFeedError` classify branch removed | every kind | `error` ≠ `not a feed` |
  | `RedditDisabledError` classify branch removed | every kind | `error` ≠ `reddit off` |
  | `asyncio.TimeoutError` not network | network variants | `error` ≠ `network` |
  | backoff gate `True or …` | ran out before a restart | not queued |
  | backoff gate never inside the window | inside the window | not backing off |
  | gate uses the row's interval for a built-in | built-in class interval | not queued |
  | `trigger_now` job lookup off | job plugin reused | `_plugin_for` called instead |
  | rss not-a-feed check off | not a feed (HTML) | malformed `ValueError` instead |
  | rss not-a-feed for every empty result | valid empty RSS 2.0 | raised `NotAFeedError` |
  | nvd fetch error → `return []` | HTTP 403 raised | did not raise |
  | cisa_kev non-JSON → `return []` | non-JSON raised | did not raise |
  | wikipedia fetch error → `return []` | HTTP 403 raised | did not raise |
  | github `all(` → `any(` | one repo failing | raised |
  | github never raises | every repo failing | did not raise |
  | GET back to `SourceOut.model_validate(row)` | GET counts | zeros |
  | PATCH back to `SourceOut.model_validate(row)` | PATCH counts | zeros |
  | `/auto` existing feed without counts | `/auto` counts | zeros |
  | list without counts | list counts | zeros |
  | 7-day filter removed from the scan | list counts | 7 ≠ 5 |
  | 24-hour `FILTER` widened to the week | 24 h edge | 2 ≠ 0 |
  | `has_stories` from the week only | list counts | stale feed `False` |
  | favicon gate back to `last_fetch_at` only | `[no list]` once an hour | 3 tries, not 1 |
  | the same | failing feed once an hour | 3 tries, not 2 |
  | `and not reddit_off` removed from the gate | Reddit off, four checks | reddit.com asked once |
  | baseline comparison removed (the gate reads `last_attempt_at` or `last_fetch_at`) | new URL | new URL never tried |
  | Reddit-off raise moved before the paused check | paused Reddit feed | `KeyError: 'status'` |

  POST of a new feed always has zero counts, so no test can tell `_one_source_out` from
  `model_validate` there; the POST test checks the fields are present and null or zero.
- **EXPLAIN (ANALYZE, BUFFERS) of `_health_counts`** on a synthetic Postgres 16 database
  migrated with `alembic upgrade head` (production's indexes): 120 feeds; 400,300 entries with
  arrivals spread evenly over 540 days (5,088 in the last week); 40% of them in 6 busy feeds
  (26,666 each), about 2,142 in each of 112 ordinary feeds, one feed with 300 stories all older
  than 8 days and one with none; each row with a 384-dim embedding and a 320-character meta
  summary (1,084 MB). `VACUUM ANALYZE`, default settings, warm cache, fastest of 5 runs. The
  statement is the one SQLAlchemy compiles from `_health_counts` for asyncpg, `PREPARE`d and run
  with `plan_cache_mode` forced to custom and to generic. Two physical layouts: arrival order
  (`CLUSTER` on `ix_entries_fetched_at`, how an append-only table looks) and scattered (random
  order, the worst case for the week's rows once rescoring updates have moved them).

  | Query | Arrival order: custom / generic | Scattered: custom / generic |
  |---|---|---|
  | list, as the route runs it (no id filter) | 5.1 ms / 9.1 ms | 9.6 ms / 141 ms |
  | list with every id in an `IN` (the first draft) | 5.6 ms / 12.4 ms | 43.1 ms / 85.2 ms |
  | one busy feed (26,666 stories) | 1.9 ms / 5.3 ms | 3.5 ms / 4.1 ms |
  | one ordinary feed | 0.5 ms / 0.5 ms | 0.7 ms / 0.7 ms |
  | a feed with only old stories | 0.3 ms / 0.2 ms | 0.5 ms / 0.4 ms |
  | a feed with no stories | 0.03 ms / 0.03 ms | 0.03 ms / 0.04 ms |

  - The list reads the week's range of `ix_entries_fetched_at` (an index scan in arrival order,
    a bitmap heap scan of about 5,000 blocks when scattered), hash-aggregates it and hash-joins
    it to `sources`. `has_stories` is an index-only scan of `ix_entries_source_id` per feed
    (`Heap Fetches: 0`, 361 buffers for 120 feeds).
  - With every id in an `IN`, the scattered layout made the planner `BitmapAnd` the week's range
    with a bitmap of the whole `ix_entries_source_id` (all 400,300 entries, 28 ms of the 43),
    because it can't tell that the list names every feed. That is why the list passes `None`.
  - One feed: a `BitmapAnd` of the week's range and that feed's `ix_entries_source_id` entries,
    then the same `EXISTS`. A feed with no stories stops at its empty `ix_entries_source_id`
    bitmap; the `fetched_at` bitmap is never executed.
  - Generic plans: Postgres's plan cache only switches a prepared statement to its generic
    plan when that plan's estimated cost is no worse than the custom plans'. For the list it is
    several times worse (scattered: 103,894 against 16,705, a parallel sequential scan plus
    JIT, because `fetched_at >= $1` can't be estimated without the value), so the custom plan is
    the one that runs; the column is there for completeness.
  - The alternative the spec rules out, an all-time `max(fetched_at)` for the feed with no
    stories, walks the whole `ix_entries_fetched_at` backwards and filters out all 400,300 rows:
    405 ms in arrival order, 1,626 ms scattered. An all-time `GROUP BY` over every feed is a
    parallel sequential scan, 188 ms.
  - PR9b's 5-second poll of `GET /sources/{id}` therefore costs under a millisecond for an
    ordinary feed and a few for the busiest. The scratch database was dropped afterwards.
- **Read-only check on the server (optional, safe before or after merging):** the list's
  statement by hand. It reads only existing columns, so it runs on today's database too:

  ```
  cd /opt/popping && docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "EXPLAIN (ANALYZE, BUFFERS) SELECT s.id, c.stories_24h, c.stories_7d, c.newest_story_at, EXISTS (SELECT 1 FROM entries e WHERE e.source_id = s.id) AS has_stories FROM sources s LEFT JOIN (SELECT source_id, count(*) FILTER (WHERE fetched_at >= now() - make_interval(hours => 24)) AS stories_24h, count(*) AS stories_7d, max(fetched_at) AS newest_story_at FROM entries WHERE fetched_at >= now() - make_interval(days => 7) GROUP BY source_id) c ON c.source_id = s.id"'
  ```

  Expected: `Execution Time` in the low tens of milliseconds at most, with
  `ix_entries_fetched_at` in the plan and no sequential scan of `entries`.
- **Security checklist:** no new route; GET, POST, PATCH and `/auto` only gain response fields;
  no client-written JSON is read; no new network I/O (the built-ins and `fetch_rss` keep their
  clients and SSRF hooks). Every response field is either a column or a count.

## Review fixes

The review agents (migration and rollback; ingest bookkeeping; query cost) confirmed four
findings. All four are fixed in the second commit, "PR9a: review fixes".

- **A frontend-only rollback could take the backend down** (major). The documented
  `POPPING_IMAGE_TAG=sha-<last good> docker compose up -d frontend` also recreates the backend
  on the old tag, as the frontend's dependency, and a backend image older than PR9a can't start
  against a database at 0031. Fixed: `--no-deps` in `docs/DEVICE_CHECKS.md` and in the
  rollback note, with the reason; the plan's copy of the command is an open item below.
- **The image-tag rollback's "or stamp afterwards" left the backend down** (minor). Step 3's
  commands resolve `:latest`, which after a tag rollback is still the PR9a image. Fixed: the tag
  rollback now gives only the downgrade-first order, plus the way back if it was missed; step 3
  says which case it is for, and that a stamp to 0031 must come before any later update while
  the deployed main has the migration.
- **Setup states stopped the hourly favicon throttle** and **a Reddit-off feed asked reddit.com
  for its favicon on every check** (two minor findings, one gate). `[no list]` and
  `[reddit off]` never move `last_fetch_at`, which the gate read, and the Reddit-off check came
  after the favicon fetch. Fixed in `_ingest_once`'s first session: Reddit off is read there and
  skips the favicon, and the gate is measured from the last check of the feed's current address
  (Favicon gate above, and Deviations). Five new tests in `test_source_health_fields.py`.

## Deviations from the spec

- **"One finally-style block" for `last_attempt_at`.** The stamp is written by one helper,
  `_stamp_attempt`, from the two places that record an outcome (the success commit and the
  failure/setup-state commit), inside the same transaction as the outcome. A literal `finally`
  would need a third session after the outcome's commit, so a poll could see a new `last_error`
  with an old `last_attempt_at`, and it would also stamp the paths that check nothing (a paused
  feed, a deleted built-in, a feed re-pointed mid-fetch), which would tell PR9b's Check now that
  a check landed when none did. The spec's list of paths (success, failure, `[no list]`,
  `[reddit off]`) is covered exactly.
- **`NotAFeedError` is "zero entries and no feed format", not "zero entries and HTML".**
  feedparser parses an ordinary `text/html` page with `bozo=False`, so the old bozo check let it
  through as an empty success; the parse's `version` (from the one parse) catches every page,
  challenge, JSON body and empty body, which all have the same fix (the address). A recognised
  feed whose body is broken (bozo, zero entries) keeps the old malformed `ValueError`, because
  its fix is Check now, not a new address.
- **The list's story counts have no id filter.** The spec's helper is `_health_counts(session,
  ids)`; the list calls it with `None` (every feed) instead of every id. The result is the same,
  and EXPLAIN showed the all-ids `IN` costing up to 4 times more (above). GET by id keeps
  `WHERE source_id = :id`, as the spec says.
- **The failure path's address guard** is new: PR4a's in-flight guard covered only the commit
  session. Without it, an error from the old address would count a failure against the new one
  and stamp `last_attempt_at` for a check the new address never had.
- **`[network]` covers every httpx transport error and `asyncio.TimeoutError`**, not only the
  four classes it matched before (connect error, connect and read timeout, too many redirects).
  A write or pool timeout, a dropped connection or a plugin's own `wait_for` budget used to read
  `[error]`, whose fix is the same Check now; the spec says timeouts map to `[network]`.
- **Setup-state logging:** INFO when a feed enters the state, DEBUG while it stays. Reddit-off
  feeds no longer reach `DynamicRedditPlugin.fetch`, so its one-shot WARNING (and
  `_warned_disabled`) only fires for a direct call; the INFO line and `last_error` replace it.
- **The favicon gate reads the last check of the current address** (review fix), not simply
  `last_attempt_at or last_fetch_at` as the review suggested. That simpler gate would hold back a
  re-pointed feed's favicon: `update_source` clears the favicon on a URL change and resets
  `last_fetch_at`, which on main made the next check fetch the new favicon, but the old
  address's `last_attempt_at` is never an hour old on a feed checked every 15 to 30 minutes, so
  the new favicon would never be fetched. The comparison with `created_at` mixes the database's
  clock with the app's; both containers read the host's clock, and a skew would cost one extra
  try or an hour's delay, nothing more.
- **Rollback step 2's order.** The plan's text downgrades, then merges the revert and waits for
  the build. The note below merges the revert and waits for the build first, then downgrades
  and runs `popping-update` straight away. Merging deploys nothing, so the downgrade still runs
  on the deployed 0031 image before any reverted image starts, which is the rule the plan's
  order exists for; the change only shortens the time the running 0031 backend spends asking
  for dropped columns from a whole CI build to about a minute.

## Risks and data-compatibility notes

- Built-ins that used to swallow errors (NVD, CISA KEV, On this day, GitHub releases when every
  repo fails) now record them, count toward `error_count`, back off and can be stopped after six
  failures in a row, like every other feed. That is the fix for "fetchers swallow errors"; a
  user re-enables a stopped built-in from Feeds as today.
- An RSS feed pointed at a web page now fails as `[not a feed]` instead of succeeding with
  nothing, so after six checks it stops (PR11c's Find its feed is the fix). The existing
  FeedEditor shows "Popping couldn't read this feed." for the new kinds until PR9b.
- Old frontends ignore the new `SourceOut` fields and `IngestResult.status`. Rows from before the
  migration keep NULL in the three columns until their next check; PR9b falls back to
  `last_fetch_at`.
- `queue_check` can now queue a failing feed whose backoff ran out before a restart, which PR6a
  reported as backing off until its re-registered job came due.
- A setup state leaves an earlier failure streak (`error_count`) and its backed-off interval as
  they were; the next success or the user's re-enable resets them, as today.

## Rollback (goes in the PR body)

Read this before merging, and again before deploying (D32). PR9a changes only the backend image,
but from its deploy on, no backend image older than PR9a may start unless the schema was
downgraded first (step 2) or stamped (step 3). That includes a frontend-only rollback.

**Frontend-only rollback, from PR9a's deploy on:**
`cd /opt/popping && POPPING_IMAGE_TAG=sha-<last good> docker compose up -d --no-deps frontend`
- Without `--no-deps`, compose also brings up the frontend's dependency, the backend, and
  recreates it on the old tag, because both images take `POPPING_IMAGE_TAG` and its image
  reference changed. If Last good is older than PR9a, that backend image can't start against a
  database at 0031 (below), and the whole app is down.
- If that already happened (the backend restarts over and over on the old tag), put the backend
  back on the image it ran, the backend sha from Step 0's revision check:
  `cd /opt/popping && POPPING_IMAGE_TAG=sha-<that sha> docker compose up -d --no-deps backend`.
- `docs/DEVICE_CHECKS.md` carries the corrected command.

**Why the order matters.** The backend container starts with `alembic upgrade head && uvicorn`
(`backend/Dockerfile:48`). An image without `0031_source_health.py` can't start against a
database at revision 0031: Alembic can't locate revision 0031, the upgrade exits with an error,
and uvicorn never starts. Every step below avoids that state, and step 3 is the way out if it
happens anyway.

1. **Preferred: revert only the app code and keep the migration.** The three columns are
   nullable with no default, so the code from before PR9a ignores them, and the database stays
   at 0031, which the kept file still describes.
   ```
   git revert --no-commit <PR9a merge sha>
   git checkout <PR9a merge sha> -- backend/alembic/versions/0031_source_health.py backend/tests/test_migration_0031_source_health.py
   git commit -m "Revert PR9a's app code, keeping migration 0031"
   ```
   Open that as a PR, merge it once CI is green, wait for the main build to publish the images,
   then run `popping-update`. Nothing to do on the database. Re-applying PR9a later needs
   nothing either: the head is already 0031.
2. **If the schema really must go** (only if the columns themselves cause a problem):
   1. Merge a full revert of PR9a, migration file included, and wait for the main build to
      publish the images. Merging deploys nothing: the 0031 image keeps running.
   2. With the 0031 image still deployed, downgrade with it:
      `cd /opt/popping && docker compose exec backend alembic downgrade 0030`
   3. Run `popping-update` straight away. It starts the reverted image against a database at
      0030.
   - Between steps 2 and 3 the running 0031 backend asks for columns that no longer exist, so
     feed lists and checks fail for about a minute. Nothing is written in that window.
   - The order matters: the reverted image has no 0031 script, so it can neither start against
     a database at 0031 nor run the downgrade itself.
   - The values in the three columns are lost. If PR9a is re-applied, they refill on each feed's
     next check.
3. **If that order was missed** and the backend no longer starts (the reverted image is up, the
   database is still at 0031, and `docker compose logs backend` shows Alembic failing to locate
   revision 0031):
   `cd /opt/popping && docker compose run --rm backend alembic stamp --purge 0030`, then
   `docker compose up -d backend`.
   - These commands resolve `:latest`, so they are only for step 2's case: the full revert is
     merged and published, and `:latest` is the reverted image. After a `POPPING_IMAGE_TAG`
     rollback, main still has PR9a; see the next bullet instead.
   - `--purge` is needed because the reverted image can't resolve revision 0031; it clears the
     version table and writes 0030 without reading the old value.
   - The columns stay (nullable, ignored by the reverted code).
   - From then on the database says 0030 while the columns exist, so any image that has
     migration 0031 fails at its `alembic upgrade head` (the columns already exist). Before any
     later `popping-update` while the deployed main has migration 0031 again (PR9a re-applied, a
     fix-forward, or step 1's revert, which keeps the file), stamp with that image before it
     starts:
     ```
     cd /opt/popping && git pull --ff-only && docker compose pull backend
     docker compose run --rm backend alembic stamp 0031
     popping-update
     ```
- **Rolling the backend image back past PR9a with `POPPING_IMAGE_TAG`** (main still has PR9a,
  so `:latest` is still a 0031 image): downgrade first, with the running 0031 image, then start
  the old image.
  ```
  cd /opt/popping && docker compose exec backend alembic downgrade 0030
  POPPING_IMAGE_TAG=sha-<full sha> popping-update
  ```
  - Between the two commands the running backend asks for dropped columns, as in step 2, for
    about a minute.
  - The next plain `popping-update` starts the PR9a image again, and its `alembic upgrade head`
    adds the three columns back cleanly. They refill on each feed's next check.
  - Don't use step 3's stamp here: its commands start `:latest`, the PR9a image, whose upgrade
    then fails on the columns that are still there.
  - If the old image was started without the downgrade and the backend no longer starts, run a
    plain `popping-update` first (the PR9a image starts against 0031 again), then the two
    commands above.
- `popping-update` prunes old images. To get an old build back, use a one-shot
  `POPPING_IMAGE_TAG=sha-<full sha> popping-update` on the command line, never a `.env` pin,
  which would silently stop every later update. Never edit tracked files in `/opt/popping`.
- As a git revert, PR9a stands alone only until a unit that depends on it merges (PR9b, PR10b,
  PR11a; PR10a only follows it in track order). After that, fix forward or revert newest-first.
- After any deploy or revert, relaunch every device.

## Open items for later units

- **PR9b:** `api.ts`'s `Source` type gains the seven fields; `feedHealth()` keys on the
  `last_error` prefixes in `scheduler.ERROR_KINDS` (also in README's table); `checkTabNow` and
  Check now wait on `last_attempt_at >= queued_at`; `applyServerSource` / `insertSource` adopt
  the fields, which every endpoint now returns. The no-list fix's PATCH also sends
  `active: true` (D27).
- **PR10b:** raises `NoArticleListError` (defined in `app/sources/base.py`); it is already a
  setup state here. The blocked and timeout website errors map to `[blocked]` and `[network]`
  through `_classify_error` as it stands.
- **PR11a:** the preview route's Reddit check can reuse `reddit_client.is_disabled()` as
  `_ingest` does; `NotAFeedError` gives the preview a "not a feed" result.
- **PR11c:** Find its feed is the fix for `[not a feed]`.
- `queue_check`'s cooldown still uses `last_fetch_at` (PR6a's rule); a setup-state feed is
  rechecked on each pull after `_last_queued` expires. Left as specified.
- **The plan's frontend-only rollback** (How I'll work, Deploy and device checks, step 4,
  `docs/UX_OVERHAUL_PLAN.md:483`) has the same command without `--no-deps`. It needs the same
  correction as `docs/DEVICE_CHECKS.md` the next time the plan is revised; this unit doesn't
  edit the plan, which changes only with a decision or the spec.

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md`:

- [ ] #<pr> · After `popping-update` (read the PR's rollback note first), run `cd /opt/popping && docker compose exec backend alembic current`. Pass: it prints `0031 (head)`.
- [ ] #<pr> · Force-quit and reopen the app, then open Menu → Settings → Feeds. Pass: stories load and every feed is listed as before.
- [ ] #<pr> · An hour after deploying, run `cd /opt/popping && docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT name, last_attempt_at, last_item_count, last_new_count, left(last_error, 30) AS error FROM sources ORDER BY last_attempt_at DESC NULLS LAST LIMIT 15"'`. Pass: the feeds checked since the deploy show a `last_attempt_at` after it, and the ones without an error show both counts.
