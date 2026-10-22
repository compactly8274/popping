# PR2b: prefSync client and sync rules (frontend track)

Saves, hides, votes and read marks go to the server as changes (PATCH
`add` / `remove`, or `set` for votes), not as whole lists, so two devices,
or two taps in one tick, never undo each other's. The app re-reads the
preferences on every poll (`?since=`) and on every return to the front (a
full GET), so something saved on the laptop shows on the phone within a
minute. Leaving the app sends what is still queued with `keepalive`.

From this build on, no code path PUTs `starred_entries`, `hidden_entries`,
`voted_entries`, `read_entries:*` or `seen:*`. That is PR2c's condition:
once both devices run this build, the server can refuse those PUTs.

## Plan (written before the code)

The unit was planned first (plan mode's stand-in: this section, written
before any code). It was started by one session, which stopped at the
org's spend limit after the plan, `lib/prefSync.ts` with its unit tests,
and the provider, `api.ts` and App changes; it was partway through
`capture.cjs` and the flows. A second session reviewed that work against
the spec, kept it, fixed what is listed under "Review of the earlier
work", finished the flows, and ran the local loop and the mutation checks.

- The sync rules go in `lib/prefSync.ts` as an engine with every effect
  injected (requests, clock, timers, visibility, a change callback), so
  every rule runs under `node --experimental-strip-types --test`.
  `preferences.tsx` is the React glue: one engine per provider, `state`
  decoded from the engine's rows with references kept, and the DOM
  listeners.
- The engine keeps, per key: `base` (the server's value last adopted, from
  a GET or a write's answer), the writes on the way (`flights`) and one
  folded queued write. What the screen shows is
  `base ⊕ flights ⊕ queued`, recomputed per key, with the old reference
  kept when the value is equal. One request counter orders sends, settles
  and GETs (sync rule 5 without clocks).
- App's handlers move to `addIds` / `removeIds` / `setVotes`, each Undo is
  the explicit inverse of what its action did, and handlers that decide on
  the current set read `prefs.latest()` instead of refs mirrored in
  effects.
- `capture.cjs` gets PATCH (as the server applies it), fault hooks, and a
  report of any whole-list PUT of a set key, which a new smoke rule (C17)
  fails in every state and flow.

## What changed

- `frontend/src/lib/prefSync.ts` (new, pure, no imports):
  - Keys and caps: `isSetKey`, `isVoteKey`, `capFor`; `MAX_HIDDEN` 1000,
    `MAX_STARRED` 1000, `MAX_VOTED` 2000 and the new `MAX_READ_PER_KEY`
    1000 (`read_entries:*` and `seen:*`), equal to the server's
    `SET_KEY_CAPS`. The caps moved here from `preferences.tsx`; App no
    longer imports any.
  - Values and changes: `coerceIds` / `coerceVotes` (the server's coercion,
    the same reference when already clean), `normalizeValue`, `sameValue`,
    `diffIds`, `foldDelta` (later op per id wins), `applyIdDelta` (remove,
    then add, new ids last), `applyVoteDelta`, `pruneIds(ids, keep, cap)`
    (ids outside `keep` first, then the smallest; returns `dropped`),
    `applyWrites`, `mergeIncoming(prev, rows, pending, {full})`,
    `stampMicros` / `laterStamp` (the `updated_at` string compared in
    microseconds), `classifyWriteError`, `legacySeedPlan`, from the
    review fixes `foldWrite` and `splitDelta` (one PATCH's worth), and
    from the final round `withoutOps` (a write without what others carry)
    and `isRolledBack` (a 404 or 405). (`overridden`, which the review
    fixes added for the leave flush's stale check, went with that check in
    the final round.)
  - `createPrefSync(deps)`: `addIds(key, ids, keep?)`, `removeIds`,
    `setVotes`, `put` and `delete` (both throw for a set key), `resync`,
    `leave`, `resume`, `flushPending`, `discardPending(drop)`,
    `registerBeforeFlush`, `rows()`.
- `frontend/src/lib/preferences.tsx`:
  - The provider holds one engine. `state` is decoded from its rows by
    `decodeState`, which reuses each part (a per-column map, a list) whose
    rows didn't change, so a resync that brings nothing re-renders nothing.
  - New context fields: `addIds`, `removeIds`, `setVotes`, `latest`,
    `resync`, `registerBeforeFlush`, `discardPending`. Gone:
    `setReadEntries`, `setHiddenEntries`, `setStarredEntries`,
    `setEntryVote`, the `MAX_*` exports and `MAX_PER_COLUMN`.
    `flushPending` keeps PR1r's contract. `loaded` now also turns true on a
    later full GET when the first one failed (PR1b's open item).
  - Listeners: `visibilitychange: hidden`, `pagehide` and `beforeunload`
    call `leave()`; `visibilitychange: visible` calls `resume()`.
  - The localStorage seed no longer writes at mount. The legacy values are
    read once; after the first full GET that succeeds, set keys go through
    `addIds` (only the ids the server lacks) and other keys are PUT only
    where the server has no row. The legacy keys are removed and the seed
    flag set only then, so a failed GET keeps them for the next launch.
  - Toasts: "Couldn't save your latest changes to Popping. They'll be lost
    when the app reloads." once retries are used up (unchanged text), and
    "Couldn't save that change. Reload Popping to try again." when the
    server refuses one; at most one per 30 s.
- `frontend/src/api.ts`: `patchPreference(key, delta, {keepalive})` (empty
  lists are left out of the body), `getPreferences(since?, signal?)`, and
  `keepalive` on `setPreference` and `deletePreference`.
- `frontend/src/App.tsx`:
  - `hideEntry`, `restoreHiddenEntry`, `toggleHideEntry`, `toggleStarEntry`
    (now over `setStarred(id, on)`), `markEntryRead`, `unmarkEntryRead`,
    `markColumnRead`, `unmarkColumnRead`, `toggleEntryRead`, the votes
    (`voteEntry`), Hide all downvoted, the Settings closures
    (`onRestoreHidden`, `onRestoreAllHidden`, `onUnstarAll` and its Undo,
    `onUnstarEntry`) and every Undo use the new calls.
  - `toggleStarEntry` returns whether the story was saved, read from
    `latestPrefs()`, and the five save sites use that for the toast and
    the Undo (`setStarred(id, wasStarred)`), so the Undo puts back exactly
    what the toggle changed.
  - The set-key aliases, the three set-key refs and their effects,
    `MAX_PER_COLUMN` and every App-side slice are gone.
  - `refresh()` calls `prefs.resync()`; the visibility handler calls
    `prefs.resync({ full: true })` on return; `resetLocalState` awaits
    `prefs.discardPending(resetDeletes)` (the keys it deletes) before its
    DELETEs, then reloads.
- `frontend/scripts/iphone-audit/capture.cjs`: PATCH on the preference
  store in the server's order (the body's shape as
  `UserPreferencePatchIn` checks it, 422; a key that isn't a set key, 400;
  `set` on an id list or `add` / `remove` on votes, 422; a list over the
  key's cap, 422; then `remove` before `add`, `null` clears a vote, the
  stored value coerced and trimmed smallest id first). `prefMock(context)`
  gives flows the store (`seed`, `value`, `keys`, `fail(status, {method,
  key, times})`, `faultsLeft`). `onSetKeyPut` reports every PUT of a set
  key.
- `frontend/scripts/iphone-audit/smoke.cjs`: rule C17 (no whole-list PUT of
  a set key) in every state and flow.
- `frontend/scripts/iphone-audit/flows.cjs`: new flows, `putPrefs` seeds
  the store directly, `prefWrites` records PATCH bodies, and the flows that
  answered `GET /api/preferences` with a fixed body seed the store instead
  (a fixed body would come back on every poll and return, which now
  re-read the preferences).
- `backend/tests/test_preferences_delta.py`: the caps pin reads
  `prefSync.ts` and pins `MAX_READ_PER_KEY` for `read_entries:*` and
  `seen:*`.
- `frontend/scripts/iphone-audit/README.md`: C17, the new flows and
  helpers, the mock's PATCH.

### Sync rules as built

1. At most one write in flight per key, PATCH and PUT alike. A queued
   write for a busy key goes in the first batch after the ack. Leaving the
   page is the exception (see Leaving): the earlier request is then kept
   as stale, and its answer sets nothing and sends nothing.
2. No answer, a 5xx, 404, 405, 408 or 429: the write is folded back ahead
   of the queued one (`foldDelta`; a PUT is dropped when a newer value is
   queued) and stays on screen. Retries go 2, 4 and 8 s after each
   failure; a key waiting on its retry timer is left out of batches. Once
   they are used up, one toast for the round; the write stays queued, and
   a later batch for the key, a return to the front, the reload flush or
   the leave flush sends it again (a return starts a new round of
   retries). Failures after the toast don't toast again until a new round.
   A write whose last failure was a 404 or 405 doesn't hold back PR1r's
   reload (`flushPending`): the server has no route for it, and only the
   reload onto the build it serves can cure that.
3. 400, 409, 422 and any other 4xx: the write is dropped, the screen falls
   back to the server's value plus what is still pending, the next resync
   is full, and a toast says so. The key's round of retries ends.
4. A write with no answer 30 s after it was sent (`Date.now`), or, once
   the page is back in front, one sent before it last hid or while it was
   hidden, is given up on: it stops blocking its key, its late answer is
   ignored, and it is not sent again. The next resync is full, and the
   first answer about the key after that (that GET, or a later write's
   answer) is adopted as it is. Until then the change stays on screen, so
   a save doesn't flicker off and back on when the page returns. Not
   sending it again is the point: it may have landed, and another device
   may have changed the same story since (a laptop's unsave), which a
   second send would undo.
5. A GET's row is ignored for a key that has a write on the way, or one
   that settled after the GET was sent (request numbers, not clocks). A
   queued write is re-applied on top of the row rather than ignoring it.
   PUT keys are covered the same way.
6. `mergeIncoming(prev, rows, pending, {full})`: full mode deletes a key
   missing from the response unless it is skipped or a pending write
   keeps it; since mode leaves missing keys alone; an empty list or object
   is a value; equal values keep their reference, and with nothing
   changed `prev` itself comes back.
7. One GET at a time. A poll during any GET, or within 1 s of the last
   GET being sent, is answered by it; a full request during a since GET
   runs right after it. Hiding the page aborts the GET on the way; a GET
   gives up after 20 s.
8. The watermark is the newest `updated_at` string seen in GET responses,
   never in PATCH or PUT answers. Polls send `since = watermark − 60 s`.
   No watermark yet, no successful full GET yet, or a refused write makes
   the resync full.
9. `discardPending(drop)` takes the keys Reset deletes (`read_entries:*`,
   `last_viewed:*`, `column_prefs:*`, `column_sections:*`,
   `history_group_by`; PR3b adds `seen:*`). For those it clears the queue
   and the retry timers, ignores what their writes on the way answer, and
   from then on queues and sends nothing, not a write made meanwhile, nor
   the leave flush's. Every other key (saves, hides, votes, saved views)
   goes on: what is queued for it, or waiting on a retry, is sent at once
   with `keepalive`, and one that fails meanwhile is sent once more. It
   waits for all of that and for the writes on the way, at most 3 s
   (`DISCARD_WAIT_MS`, like PR1r's `FLUSH_CAP_MS`; one rule 4 has given up
   on isn't waited for), stops the GET, and no GET goes after it. Reset
   calls it before its DELETEs and reloads after; the reload's leave flush
   sends what is still queued for the kept keys. The wait is capped
   because an iOS fetch may never settle: uncapped, one stuck write left
   Reset doing nothing while every later change stayed unsent. A write
   that lands after the DELETEs can at worst put back a few read marks or
   one tab's setting.

- Batches: set keys in a fixed 1 s batch, the others (PUT or DELETE) in a
  fixed 250 ms batch; neither is a debounce.
- Bodies within the server's limits: a PATCH carries at most the key's
  cap per list (1000 ids, 2000 votes), which is what
  `UserPreferencePatchIn` and the route allow. Before a change goes, a
  remove (or a cleared vote) of an id is dropped when the id is not in
  base and no write the server may still hold added it: that id was added
  and taken back, or pruned, before it ever reached the server. Each id a
  write adds is kept with the number of the request that sent it until an
  answer to that request or a later one is adopted (re-check: not just
  any answer); the ids a write given up on (rule 4) adds are kept for
  good, as it may land after any answer. A remove kept when it wasn't
  needed costs nothing. A change still
  over the cap (a long failing stretch) goes in parts, one after the other
  under rule 1: removes and cleared votes first, adds last, so the server's
  trim after each part ends where one PATCH would. The rest stays queued.
- Leaving (`visibilitychange: hidden`, `pagehide`, `beforeunload`): every
  `registerBeforeFlush` function runs, then every queued key goes at once
  with `keepalive`, one key per request (a change over the cap sends its
  first part; the rest goes on the return). A key with a write still on
  the way sends that change again folded with the queued one: the page may
  not come back to send it after the answer. A set key with nothing
  queued whose change is on the way in an ordinary request (not
  `keepalive`) sends it again the same way (re-check): iOS may cut that
  fetch off, and a failure that only comes after the return is ignored
  under rule 4. One already sent with `keepalive` isn't sent a third time
  by a second leave flush, and a change that a carried request's OK
  answer showed landed isn't folded in again. The earlier request is kept
  as stale (`by`; the keepalive request lists it in `carries`): its answer
  never sets base and never sends anything (final round: which of the two
  landed last can't be told, see Deviations). When the keepalive request
  fails and is tried again, only what isn't known to have landed goes back
  in the queue: an earlier request that answered OK landed, and its row
  becomes base; one still on the way is waited for again like any request
  (rule 1); one given up on (rule 4) stays shown and isn't sent again;
  only one that failed too has its change sent again, with the queued
  change.
- Back in front: the provider's `resume()` gives up on writes sent before
  or during the hide (rule 4) and sends everything queued; App asks for
  the full resync.
- `flushPending()` (PR1r's reload): runs the before-flush functions, sends
  each queued key (a busy key after its ack, a change over the cap part by
  part), waits, doesn't send again a write it sent that failed, and
  returns whether nothing is left to retry. A write it sent that got a 404
  or 405 doesn't count as left (final round).
- Pruning: `addIds` prunes against what is shown with `pruneIds` (the
  added ids and `keep` are kept unless they alone pass the cap) and sends
  the dropped ids it held in the same PATCH's `remove`. `setVotes` clears
  the smallest other ids past `MAX_VOTED` as `null` in the same PATCH.

## Review of the earlier work

The second session read the WIP against the spec and `routes/preferences.py`
and kept it. Fixed:

- A refused write (rule 3) left the key's retry count where it was, so the
  next failure on that key waited 4 or 8 s instead of 2. A refusal now ends
  the round.
- `addIds` put every pruned id in `remove`, new ids included. Adding more
  ids than the cap holds at once could then send a `remove` list past the
  cap (a 422). Only ids held now go in `remove`.
- The save sites read "was it saved" from the render's `starredSet` and
  toggled from `latestPrefs()`; the two could disagree, and the Undo
  (`setStarred(id, wasStarred)`) would then put back the wrong state.
  `toggleStarEntry` now returns the state it read.
- The PATCH mock didn't check bodies. It now answers 422 where
  `UserPreferencePatchIn` does (an id of 0 or a string, an unknown field, a
  vote other than up / down / null, a non-canonical vote id, a list over
  the key's cap), in the server's order (body, then key, then pairing).
- The `discardPending` test didn't guard its `closed` flag (the mutation
  survived); it now makes a write and a resync after the discard and
  asserts neither goes out.
- Flow timing: under load (three smokes in parallel) `flow-visibility-flush`
  failed once in nine with "2 PATCHes of one save": the flow came back to
  the front before the page had taken in the keepalive PATCH's answer, so
  rule 4, as first built, sent it again. (The review fixes below make rule
  4 give such a write up instead.) The retry flows had the same race
  (a retry is timed from the answer, and the flows moved the fake clock
  after a fixed real-time sleep). H1's fetch wrapper now records
  `answered` (the app has read the body, or the request failed) and
  `answered(page, method, path)` reads it; the flows wait for it before
  moving the clock or the visibility. Two earlier sequential runs, right
  after the mutation runs' last rebuild, also had one failure each; their
  output wasn't kept, and 16 sequential runs after them passed, so that
  cause is the likely one but not proven.
- New flow `flow-prefs-fail-save-keeps-list` for the path PR1b's notes
  recorded: with the preferences GET failing, a save sent a one-story
  `starred_entries` PUT over the server's list.

## Tests

- Unit `frontend/scripts/unit/prefSync.test.ts` (31 tests, against a fake
  server that applies PATCH as `routes/preferences.py` does, a fake clock
  and requests a test answers when it wants): the helpers; two adds in one
  tick; add then remove; two devices; an in-flight change surviving a stale
  GET; a PUT key in flight surviving a stale full GET; reference identity;
  caps and prune order (smallest first, dropped ids in `remove`, client and
  server holding the same set, more new ids than the cap); votes; a failed
  change folded back ahead of newer ones with retries at 2, 4 and 8 s and
  one toast; 405 kept, 400 and 422 dropped, a refusal ending the retries; a
  full request during a since GET; the watermark only from GETs with the
  60 s overlap; the in-flight deadline; coming back after a hide; leaving
  (collectors first, a busy key's change resent folded); `flushPending`;
  `discardPending`; the first full GET failing; the legacy seed producing
  no set-key PUT and removing no server id; set keys refused by `put` and
  `delete`.
- Flows (390): `flow-prefs-resync`, `flow-visibility-flush`,
  `flow-prefs-retry`, `flow-prefs-405`, `flow-prefs-fail-save-keeps-list`
  (new); `flow-reset-keeps-saves` extended (a save and a read mark queued
  when Reset is tapped: no PATCH or PUT after the DELETEs; since the final
  round, the save goes before the DELETEs and is kept, and nothing for a
  key Reset deletes goes at all);
  `flow-h1-pref-store` covers PATCH and its 400s and 422s;
  `flow-reload-on-new-build` steps 6-8 follow the PATCH and the leave
  flush (the pending save now goes when the page hides, not on return);
  `flow-tab-stays-on-star` waits for the PATCH; `flow-unsave-last-saved`,
  `flow-tab-migrates-index`, `flow-foryou-down-settles` and
  `flow-fl-rename-keeps-filter-and-view` seed the store instead of a fixed
  GET body. FL's `filter_presets` write stays a PUT.
- Smoke rule C17 in every state and flow: any `PUT /api/preferences/<key>`
  of `starred_entries`, `hidden_entries`, `voted_entries`, `read_entries:*`
  or `seen:*` fails the run.
- Backend: `test_set_key_caps_match_the_frontend` reads `prefSync.ts`.

## Verification

- `npm run lint`: 0 errors, 5 warnings, as on main (four
  `react-refresh/only-export-components` and the old `toast` dependency in
  `markColumnRead`). `npm run
  build` (tsc + vite) clean. Unit tests 128 of 128 (31 in
  `prefSync.test.ts`). `git diff --exit-code` on `package.json` and the
  lockfile: clean. The `node_modules` symlink was made for the loop and
  removed before the commit.
- Full strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173):
  0 failures, 0 warnings, 317 s, on the final code (and 0 failures on the
  WIP before the fixes).
- The PR2b flows plus `flow-reset-keeps-saves`, `flow-h1-pref-store`,
  `flow-h1-keepalive` and `flow-reload-on-new-build`: 10 sequential runs
  (before the `answered` wait), then after it 3 rounds of 3 parallel runs
  and 2 rounds of 5 parallel runs (with the PR1b save flows added): 0
  failures in all 19 stressed runs.
- Backend pytest (`popping_test_f`): 1238 passed, 13 failed, the 13 being
  exactly the DNS-only baseline list. The tests that pin frontend source
  (`test_preferences_delta.py`, `test_preferences_anonymous.py`, slice19,
  slice21, slice15, slice30, the sitemap link pattern, reserved category
  names) re-run on the final tree: 168 passed, the 2 baseline DNS failures.
- Screenshots looked at (not committed): `flow-prefs-405`'s end (the story
  still shows saved under one "Couldn't save your latest changes" toast),
  `flow-prefs-fail-save-keeps-list`'s end (Saved lists both the server's
  save and the new one), `flow-prefs-resync`'s end (the hidden story stays
  gone).

### Mutation checks (test → broken line → failing rule)

Applied by a script, built where needed, run alone, and restored with
`git checkout` plus `git diff --exit-code`.

| Test | Broken line | Fails on |
|---|---|---|
| `prefSync.test.ts` | rule 5: no skip for a key settled after the GET was sent | "an in-flight change survives a stale GET…" |
| `prefSync.test.ts` | rule 5: no skip for a busy key | the same, and "a PUT key in flight survives a stale full GET" |
| `prefSync.test.ts` | rule 1: a busy key sent anyway | "add then remove…", the deadline test, "coming back after a hide…" |
| `prefSync.test.ts` | rule 2: the failed change folded after the queued one | the deadline test (the queued remove lost to the old add) |
| `prefSync.test.ts` | rule 3: every error retried | "405 keeps… 400 and 422 drop it", `flushPending` |
| `prefSync.test.ts` | rule 3: a refusal keeps the retry count | "405 keeps…" (retried 2000 ms after the failure) |
| `prefSync.test.ts` | rule 6: full mode deletes nothing | "full mode deletes a missing key…" |
| `prefSync.test.ts` | rule 6: an empty list from the server ignored | the same |
| `prefSync.test.ts` | rule 7: a full request during a since GET answered by it | "a full request during a since GET runs right after it" |
| `prefSync.test.ts` | rule 8: a PATCH answer moves the watermark | "the watermark moves only with GETs" |
| `prefSync.test.ts` | rule 8: no 60 s overlap | the same |
| `prefSync.test.ts` | rule 4: no 30 s deadline | the deadline test never finishes (killed at 60 s: the held batch re-arms at once) |
| `prefSync.test.ts` | rule 4: no expiry on return | "coming back after a hide…" |
| `prefSync.test.ts` | rule 9: `discardPending` leaves `closed` false | `discardPending` (after the test was strengthened; it survived before) |
| `prefSync.test.ts` | `pruneIds` drops the largest first | `pruneIds`, caps, votes |
| `prefSync.test.ts` | `addIds` sends every dropped id in `remove` | caps (more new ids than the cap) |
| `prefSync.test.ts` | `mergeIncoming` never keeps a reference | three reference tests |
| `prefSync.test.ts` | the legacy seed writes set keys whole | "legacy localStorage lists plus server rows…" |
| `prefSync.test.ts` | leaving sends without `keepalive` | the retry test, "coming back…", "leaving…" |
| `prefSync.test.ts` | a second change replaces the queued one | "two adds in one tick", two devices, the retry test |
| `prefSync.test.ts` | `flushPending` always true | `flushPending` |
| `prefSync.test.ts` | `put` takes set keys | "set keys are never written whole" |
| `flow-prefs-resync` | rule 5 settled skip removed | PREFS-RESYNC: a GET sent before the hide's answer, answered with the old set, brought story 101 back |
| `flow-prefs-resync` | rule 5 busy skip removed | the same (the GET's base outranks the PATCH's answer) |
| `flow-visibility-flush` | `visibilitychange: hidden` doesn't call `leave()` | VIS-FLUSH: hiding the page sent no PATCH of the save |
| `flow-visibility-flush` | the transport drops `keepalive` | VIS-FLUSH: the PATCH sent when the page hid has no keepalive |
| `flow-prefs-retry` | a 5xx refused, not retried | PREFS-RETRY: 2 PATCHes before the retry, expected 1 |
| `flow-prefs-405` | 405 refused, not retried | PREFS-405: after 1 answers of 405 the story no longer shows as saved |
| `flow-reset-keeps-saves` | Reset's DELETEs without `discardPending()` | RESET: writes after the DELETEs (PATCH starred_entries, PATCH read_entries:news); the server kept read_entries:news |
| `flow-prefs-fail-save-keeps-list` | `setStarred` PUTs the list it holds (the old path) | C17; PREFS-FAIL-SAVE: the store holds [101]; the save sent PUT [101] |
| `flow-tab-stays-on-star` (C17) | the same | C17: PUT /api/preferences/starred_entries |
| `flow-h1-pref-store` | the mock ignores `remove` | H1-PREFS: remove before add gave [3,1,2,4] |
| `flow-h1-pref-store` | the mock's 422 for bad bodies removed | H1-PREFS: PATCH starred_entries {"add":[0]} was not a 422 (and two more) |
| `flow-reload-on-new-build` | `flushPending` always true | RELOAD-SAVED: reloaded although the pending save did not get through |
| `test_set_key_caps_match_the_frontend` | `MAX_READ_PER_KEY` 999 | the `read_entries:*` assert |

The flows whose waits changed (`flow-visibility-flush`, `flow-prefs-retry`,
`flow-prefs-405`, `flow-reload-on-new-build`) were mutation-checked again
after the change, with the same results.

## Review fixes (second commit)

The review agents confirmed eight findings on the first commit. All eight
are fixed; none was skipped. Two of them (a major and a minor) were the
same problem from two angles.

- **A queued change could grow past what one PATCH may carry** (major,
  plus the `voted_entries` duplicate). While writes kept failing, every
  add on a key at its cap pruned one id into the folded `remove`, with no
  upper bound, so after a long offline stretch the server's 422 (lists
  over the cap) dropped every change made meanwhile. Fixed in `send()`:
  a remove or cleared vote of an id that is neither in base nor added by a
  write sent since base was last set is dropped (such an id never reached
  the server). The adds of failed or given-up writes count as sent: a
  request whose answer was lost may still have landed. Whatever is still
  over the cap goes in parts (`splitDelta`), removes and cleared votes
  first, adds last, one at a time under rule 1, with the rest queued.
  `flushPending` sends every part.
- **Rule 4 sent an unanswered write again** (major). On a return, or past
  the 30 s deadline, the first commit put the write back in the queue and
  sent it again. If the keepalive had landed and the laptop had since
  unsaved the story, the second send saved it again on every device. Rule
  4 now follows the spec: the write is given up on and not sent again,
  and the next resync is full. Its change stays on screen until the first
  answer about the key after that, so the return doesn't flicker. The
  claim that a second send is harmless is gone from the code and these
  notes. The grace-period open item is gone too; the safe resend is a
  plan change (see Open items).
- **The leave flush could be overtaken** (minor). It sends a busy key's
  change folded with the one still on the way. If the keepalive request
  landed first and the earlier one after it, an Undo made just before the
  hide was undone. The earlier request is now kept as stale (`by` points
  at the keepalive request): its answer never sets base, and when it comes
  after the keepalive request's answer and its row shows what the
  keepalive request reversed (`overridden`), that part is queued again,
  ahead of anything newer, and sent at once with `keepalive` while the
  page is hidden. When the earlier request answers first, nothing more
  goes. Order is not read from `updated_at`. (The final round found the
  row test vacuous, and removed the check and its resend.)
- **Reset waited with no limit** (minor). `discardPending` now waits at
  most `DISCARD_WAIT_MS` (3 s), and not at all for a write rule 4 has
  given up on, so Reset always goes on to its DELETEs and the reload.
- **A "Couldn't save" toast on the return** (minor). The return called
  `expire()`, which counted a retry for a key whose retries were used up
  and toasted before anything was sent. `expire()` no longer touches
  retries (rule 4 sends nothing again), and the toast is once per round
  of retries, so a long failing stretch shows one toast, not one every
  30 s.
- **The airplane-mode device check could fail on working code** (minor).
  Control Center doesn't hide the page, and nothing listens for `online`.
  The check now switches to the Settings app and back, and its pass
  allows for the laptop's 60 s poll.
- **README** (minor): rows for `flow-prefs-fail-save-keeps-list`, and the
  reload flow's changed steps.

Also changed, because rule 4 changed:

- A write sent while the page is hidden (a batch timer that ran before
  iOS suspended the page, or the stale check's resend) is given up on at
  the return as well (`whileHidden`), like one sent before the hide.
  Otherwise it would hold its key for up to 30 s after the return.
- `flow-reload-on-new-build` steps 8b and 8c relied on a resend at the
  return. Each hide now waits for its answer (`awayAfterAnswer`), and 8c
  has the server refuse the save sent by the hide once, so the return
  sends it and the reload must wait for it.

### Tests added or rewritten

`prefSync.test.ts`, now 43 tests. The fake server answers 422 for a list
or a votes map past the cap, as the route does, and the harness has an
offline mode where every request fails at once.

- Rewritten for rule 4: "an in-flight record is cleared by its deadline"
  (not sent again, late answer ignored, the next resync is full, its GET
  settles the key; a change queued behind goes alone) and "coming back
  after a hide" (given up on, still shown until the full GET, which keeps
  it when it landed and drops it when it didn't).
- New: another device unsaves while this page is hidden with its
  keepalive answer unread, and the unsave stands after the return; a
  write sent while hidden holds nothing up after the return; the return
  after the retries were used up shows no second toast, and the next
  failure starts a fresh 2, 4, 8 s round; the leave flush overtaken (the
  Undo goes again with `keepalive`, the server ends empty) and not
  overtaken (no extra PATCH); `discardPending` capped at
  `DISCARD_WAIT_MS` and not waiting for a given-up write; a key at its
  cap with 1200 adds while offline (every PATCH after reconnecting within
  the cap, two of them, the second without removes, the server and the
  screen on the newest 1000, one toast, nothing refused); the same for
  2100 votes, given 50 at a time; `hidden_entries` at its cap with Remove all, a hide and its
  Undo while offline (one PATCH, the server empty), and the variant where
  the hide reached the server but its answer was lost (the Undo goes in
  its own PATCH after the first part); a vote set and cleared before it
  goes sends nothing; `splitDelta`; `overridden`.
- Changed: "add then remove" (an id added and removed before it went is
  nothing to send).

### Verification of the fixes

- `npm run lint`: 0 errors, 5 warnings (as before). `npm run build`
  clean. Unit tests 140 of 140 (43 in `prefSync.test.ts`). `git diff
  --exit-code` on `package.json` and the lockfile: clean.
- Full strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173):
  0 failures, 0 warnings, 333 s.
- The PR2b flows, `flow-reset-keeps-saves`, `flow-h1-pref-store`,
  `flow-h1-keepalive`, `flow-reload-on-new-build`,
  `flow-tab-stays-on-star` and `flow-unsave-last-saved`: 18 sequential
  runs, 3 of them with the unit mutation script running alongside. 17
  ended with 0 failures and 0 warnings. One (the second, with a backend
  pytest running alongside) ended in Node's crash footer with no summary.
  Only its last line was kept, so its stack is lost; the 13 runs after
  it kept full logs, and none crashed. The footer fits Playwright
  rethrowing a "target closed" error from a route callback still running
  when `smoke.cjs` closes the context: `_onRoute` is called without a
  catch, and smoke closes contexts without `unrouteAll({ behavior:
  'ignoreErrors' })`. That is the harness from H1, not this unit's code,
  so it is an open item and not changed here; but it is not proven.
- Backend `test_preferences_delta.py` and `test_preferences_anonymous.py`
  (`popping_test_f`, they pin `prefSync.ts`): 89 passed.

### Mutation checks of the fixes

Unit mutations ran on a scratch copy of `prefSync.ts` and the test file;
the flow mutation was built into `dist`, run alone, and the file restored
from a copy (`cmp` clean).

| Test | Broken line | Fails on |
|---|---|---|
| `prefSync.test.ts` | `forServer` returns the change as it is | "add then remove", the vote set-and-cleared test, the three cap tests |
| `prefSync.test.ts` | `forServer` ignores the adds sent since base | the retry test, both leave-overtaken tests, the read and `hidden_entries` cap tests |
| `prefSync.test.ts` | the vote filter off | the vote set-and-cleared test, the votes cap test |
| `prefSync.test.ts` | no `noteSent` | the same five as ignoring the adds sent |
| `prefSync.test.ts` | no split (one PATCH of everything) | the three cap tests (422, refused) |
| `prefSync.test.ts` | the adds ride with the first remove part | `splitDelta`, the read cap test |
| `prefSync.test.ts` | `flushPending` stops after one part | the read and vote cap tests |
| `prefSync.test.ts` | rule 4 puts the write back in the queue (the old code) | the deadline test, "coming back", the hidden-send test, the other-device test |
| `prefSync.test.ts` | a later answer keeps the given-up change on screen | the deadline test, "coming back", the other-device test |
| `prefSync.test.ts` | giving up doesn't make the next resync full | the deadline test |
| `prefSync.test.ts` | a write sent while hidden is waited for | the hidden-send test |
| `prefSync.test.ts` | stale answers not checked | the overtaken test |
| `prefSync.test.ts` | the stale fix waits for a batch while hidden | the overtaken test |
| `prefSync.test.ts` | the adds sent are forgotten while a stale request is out | the overtaken test |
| `prefSync.test.ts` | `discardPending` waits with no limit | the `discardPending` cap test |
| `prefSync.test.ts` | `discardPending` waits for given-up writes | the same |
| `prefSync.test.ts` | a toast on every failure past the retries | the read and vote cap tests |
| `prefSync.test.ts` | the return keeps the retry count | the toast-on-return test |
| `flow-reload-on-new-build` | `flushPending` always true | RELOAD-SAVED: reloaded although the pending save did not get through |

One survivor, equivalent: checking a stale answer before the keepalive
request's answer is in. The fix it queues is then dropped by the filter
once that answer sets base (the id is in neither base nor the adds sent),
or folds into the same change when that request failed, so nothing
observable differs. (The final round removed the check and its resend
altogether; see its section.)

## Review fixes (final round)

The re-check of the second commit confirmed seven findings: two major and
five minor (three of them about tests). A fixer started on them and was
stopped partway (a WIP commit); this round reviewed that work, kept most
of it, replaced its fix for the stale check, and folded it into one
commit. All seven are fixed; none was skipped. No server change.

- **A failed leave-flush request sent again what an earlier request had
  already saved** (major). With a save on the way when the page hid,
  `leave()` sends its change again folded with what is queued. When that
  keepalive request failed with something worth retrying (a 503 during a
  backend restart, a network error), `settle()` put back the whole fold,
  so the retry sent the earlier request's change again even when its OK
  answer had shown it landed, and a laptop's unsave made meanwhile was
  undone. Now the keepalive request lists what it carries (`carries`),
  each earlier request keeps how it settled, and a retryable failure of
  the keepalive request goes through `unfold()`, which puts back only what
  isn't known to have landed: an earlier request that answered OK landed,
  and its row becomes base; one still on the way goes back on the list and
  is waited for like any request (rule 1), so its own answer decides; one
  given up on (rule 4) stays shown in limbo and isn't sent again (the next
  resync is full); only one that failed too has its change sent again.
  What goes back in the queue is the keepalive request's change without
  what those carry (`withoutOps`). This holds for a second leave flush too
  (`visibilitychange` then `pagehide`): an earlier request that answered OK
  while folded into the first stays on the second's list.
- **The stale check decided by answer order alone** (minor). `overridden()`
  kept only the ids where the earlier request's row "still shows its
  side", but that row always does (the server applies the request's own
  change before it answers), so the check sent the Undo again whenever the
  keepalive request's answer came first. When the earlier request had in
  fact landed first and only answered late, that resend undid a change
  another device made in between. Neither row's value can tell the two
  landing orders apart, so the resend is gone: a stale request's answer
  sets nothing and sends nothing, whichever order the answers come in.
  `overridden()` went with it. The WIP had instead made the server stamp
  `updated_at` with `greatest(now(), updated_at + 1 µs)` under the row
  lock and compared the stamps; that was reverted (`routes/preferences.py`
  and `test_preferences_delta.py` are as on the second commit). It changed
  how every PUT and PATCH stamps its row, in a frontend unit whose plan
  keeps the server on `now()`, and a guess-free fix is simpler. What this
  leaves open (the earlier request overtaken and landing last) is listed
  under Deviations. The "adds sent kept while a stale request is out"
  clause in `adopted()` only served the resend's remove, and is gone too.
- **Reset dropped unsent saves, hides, votes and saved views** (major).
  `discardPending()` cleared every key and closed the engine, so a save
  still in its batch, on a retry, queued after the retries, or failing
  during Reset's wait never reached the server, although Settings says
  saves, hides, votes and saved views stay. It is now
  `discardPending(drop)`: App passes `resetDeletes` (the History grouping
  and the four per-column kinds it deletes). Only those keys are dropped
  and closed (`enqueue` ignores them from then on, so neither a write made
  meanwhile nor the reload's leave flush sends them). Every other key is
  sent at once with `keepalive` (`flush(true)`, the loop `flushPending`
  uses, so one that fails meanwhile is tried once more), and Reset waits
  for that and for the writes on the way, at most 3 s, as before. What is
  still queued goes with the reload's leave flush. No GET goes after it,
  as before.
- **After a rollback, a 405 stopped PR1r's reload on every return**
  (minor). Rule 2 keeps a write that gets 404 or 405 queued, and
  `flushPending` answered false while anything was queued, so a resumed
  page on this bundle never reloaded onto the reverted build. A write's
  404 or 405 now marks it `rolledBack`, and `flushPending` answers true
  when everything left failed that way: only that reload can cure it.
  (The reload's leave flush sends it once more.) Rule 2 is unchanged.
- **The limbo layer was untested** (minor, tests). The "still shown"
  asserts in the deadline test and the "coming back after a hide" test
  read the last emission, which `expire()` doesn't change. Both tests now
  save and unsave another story before the full GET answers, which
  recomputes the key and sends nothing, and assert the given-up change is
  still shown. Removing limbo fails both.
- **No flow failed with the review fixes reverted** (minor, tests). New
  `flow-prefs-return-before-answer` holds the hide's keepalive PATCH
  across the return, lets it through after, and counts PATCHes; new
  `flow-prefs-landed-unread` lets it land, holds its answer, has another
  device unsave the story, then comes back. Both fail with rule 4 putting
  the write back in the queue, as 979dc6f's `expire` did.
  `flow-reload-on-new-build` gained step 8d for the rollback fix, and
  `flow-reset-keeps-saves` checks the Reset fix.
- **Two more fix hunks survived every unit test** (minor, tests). Reset
  waiting for a request the leave flush folded into a later one now has a
  test. The other hunk (`expire()` giving up on a folded request, at the
  return or at its deadline) was caught by a probe whose late answer
  queued the stale check's resend;
  with the resend gone, no answer of a folded request queues anything, so
  that probe can't fail any more. It stays as a test ("given up on at the
  return: its late answer queues nothing"). What the hunk still decides is
  that such a late answer is ignored (rule 4) rather than taken as the
  server's row when the keepalive request then fails; a new test covers
  that (the next resync is full), and fails without the hunk.

### Tests added or changed (final round)

`prefSync.test.ts`, now 57 tests (154 unit tests in all).

- New, leave flush: the earlier request landed first but answers last,
  for ids and for votes (nothing more goes, and a laptop's later change
  stands); the keepalive request fails after the earlier one landed,
  fails before its OK answer, and fails on a desktop tab left hidden whose
  2 s retry runs while hidden (each time only the queued change goes
  again, and the other device's unsave stands); it fails while the earlier
  one is still on the way (the retry waits for it, and when it fails too
  both changes go); an earlier request given up on at its deadline while
  folded, then the keepalive request fails (still shown, not sent again,
  the next resync full), and the same with the given-up request's late
  answer arriving first (ignored); two leave flushes in a row with both
  later requests failing; a folded request given up on at the return whose
  late answer queues nothing.
- Rewritten: "lands after the leave flush and undoes an Undo, the Undo
  goes again" is now "the earlier request's answer sends nothing more",
  for both answer orders, and checks that the full GET then shows what the
  server holds. The `overridden` test is gone with the function.
- New, Reset: `discardPending(resetDeletes)` sends the hide at once with
  `keepalive` and drops the read mark and the tab setting, and afterwards
  sends a new save but nothing for the dropped keys; it waits for a
  request the leave flush folded; a vote whose retries were used up, a
  hide waiting on its retry and a save in its batch all reach the server;
  a save whose PATCH answers 503 during the wait is sent again at once,
  and what still fails goes with the reload's leave flush. The two old
  `discardPending` tests were rewritten for the key filter.
- New: `flushPending` answers true when what is left got 405 or 404, false
  with a 503 on another key; the reload's leave flush still sends both.
  `withoutOps`; `isRolledBack` (in the `classifyWriteError` test).
- Changed: the deadline and "coming back" tests (limbo). The two
  "given up on while folded" tests seed a row first, so the page has a
  watermark and "the next resync is full" can fail (with an empty server
  every resync is full).
- Flows: `flow-reset-keeps-saves` now asserts the queued save goes before
  the DELETEs and is stored next to 103, and that no write for a key Reset
  deletes goes at all; `flow-prefs-return-before-answer` and
  `flow-prefs-landed-unread` (new); `flow-reload-on-new-build` step 8d
  (every PATCH answers 405 and the return still reloads; step 9 now counts
  4 reloads). README rows for all four.

### Verification (final round)

- `npm run lint`: 0 errors, 5 warnings (as before). `npm run build`
  clean. Unit tests 154 of 154 (57 in `prefSync.test.ts`). `git diff
  --exit-code` on `package.json` and the lockfile: clean. The
  `node_modules` symlink was made for the loop and removed before the
  commit.
- Full strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173):
  0 failures, 0 warnings, 851 s (other units were running on the
  container).
- The PR2b flows, `flow-prefs-return-before-answer`,
  `flow-prefs-landed-unread`, `flow-reset-keeps-saves`,
  `flow-h1-pref-store`, `flow-h1-keepalive`, `flow-reload-on-new-build`,
  `flow-tab-stays-on-star` and `flow-unsave-last-saved`: one run, then 3
  more in a row, 0 failures and 0 warnings each time (132 to 138 s).
- Backend `test_preferences_delta.py` and `test_preferences_anonymous.py`
  (`popping_test_f`, they pin `prefSync.ts`): 89 passed. The backend is
  as on the second commit.

### Mutation checks (final round)

Each reverts one hunk of these fixes. Unit mutations ran on a scratch copy
of `prefSync.ts` with this test file. Flow mutations were built from a
scratch copy of `frontend/` into the worktree's `dist`, served on 4173 and
run alone; the worktree's sources were not touched, and `dist` was rebuilt
clean after.

| Test | Broken line | Fails on |
|---|---|---|
| `prefSync.test.ts` | a failed keepalive request puts back its whole fold (the old `settle`) | the three "fails after / before the earlier one landed" tests, "still on the way", both "given up on while folded" tests, "leaving twice" |
| `prefSync.test.ts` | an earlier request still on the way isn't put back on the list | "fails first, then the earlier one answers OK", "still on the way" |
| `prefSync.test.ts` | a landed earlier request's row doesn't become base | "fails after that one landed" (5 vanishes from screen), "leaving twice" |
| `prefSync.test.ts` | one given up on while folded isn't put in limbo | both "given up on while folded" tests |
| `prefSync.test.ts` | an earlier request that answered OK isn't carried by a second leave flush | "leaving twice" |
| `prefSync.test.ts` | the stale answer checked as the second commit did (answer order, row test) | "the earlier request's answer sends nothing more", both "landed first but answers last" |
| `prefSync.test.ts` | a folded request isn't given up on (`expire`'s stale loop gone) | "the late answer ... is ignored" |
| `prefSync.test.ts` | `discardPending` drops every key (the old one) | the four Reset tests |
| `prefSync.test.ts` | `discardPending` doesn't send the kept keys | the same |
| `prefSync.test.ts` | it sends them without `keepalive` | three Reset tests |
| `prefSync.test.ts` | `enqueue` ignores the dropped keys | the two `discardPending` tests |
| `prefSync.test.ts` | `discardPending` doesn't wait for stale requests | "waits for a write the leave flush folded" |
| `prefSync.test.ts` | `flushPending` answers false while anything is queued (the old one) | the 404 / 405 `flushPending` test |
| `prefSync.test.ts` | every retry counts as rolled back | both `flushPending` tests |
| `prefSync.test.ts` | no limbo (the given-up write isn't kept on screen) | the deadline test, "coming back" |
| `prefSync.test.ts` | rule 4 puts the write back in the queue (as 979dc6f's `expire` did) | the deadline test, "coming back", the hidden-send test, the other-device test |
| `flow-reset-keeps-saves` | `discardPending` drops every key | RESET: the queued save was never sent; the store's saves are [103] |
| `flow-reset-keeps-saves` | `discardPending` drops no key | RESET: a write for a key Reset deletes went (PATCH read_entries:news) |
| `flow-prefs-return-before-answer` | rule 4 puts the write back in the queue | PREFS-RETURN: 2 PATCHes of one save (2 by the return) |
| `flow-prefs-landed-unread` | the same | PREFS-LANDED: the other device's unsave was undone (the store holds [101]); 2 PATCHes; the story still shows as saved |
| `flow-reload-on-new-build` | `flushPending` answers false while anything is queued | RELOAD-ROLLBACK: saves answered 405 kept the page from reloading |

No survivors. The two-device fuzz from the re-check (2000 seeds of 300
steps, strict landing order, so it can't produce the overtaken case) flags
3 seeds on the second commit (1363, 1385, 1742) and 1 on this one (1363:
the fold window under Deviations).

Device checks: unchanged. The #38 Reset line (save, then Reset reading
state) and the #40 lines already cover what these fixes touch on a real
device; the failure cases (a backend restart during Reset, a rollback to
before PATCH, two requests reordered on the network) can't be staged from
the phone.

## Review fixes (re-check)

The re-check of the final round confirmed two findings: one major, one
minor. The task named a fixer's WIP commit at HEAD; there was none (HEAD
was the final-round commit, with a clean tree), so this round starts from
it. Both are fixed; none was skipped. No server change.

- **An unsave, unhide or cleared vote was dropped once a given-up write's
  adds were forgotten** (major). `forServer` drops a remove (or a cleared
  vote) of an id that is neither in base nor in `sentAdds`, the ids added
  by writes the server may hold. `adopted()` cleared all of `sentAdds`
  whenever an answer about the key was adopted and nothing was on the
  way. A write given up on (rule 4) can land after that answer was read,
  though. Save 5, open the story before the PATCH answers, and come back.
  If the unsave is made before the full GET answers, and the GET is read
  before the suspended request lands, the unsave was dropped, never sent,
  and the next poll showed the story saved again on every device. The same
  happened when the unsave waited behind another save's PATCH whose
  answer came first, for hides and votes too, and after the 30 s deadline
  with no hide at all. The same clearing in `unfold()` forgot the adds of
  the later keepalive request when an earlier request's row became base.
  Now each id in `sentAdds` keeps the number of the request that sent it,
  and `adopted(key, at)` forgets only those sent by request `at` or
  before it (a GET's number, or the request whose answer set base). The
  ids a write given up on adds go into `lateAdds`, which `adopted` never
  clears (only Reset, for the keys it deletes), and `forServer` keeps any
  remove of an id in either set. A remove kept when it wasn't needed costs
  nothing.
- **A save on the way in an ordinary request when the page hid, with
  nothing queued, was neither carried with keepalive nor sent again**
  (minor). `leave()` went only through queued keys. Save a story, and
  open it after the 1 s batch sent the PATCH but before the answer: iOS
  cuts that fetch off, and on the return `resume()` gives the request up
  before its failure (a network error or a 503) is delivered, so
  `settle()` ignored it. The full GET then showed the story unsaved, and
  nothing sent it again. Now `leave()` treats a set key whose writes on
  the way include an ordinary (not `keepalive`) request like a queued key:
  its change goes again at once with `keepalive`, and the earlier request
  is kept as stale (`by`, `carries`), so the existing stale and `unfold`
  handling covers both answers. A key whose requests on the way were all
  sent with `keepalive` (the second leave flush, `pagehide` right after
  `visibilitychange`) isn't sent a third time. `Flight` records
  `keepalive`. This widens the fold window under Deviations to this case
  (one extra request on such a hide, one round trip of exposure). It also
  covers the reload after Reset: a kept save or hide still on the way in
  an ordinary request (a retry sent after Reset's wait) when the page
  unloads goes again with `keepalive` (the Reset fuzz's 8 other losses on
  the final round were this).
  With the copy in place, a second leave flush folded the copy's change in
  again even when the earlier request's OK answer had shown it landed (in
  the fuzz with changes made while hidden and failures on, resends of a
  change known to have landed went from 3 seeds on the final round to 12
  with the copy alone). The leave fold now leaves out what a carried
  request's OK answer showed landed, as `unfold` does: 0 such seeds with
  failures on, and 2 against the final round's 6 with none (in both, an
  earlier request given up at its deadline while hidden, whose OK answer
  rule 4 ignores).
- What stays a rule-4 cost, under Deviations: a write given up on whose
  request fails only later (past its 30 s deadline, or a desktop tab whose
  request and keepalive copy both fail after the return). The reviewer's
  fallback (record the lone-request case as a rule-4 cost) wasn't needed
  for the iOS case.

### Tests added or changed (re-check)

`prefSync.test.ts`, now 68 tests (165 unit tests in all).

- New, finding 1: a save, a hide and a vote given up on at the deadline,
  landing after the full GET was read, with the Undo made before that GET
  answered (the Undo goes, and the next full GET shows it); a save given
  up on at the return, the same; the unsave queued behind a later save's
  PATCH whose answer comes before the given-up request lands; a leave
  flush with the page in front (`beforeunload` that doesn't unload), the
  carrying request landing with its answer lost, and an unsave of what it
  added made after the earlier request's row became base.
- New, finding 2: a lone ordinary request on the way when the page hides,
  for ids and votes: the change goes again with `keepalive`, once (a
  second `leave()` sends nothing), and the first request failing after the
  return with a network error or a 503, or answering OK, ends with the
  change stored and shown, nothing more sent and no toast. A second leave
  flush doesn't fold in again what an earlier request's OK answer showed
  landed, so another device's unsave made since stands.
- Changed: the two `discardPending` tests now expect the reload's leave
  flush to send a kept save still on the way again with `keepalive` (and
  still nothing for a key Reset deletes); "leaving twice" expects the
  second keepalive request to carry 6 and 7, not 5 again (5's request
  answered OK).
- Flow `flow-prefs-hide-while-sending` (new, README row): the save's
  batch PATCH is held when the page hides; the hide sends it again with
  `keepalive`, which is stored and answered; the held request is aborted
  only after the return. Two PATCHes in all, the save stays stored and
  shown, no toast.

### Verification (re-check)

- `npm run lint`: 0 errors, 5 warnings (as before). `npm run build`
  clean. Unit tests 165 of 165 (68 in `prefSync.test.ts`). `git diff
  --exit-code` on `package.json` and the lockfile: clean. The
  `node_modules` symlink was made for the loop and removed before the
  commit.
- Full strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173):
  0 failures, 0 warnings, 870 s (other units were running on the
  container).
- The PR2b flows (`flow-prefs-resync`, `flow-visibility-flush`,
  `flow-prefs-retry`, `flow-prefs-405`, `flow-prefs-fail-save-keeps-list`,
  `flow-prefs-return-before-answer`, `flow-prefs-landed-unread`,
  `flow-prefs-hide-while-sending`, `flow-reset-keeps-saves`,
  `flow-h1-pref-store`, `flow-h1-keepalive`, `flow-reload-on-new-build`,
  `flow-tab-stays-on-star`, `flow-unsave-last-saved`): 3 runs in a row,
  14 of 14 each, 0 failures and 0 warnings (141, 135 and 132 s).
- Backend `test_preferences_delta.py` and `test_preferences_anonymous.py`
  (`popping_test_f`, they pin `prefSync.ts`): 89 passed. The backend is
  unchanged.

### Mutation checks (re-check)

Unit mutations ran on a scratch copy of `prefSync.ts` with this test
file. The flow mutation was built from a scratch copy of `frontend/` into
the worktree's `dist`, served on 4173 and run alone; `dist` was rebuilt
clean after.

| Test | Broken line | Fails on |
|---|---|---|
| `prefSync.test.ts` | all of this round's engine changes reverted (the final round's `prefSync.ts`) | the 11 new tests, the two changed `discardPending` tests, "leaving twice" |
| `prefSync.test.ts` | `forServer` ignores `lateAdds` | the three deadline tests, "at the return", "behind a later save's PATCH" |
| `prefSync.test.ts` | `toLimbo` doesn't fill `lateAdds` | the same five |
| `prefSync.test.ts` | `adopted` clears `sentAdds` when nothing is on the way (the old rule) | "a leave flush in front ... answer is lost" |
| `prefSync.test.ts` | `adopted` clears all of `sentAdds` | the same |
| `prefSync.test.ts` | `leave()` doesn't carry a lone ordinary request | the four lone-request tests, the two `discardPending` tests, "leaving twice: the second leave flush doesn't send again ..." |
| `prefSync.test.ts` | it carries one sent with `keepalive` too | the four lone-request tests ("not a third time"), "before-flush collectors", the `discardPending` cap test |
| `prefSync.test.ts` | the leave fold keeps what a carried request's OK answer showed landed | "leaving twice", "leaving twice: the second leave flush doesn't send again ..." |
| `flow-prefs-hide-while-sending` | `leave()` doesn't carry a lone ordinary request | PREFS-HIDE-SENDING: hiding the page did not send the save on its way again |

One mutation survives and is equivalent: the empty change `leave()` queues
for a vote key as `{add: [], remove: []}` instead of `{set: {}}`
(`foldDelta` folds either onto the votes the same way).

The re-check's fuzz (`fuzz.ts`: a single device, random answers, failures,
lost answers, hides, returns, polls and deadlines; requests land in send
order), run on the final round and on this round, seeds 1 to N:

| Run | Final round | Re-check |
|---|---|---|
| 1 key, no failures (2000) | 9 seeds lose an op (all a dropped remove) | 0 |
| 1 key, failures (2000) | 2 dropped removes, 47 given up then failed | 0 dropped, 41 given up then failed |
| 3 keys, no failures (1500) | 15 dropped removes | 0 |
| 3 keys, failures (1500) | 6 dropped removes, 157 given up then failed | 0 dropped, 144 given up then failed |
| Reset, then the reload (1500) | 6 dropped removes, 8 other losses, 144 given up then failed | 0 dropped, 0 other, 124 given up then failed |
| 3 keys, changes while hidden, failures (1500) | 6 dropped removes, 3 known-landed resends, 203 given up then failed | 0 dropped, 0 resends, 181 given up then failed |
| 3 keys, changes while hidden, no failures (1500) | 18 dropped removes, 6 known-landed resends | 0 dropped, 2 resends |
| no rule-4 cost (given-up requests never fail), 5000 seeds over the three shapes | 30 dropped removes, 3 known-landed resends | 0 |

"Given up then failed" is the rule-4 cost under Deviations; with that
cost taken out (the last row) nothing is lost or resurrected.

Device checks: unchanged. The #40 line "save a story on the phone and
switch to another app straight away" covers leaving inside the 1 s batch.
Leaving within the round trip after it, or an unsave made in the second
before the return's GET answers while a given-up request lands, can't be
staged reliably from the phone; the unit tests and
`flow-prefs-hide-while-sending` cover them.

## Deviations from the spec

- The sync rules live in `lib/prefSync.ts` as an engine
  (`createPrefSync`) with injected effects, not in `preferences.tsx`: the
  rules need a queue, timers and requests, and only injected effects let
  the spec's unit tests run without React. `preferences.tsx` wires it to
  React and the DOM.
- Rule 1 covers PUT keys too, so two whole values can't land out of
  order. Leaving the page is the one exception: a busy key's queued change
  goes at once, folded with the change still on the way, because the page
  may never come back to send it after the answer; holding it back would
  lose it outright when the page doesn't come back. The earlier request is
  kept as stale (review fix); its answer sets nothing and sends nothing.
  Two windows stay open, both needing the two requests for one key on the
  way together at the moment the page hides:
  - The earlier request is overtaken: the keepalive request lands first
    and the earlier one after it, so what the keepalive request reversed
    of it (an Undo, or a changed vote, made within about a second before
    the hide) is undone on the server, and the next GET shows it (the
    story comes back saved or hidden). Neither row's value can show which
    of the two landed last (each shows its own change), the answers can
    arrive in either order, and `updated_at` is the transaction's start,
    not the lock order. The second commit sent the reversal again when the
    keepalive request's answer came first; that was a guess, and when it
    was wrong it undid another device's change made in between, so the
    final round removed it. Covering this properly needs the server to say
    which write took the row last (a per-row version bumped under the row
    lock); that is a server change for its own unit.
  - The fold: the keepalive request carries the earlier request's change
    again, so another device's change to the same story that lands
    between the two (about one round trip) is undone. Not carrying it
    would lose the change whenever the earlier request fails as the page
    goes. (The two-device fuzz flags this one, seed 1363.) Since the
    re-check this window also opens with nothing queued: an ordinary
    request still on the way when the page hides (a story opened within
    one round trip of the 1 s batch) is sent again with `keepalive`. That
    costs one extra request on such a hide, and the same one-round-trip
    window; without it, iOS cutting the first request off lost the save
    whenever its failure came after the return.
- Rule 2 also retries 408 and 429, which are transient. After the third
  retry the change stays queued and on screen (one toast) instead of
  waiting only for a return: a later batch for the key, the reload flush
  and the leave flush send it too.
- Rule 4 follows the spec since the review fixes: a write past its
  deadline, or unanswered when the page comes back, is given up on, not
  sent again, and the next full GET is adopted for the key. (The first
  commit sent it again and called that harmless; it isn't, because it
  undoes a change another device made meanwhile.) Two additions: the
  change stays on screen until that answer, so a save doesn't flicker off
  and on at the return; and a write sent while the page was hidden is
  given up on at the return too, since it is as likely to have been
  suspended. The cost, which the spec accepts: a keepalive that iOS never
  delivered is lost. So is a write given up on whose request fails only
  later (re-check): its late failure is ignored like any late answer, and
  the change isn't sent again. The common case, an ordinary request on
  the way when a story opens, now has a `keepalive` copy sent by the hide
  (see the fold above). What is left: a request past its 30 s deadline
  that then fails (a backend hang ending in a 5xx), and a desktop tab
  hidden during a backend restart whose request and keepalive copy both
  fail only after the return. In the re-check's single-device fuzz (2000
  seeds, failures on), 41 seeds end with an op lost this way, against 47
  on the final round; all of them are a request given up on that then
  failed.
- Rule 9 (final round): `discardPending` clears only the keys Reset
  deletes, not the whole queue the spec names. The rest (saves, hides,
  votes, saved views, which Reset keeps) is sent before the DELETEs and
  the reload: clearing it lost any of them still queued, on a retry or on
  the way when Reset was tapped.
- Rule 2 and PR1r (final round): a write answered with 404 or 405 is
  still retried and kept on screen, but no longer holds back PR1r's
  reload (`flushPending` answers true for it). After a revert to before
  PATCH, a resumed page would otherwise never reload onto the reverted
  build by itself. The reload's leave flush sends the write once more;
  then it is lost, as the reverted build can't take it.
- Rule 5: a queued (unsent) write doesn't make a GET's row ignored; the
  write is re-applied on top of it. The row is newer than anything the
  queue has sent, so it is the right base. Writes on the way and writes
  settled after the GET was sent are skipped as the spec says, for PUT
  keys too.
- Rule 7 adds a 1 s window: a poll within 1 s of a GET being sent is
  answered by it (the mount's full GET and the first refresh's poll start
  together). Hiding aborts the GET on the way, and a GET gives up after
  20 s, so a request iOS suspended can't block the next one.
- Rule 8: `since` is the watermark string read to the millisecond minus
  60 s. Only comparisons use microseconds; the overlap makes the lost
  microseconds irrelevant.
- The localStorage seed stays, but writes only after the first successful
  full GET: set keys by `addIds` (only ids the server lacks), other keys
  only where the server has no row (the spec's "if any seeding stays").
- The provider gains `latest()` (the state as of now) for handlers that
  Column's memo keeps; it replaces App's set-key refs mirrored in effects.
- The caps moved from `preferences.tsx` to `prefSync.ts`; the backend pin
  reads them there.
- `api.patchPreference` leaves empty lists out of the body. The server
  takes a missing list as empty, and answers 422 when `add` or `remove`
  appears at all on `voted_entries`.
- Extra: smoke rule C17 (the assertion that no path PUTs a set key),
  `flow-prefs-fail-save-keeps-list`, the mock's body checks, and H1's
  `answered` flag.

## Data compatibility and rollback

- Stored rows keep their shapes (id lists, the votes map). This build
  needs PR2a's PATCH and `since` (merged in #28).
- Old bundles still PUT whole lists until PR2c. This build's resync adopts
  what they write, which is why PR2c refuses those PUTs once both devices
  run this build (the first new Device checks line).
- `read_entries:<column>` now holds up to 1000 ids (App sliced to 200). An
  old bundle's next whole-list PUT of that key cuts it back to its own 200.
- Revert: safe while PR2c isn't merged; PR2a's routes are then unused and
  the old bundle PUTs again. After PR2c merges, revert PR2c first (it
  would refuse the reverted bundle's PUTs with 409). After PR7a merges,
  PR2b no longer reverts alone.
- After a PR2a and PR2b revert, a resumed page still on this bundle gets
  405 for its PATCHes. They stay queued and shown, and on the next return
  after 10 min away PR1r reloads onto the reverted build (a 405 doesn't
  hold the reload back); the reload's leave flush sends them once more,
  and then they are lost.

## Open items for later units

- PR2c: refuse set-key PUTs once both devices show this build; the mock's
  PUT answers 409 for set keys then (C17 reports them today).
- PR3b: new seen marks go to `seen:<category>` through `addIds(key, ids,
  keep)` (the ids on screen as `keep`), its collectors drain through
  `registerBeforeFlush`, Mark all's Undo is `removeIds` of the added ids,
  and Reset adds `seen:*` to its deletions. `decodeState` ignores `seen:*`
  until then. PR3b deletes `toggleEntryRead` and turns `unmarkColumnRead`
  into Mark all's Undo.
- PR5b: `muted_sources` joins the set keys in `isSetKey` / `capFor`, the
  mock's `SET_KEY_RE` and `setKeyCap`, and the server's `SET_KEY_CAPS`.
- PR7a (`category_layout`) and PR8a (`custom_tabs`) are PUT keys, covered
  by rules 1 and 5 as `column_prefs:*` is.
- For the owner, a possible plan change: under rule 4 a keepalive that
  never landed is lost, and so is a write given up on whose request fails
  later (re-check, under Deviations). Resending either safely needs the
  server to say whether it landed, for example a client write id that the
  PATCH stores and the row echoes. A blind resend undoes other devices'
  changes.
- For the owner, next to that: the leave flush's overtaken case (under
  Deviations: an Undo made just before the hide, overtaken by its own
  keepalive request) needs the server to say which of two writes took the
  row last, for example a per-row version bumped under the row lock and
  echoed in the answer. One additive server change could serve both items.
- Not in this unit: a save made offline while staying in the app goes out
  only with a later write's batch, a hide and return, or a flush. Nothing
  listens for `online`. Sending on `online` would be new behaviour for its
  own unit or the owner's approval; the device check leaves the app and
  comes back instead.
- Harness (H1's `smoke.cjs`): one loop run in 18 crashed with no summary
  (see Verification of the fixes). If it shows up again, keep the whole
  log; if the stack names a route callback, call
  `context.unrouteAll({ behavior: 'ignoreErrors' })` before
  `context.close()`.
- ARCHITECTURE.md is unchanged: this unit adds no server process state.
