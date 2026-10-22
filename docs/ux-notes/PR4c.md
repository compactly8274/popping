# PR4c: Saved by id (frontend track)

Stacked on PR3b (`claude/ux-pr3b`, #45, in its re-check), built as if it
were merged. PR4b (#31, merged) provides the by-ids projection. PR3c
(seen on scroll) was built at the same time in another worktree and
landed between PR3b and this unit; nothing here touches seen-on-scroll
code, and the App.tsx changes stay inside Saved, the hide Undo and the
keep set. Since then the unit was rebased onto main with both merged
("Rebased onto main", at the end).

## What changed

Saved now lists every save, newest save first, whatever the tabs loaded
(D5). A save the tabs don't hold (an old story, or one saved on another
device) is fetched by id, with its picture, instead of being missing.
Undo on a hide puts the story back everywhere, even after a poll dropped
it.

- `frontend/src/lib/saved.ts` (new, pure, no imports):
  - `savedStories(starred, find, isRemoved)`: the saves to draw, newest
    save first. `starred_entries` holds the saves in the order they were
    made (a save appends its id, on the client in `applyIdDelta` and on
    the server's PATCH), so Saved walks it from the end. A save with no
    copy yet is left out until its fetch lands; `isRemoved` keeps "hide
    wins over saved".
  - `savedToFetch(starred, skip)`: the ids to fetch, newest save first,
    so the first chunk of 200 is the top of Saved.
  - `placeOf(list, i, isHidden)` and `putBack(list, items, kept)` (re-check
    round 3; they replace `reinsert` and `reinsertAll`): a hidden story's
    place (the stories next to it, up to the first one not hidden, and
    its index), and the list with stories put back at their places, or
    the same list when a poll already brought them back.
- `frontend/src/lib/feedOrder.ts`: `keepSet(...groups)`, the keep set a
  `seen:` key is pruned against, built from stories or bare ids; and
  (re-check round 1) `keepIdsFor(held)`, App's keep set from what it
  holds (`Held`: `entries`, `forYou`, `starred`, `savedById`, all
  required).
- `frontend/src/App.tsx`:
  - **Saved** (replaces `visibleStarred`'s `entries.filter`): `savedById`
    (state) is the by-id cache. An effect fetches, with
    `api.entriesByIds` (200 per call, chunked), only the saves that the
    tabs (`entries`, `forYou`) and the cache don't hold, that aren't
    hidden, that the server didn't already fail to return and that
    aren't already being fetched. It runs once the preferences have
    answered and the first refresh is done, so a save the tabs bring is
    never fetched too, and again whenever the saves or the tabs' stories
    change: a save made on another device arrives through PR2b's resync
    (`starredEntries` changes) and is fetched; a save a poll stops
    returning is fetched then. Ids the server no longer returns go into
    `savedGoneRef` (the negative cache), so no poll asks for them again.
    An unsave drops the id from the negative cache but keeps the story
    in the by-id cache (review fix), so the unsave's Undo draws it again
    at once, with no request and offline too; a response that lands
    after an unsave is kept the same way. Saved draws only saved ids, so
    a cached unsaved story isn't drawn. The cache is bounded lazily:
    once it holds more than `MAX_STARRED` (1000, the saves' own cap)
    stories, the unsaved ones leave it.
  - Each save is drawn from the tabs' copy (the freshest), else the
    cache, else the copy drawn last time, so a save a poll just dropped
    doesn't blink out while its fetch is out. Hidden saves stay out
    (hide wins), and so do stories of a feed no longer in `sources` (the
    server deletes a feed's stories with it, and the tabs already drop
    them). While the same stories are drawn the same array comes back,
    so a poll that changed nothing in Saved doesn't redraw it.
  - **Hide Undo:** every hide (`hideEntries`; `hideEntry` and
    `toggleHideEntry` for one story, Settings › Voted's "Hide all
    downvoted" for many) keeps each story and its place in `entries`
    and in `forYou` (`keepHiddenStories`, `hiddenStoriesRef`: the latest
    `KEPT_HIDDEN` = 100, and never fewer than the hide's own, so a bulk
    hide keeps all of its stories and pushes out none of the 100 before
    it). Every unhide (`restoreHiddenEntries`; `restoreHiddenEntry`,
    `toggleHideEntry`'s unhide, Settings' Hidden list, both Restore and,
    since re-check round 2, Restore all, and the bulk hide's Undo) puts
    them back there with `putBack` (re-check round 3: next to the
    stories that were next to it, its index only when none of them is
    there), except a story whose feed is gone since the hide (re-check
    round 2). While For You's last request failed, the Undo leaves For
    You's empty list alone and the story is laid over its next answer
    (re-check round 3).
    For two minutes (`SOURCE_CHANGE_TTL_MS`, the same window
    `sourceChangesRef` uses for feeds) `refresh` lays a put-back story
    over a poll that doesn't return it yet (the server answered before
    the Undo's change, sent in a 1 s batch, reached it), until a poll
    returns it, it is hidden again or its feed is gone, here or on
    another device (`restoredRef`). A failed For You request gets no
    lay-over: For You stays empty until it answers, and the story is
    laid over that answer (re-check round 2). A story only Saved holds
    needs nothing kept: the by-id cache still has it.
  - **Keep set:** `keepIds` is `keepIdsFor({ entries, forYou, starred:
    starredEntries, savedById })` (re-check round 1; it was
    `keepSet(entries, forYou, starredEntries, savedById.keys())`).
  - `byNewest` is no longer imported (Saved isn't sorted by date).
- `frontend/src/lib/sorting.ts`: the header comment says Saved lists
  saves newest save first.
- `frontend/src/components/Column.tsx` (re-check round 3): Saved's
  stories go through the 30-card batches however they arrive. A tab
  draws an arrival at once once every card is drawn, to keep its marker
  and Earlier in place; Saved has nothing under its cards, and its saves
  can land by id by the hundred.
- `frontend/scripts/iphone-audit/capture.cjs`:
  - `GET /api/entries/by-ids` mirrors the real route: at most 200 valid
    ids, in the order asked, ids with no row or whose feed is gone
    dropped, and each row the card projection (`_CARD_COLUMNS`, never
    `meta`) plus `source_name` (`byIdRow`).
  - `GET /api/entries` (without `q`) and `GET /api/foryou` leave out the
    store's `hidden_entries`, as the server has since PR4b.
- `frontend/scripts/iphone-audit/flows.cjs`: `flow-saved-by-id` and
  `flow-hide-undo-after-poll` (new), `flow-saved-order` (rewritten), and
  a `phoneIds(page)` helper; with the review fixes also
  `flow-saved-drops-hidden-and-deleted`, `flow-hide-all-downvoted-undo`
  and a `keepOutOfTabs(page, ids)` helper; with re-check round 1 also
  `flow-saved-keeps-a-dropped-save` and the helpers `setKeptOut`,
  `recordByIds`, `holdByIds`, `byIdsAsked`, `pollDone` and `pollHas`.
- `frontend/scripts/iphone-audit/README.md`: the flows and the
  mock's by-ids and hidden rules.

## Deviations from the spec

- **Saved's order.** PR1c's `flow-saved-order` pinned Saved by
  `byNewest` (published, else fetched). The spec says "newest save
  first", so Saved now follows the save order and the flow is rewritten
  to pin that (saved as 103, 105, 111, 101 it reads 101, 111, 105, 103;
  by date it would read 101, 103, 105, 111). The spec doesn't name this
  flow; it is named here as the rules ask.
- **Hide Undo.** The spec says the Undo "re-inserts the cached Entry".
  PR3b's snapshot cache already redraws a hidden story in its tab after
  the Undo, but only in a tab that is drawn and only until its next
  snapshot; For You, the other tabs the story belongs to and Saved build
  from `entries` / `forYou`. So the hide keeps its own copy and the
  Undo puts it back into those lists. Beyond the spec, a put-back story
  is also laid over polls for two minutes, as feeds are: a poll sent
  between the Undo and its PATCH (a 1 s batch) would otherwise drop the
  story again until the next poll. Since re-check round 3 its place is
  kept as the ids of the stories next to it, not an index (below).
- **Column.tsx** (re-check round 3). PR3a's batching draws a tab's
  arrivals at once once every card is drawn. Saved is now left out of
  that rule, since this unit's by-id fetch can bring hundreds of saves
  in one update; the tabs are unchanged.
- **Keep set.** `starredEntries` has been in the keep set since PR3b.
  Since the review fixes the by-id cache also holds recently unsaved
  stories (bounded at 1000), so the cache in the keep set now also keeps
  those stories' seen marks from being pruned first while they may
  still come back through an Undo.
- **Mock fidelity.** The spec asks only for the by-ids projection. The
  mock now also leaves hidden stories out of `/api/entries` and
  `/api/foryou`, which the server has done since PR4b; without it no
  flow could reproduce the poll that drops a hidden story.
- **New pure lib.** `lib/saved.ts` isn't in the Rules' list of new pure
  libs; it follows the same rules (no imports, runs under strip-types),
  and `scripts/unit/saved.test.ts` tests it.
- `ARCHITECTURE.md` is about the backend's process-local state; the
  by-id cache is per page, so it isn't listed there.

## Tests

- **Unit** (`node --experimental-strip-types --test`):
  - `feedOrder.test.ts`: `keepSet` (stories and bare ids, each once) and
    the spec's keep-set case. As first written, the case called
    `keepSet(tabs, [], cache)` without the saves, a keep set App never
    builds, and its claim (an old save held only by the cache would lose
    its mark) was wrong: App also passes `starredEntries`, which holds
    every save. Re-check round 1 replaced it with `keepIdsFor` cases
    (below, "Review fixes (re-check round 1)").
  - `saved.test.ts` (new): newest save first; a save with no copy left
    out; hide wins; an id stored twice drawn once; `savedToFetch` order
    and skip; `reinsert` at its place, clamped (a negative index since
    re-check round 1), the input unchanged, and the same list when the
    story is already there. Re-check round 3 replaced the `reinsert`
    cases with `placeOf` and `putBack` cases (below).
- **Flows:**
  - `flow-saved-by-id`: 503 and 402 never reach a tab (the page's fetch
    drops them). Saved lists 105, 503, 103 with 503's thumbnail loaded,
    from one by-ids request for `999999,503` (the saves no tab holds,
    newest first; 999999 has no row). A save from another device (seeded
    in the store, then a return to the front) shows at the top after one
    request for `402` alone; an unsave takes 503 out; after two more
    polls, 999999 was never asked for again.
  - `flow-hide-undo-after-poll`: hide 101 on For You, a poll without it,
    Undo, a poll sent at once (before the Undo's 1 s batch, so the
    server still leaves 101 out), then News: 101 is there, and the
    store loses the hide. (Rewritten in re-check round 1: below.)
  - `flow-saved-order`: newest save first (above).

## Verification

- `npm run lint`: 0 errors (the base's 4 warnings, none in files this
  unit touches). `npm run build` passes. Unit tests: 202 of 202 (PR3b's
  193 plus 9).
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest, the tests that pin frontend source plus
  `test_entries_by_ids_projection.py` (11 files): 214 passed.
- Full backend pytest on `popping_test`: 1712 passed and 13 failed. The
  13 are exactly the DNS-only baseline list
  (`/root/popping-env/baseline.txt`).
- Full strict smoke (`--csp --strict`, CI fonts, port 4175) on the final
  build: 173 states and flows, 0 failures, 0 warnings (457 s, with the
  other units' smoke running on the same container). An earlier full run
  on the build before the last change (Saved's array reused while
  unchanged) had one failure in `flow-fl-rename-keeps-filter-and-view`
  ("the field shows \"Slickdeals Renamed\"": the value it expected, read
  a moment later), a feed-editor flow this unit doesn't touch; it passed
  on its own straight after, and in the final full run.
- Screenshot looked at (not committed): `390-flow-saved-by-id-saved`.
  Saved reads 105, 503, 103; 503, which no tab holds, is a full card with
  its thumbnail, feed name and time.

### Mutation checks (test → broken line → failing rule)

Each was a scratch edit, then a rebuild, `--only` on the flows (or the
unit file), a restore from a backup copy and a byte compare (`cmp`).

| Test | Broken line | Fails on |
|---|---|---|
| `flow-saved-by-id` | the by-id effect never fetches (`want.length >= 0`) | SAVED-BY-ID: "Saved shows 105,103, expected 105,503,103" |
| `flow-saved-by-id` | the negative cache never filled (`savedGoneRef.current.add` dropped) | SAVED-BY-ID x3: by-ids asked for `["999999,503","999999"]`, then 999999 again on every poll |
| `flow-saved-by-id` | the by-id cache not counted as held (`savedById.has(id)` → `false`) | SAVED-BY-ID x3: 503 asked for again on every render |
| `flow-saved-order`, `flow-saved-by-id` | `savedStories` walks the saves oldest first | SAVED-ORDER: "103,105,111,101, expected 101,111,105,103"; SAVED-BY-ID: "103,503,105" |
| `flow-hide-undo-after-poll` | `restoreHiddenEntry` doesn't put the story back | HIDE-UNDO: "News shows 102,…,112, without 101" (For You still drew it from its snapshot, as PR3b intends) |
| `flow-hide-undo-after-poll` | `refresh` doesn't lay the put-back story over the poll | HIDE-UNDO: "News shows 102,…,112, without 101" |
| `feedOrder.test.ts` | `keepSet` reads only its first two groups | both `keepSet` cases |
| `saved.test.ts` | `savedStories` ignores `isRemoved` | "hide wins over saved" |

The keep-set wiring in App (`savedById.keys()` passed to `keepSet`) had
no failing test when removed: no flow fills a `seen:` key past its cap
of 1000 marks, and the unit case didn't model App's keep set. Since
re-check round 1, App calls `keepIdsFor`, whose type requires each
field (tsc fails if one is left out), and `feedOrder.test.ts` pins each
of its groups. What App passes was still unchecked: an empty Map or `[]`
compiled and passed every test. Since re-check round 2,
`flow-seen-prune-keeps-saved` fills `seen:news` to its cap and checks
that App's keep set holds a save and a story only the cache holds.

## Review fixes (second commit, "PR4c: review fixes")

Review step: /code-review medium. Five findings, four distinct (two
reported the same bulk-hide gap); all fixed, none skipped.

1. **An unsave dropped the story from the by-id cache**, so its Undo
   had to fetch it again and drew nothing offline. The by-id effect no
   longer evicts on an unsave: Saved draws only saved ids
   (`savedStories` walks `starredEntries`), so a cached unsaved story is
   never drawn. The cache is bounded lazily instead: past `MAX_STARRED`
   (1000) stories the unsaved ones go. A by-id response that lands after
   an unsave is kept the same way (it was dropped before), and the
   effect no longer reads `latestPrefs`.
2. **"Hide all downvoted" (Settings › Voted) didn't keep or put back
   its stories** (two findings). `keepHiddenStory` /
   `putBackHiddenStory` became `keepHiddenStories` /
   `putBackHiddenStories` (lists), behind `hideEntries` /
   `restoreHiddenEntries`; `hideEntry`, `restoreHiddenEntry` and
   `toggleHideEntry` call them with one id, and the bulk hide and its
   Undo with `added`. The keep cap is `KEPT_HIDDEN` (100) plus the
   hide's own count, so a bulk hide keeps all of its stories and pushes
   out none of the 100 kept before it (the toast is a single slot, so
   only Settings' Hidden list could still restore those). Stories put
   back together go in with the new pure `reinsertAll` (lowest index
   first, so each lands at its own place); `refresh`'s lay-over of
   put-back stories uses it too.
3. **The first device check failed falsely when a save is also
   hidden**: Settings › Saved lists hidden saves, the Saved tab doesn't
   (hide wins). The line in `docs/DEVICE_CHECKS.md` (and below) now
   excepts "any you also hid".
4. **No test guarded App's Saved filter** (hide wins over saved; a
   deleted feed's saves leave). New flow
   `flow-saved-drops-hidden-and-deleted`.

### Tests added or changed

- `flow-saved-by-id`: after the unsave of 503 (held only by the by-id
  cache), by-ids is made to fail (`route.abort`), Undo is tapped, and
  Saved must show 503 again with no new by-ids request; then 503 is
  unsaved again and the old poll checks run. The Undo's place is
  either the top or 503's old place (whether the unsave was sent before
  the Undo; Risks), so the flow accepts both. (Re-check round 2 holds
  the clock and checks each case on its own.)
- `flow-saved-drops-hidden-and-deleted` (new): saves 103, 105 and 503
  (503 kept out of the tabs by the page's fetch, as in
  `flow-saved-by-id`; the helper is now `keepOutOfTabs`). Hiding 105
  from its card in Saved takes it out before and after a poll; deleting
  feed 14 in the store (another device) takes 503 out after the next
  poll.
- `flow-hide-all-downvoted-undo` (new): Settings › Voted with 101 and
  103 downvoted, Hide all downvoted, a poll without them, Undo, a poll
  before the Undo's batch, then News shows 101 and 103 and the store
  loses both hides.
- `saved.test.ts`: `reinsertAll` (any input order, one already back,
  nothing to do, input unchanged).

### Mutation checks (review fixes)

| Test | Broken line | Fails on |
|---|---|---|
| `flow-saved-by-id` | the old eviction (`savedById.size > MAX_STARRED &&` dropped) | SAVED-BY-ID: "offline, after the unsave's Undo Saved shows 402,105,103"; "the unsave's Undo asked by-ids again: [\"999999,503\",\"402\",\"503\"]" |
| `flow-hide-all-downvoted-undo` | the bulk Undo only `removeIds` (no put-back) | HIDE-ALL-UNDO: "News shows 102,104,…,112, without 101 and 103" |
| `flow-hide-all-downvoted-undo` | the bulk hide only `addIds` (nothing kept) | the same |
| `flow-saved-drops-hidden-and-deleted` | `visibleStarred`'s `hiddenSet.has(e.id)` → `false` | SAVED-DROPS: "after hiding 105 Saved shows 503,105,103", and the same after the poll |
| `flow-saved-drops-hidden-and-deleted` | the `sourcesById` clause dropped | SAVED-DROPS: "after its feed was deleted Saved shows 503,103" |
| `saved.test.ts` | `reinsertAll` without its sort | "reinsertAll: stories put back together each land at their own place" |

Each was a scratch edit, a rebuild, `--only` on the flow (or the unit
file), then a restore from a copy and `cmp`. The `+ added` in the keep
cap has no failing test: no flow hides more than 100 stories.

### Verification (review fixes)

- `npm run lint`: 0 errors (the base's 4 warnings). `npm run build`
  passes. Unit tests: 203 of 203 (202 plus `reinsertAll`).
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest, the 10 files that read frontend source plus
  `test_entries_by_ids_projection.py`: 187 passed and 2 failed, both
  in the DNS-only baseline list
  (`test_reserved_category_names.py`, `/root/popping-env/baseline.txt`).
  No backend file changed.
- The five Saved and hide flows (`flow-saved-by-id`,
  `flow-saved-drops-hidden-and-deleted`, `flow-hide-all-downvoted-undo`,
  `flow-hide-undo-after-poll`, `flow-saved-order`): 0 failures, and the
  four timing ones looped 5 times, 0 failures each.
- Full strict smoke (`--csp --strict`, CI fonts) on the final build: 175
  states and flows, 0 failures, 0 warnings (539 s). It ran on port 4176:
  port 4175 was taken by a review agent's leftover `vite preview` of a
  scratch copy (its mutated build), which this session didn't start and
  left running.

## Review fixes (re-check round 1, "PR4c: re-check fixes, round 1")

Six findings, all about the tests; all applied (the third in part, as
found). No product behaviour changed: App's only change is the keep
set's call, which builds the same ids.

1. **The hide-Undo flows didn't guard the Undo's own put-back, or any
   For You put-back** (major). Both flows looked at News only after a
   second poll, whose lay-over brings 101 back whether or not the Undo
   did, and For You's check was met by PR3b's snapshot of the drawn
   tab. `flow-hide-undo-after-poll` is rewritten. Right after the Undo
   it opens News (never drawn before) and For You again (left for News,
   so it starts over), so each is built from the list the Undo put 101
   back in. After the second poll it opens both afresh again, so the
   lay-over is checked in each list. Its comment now says what it
   asserts (News and For You, not "everywhere").
2. **The keep-set unit case tested a keep set App never builds**
   (minor). App's keep-set inputs moved into the pure
   `keepIdsFor(held)` (`lib/feedOrder.ts`). Its `Held` type requires
   all four groups, so App can't leave the cache field out without tsc
   failing. (Re-check round 2 corrects this: tsc checks only that each
   field is passed, not what; `flow-seen-prune-keeps-saved` now checks
   what App passes.) `feedOrder.test.ts` replaces the case with two:
   - each group holds one id no other does, and all of them are kept;
   - a story unsaved but still in the cache (its Undo can draw it
     again) keeps its mark when `seen:news` is pruned, and without the
     cache it goes first. A story still saved is kept by the saves
     either way.
   The old case's wrong claim is corrected under Tests.
3. **`flow-saved-by-id` didn't guard the in-flight dedupe or the
   hidden-save skip** (minor). Reproduced in part. The dedupe (M05)
   already failed the flow in 5 runs of 6, through a race between the
   resync's by-ids request and the poll's stories, so "no flow fails"
   was wrong for it; it still passed 1 run in 6. 402's by-ids answer is
   now held while a second poll lands, so the dedupe is checked every
   time. The hidden-save skip (M04) was confirmed as unguarded:
   `flow-saved-drops-hidden-and-deleted` now records by-ids requests
   and fails if the hidden 105 is asked for after the hide and the poll
   (the flow also checks that the poll left 105 out). The cost of M04
   is one extra request per hidden save no tab holds, once per load, not
   one "on every change".
4. **The Undo flows depended on the 1 s batch and the 5 s toast in real
   time** (minor). Both now install the clock and hold it (`pauseAt`).
   The hide's PATCH, and later the Undo's, go on `runFor(1000)`, so the
   second poll always reaches the mock before the Undo's change, and
   the toast can't close mid-flow. Each poll is `pollDone`: it waits
   until the app has taken the poll in (the phone's refresh button is
   enabled again), not for a fixed pause.
5. **Saved's last-copy fallback and its "gone" exception were untested**
   (minor). New `flow-saved-keeps-a-dropped-save`. A poll stops
   returning 103, which the tabs held until then, and its by-ids answer
   is held: Saved must keep drawing 103. The answer then comes back with
   no row: after the next poll 103 must be out of Saved, and it is not
   asked for again. The lay-over's two-minute expiry is now checked in
   `flow-hide-undo-after-poll` with `runFor(120_000)`. The lines still
   unguarded are listed under the mutation checks below.
6. **`reinsert`'s "clamped" test passed with the clamp removed**
   (minor). `saved.test.ts` adds `reinsert(list, story(9), -1)`, which
   gives `[9, 1, 2, 3]`. The upper bound stays: `slice` already stops at
   the end, so no test can tell it apart, and it states the intent.

Also: the `withStories` comment named `flow-saved-order` as the fetch
wrapper to copy, but this unit took the wrapper out of that flow; it now
names `keepOutOfTabs`.

### Tests added or changed (re-check round 1)

- New helpers in `flows.cjs` (README, Helpers):
  - `setKeptOut(page, ids)` changes `keepOutOfTabs`'s ids mid-flow.
  - `recordByIds(page)` records by-ids requests in `page._byIds` and
    answers each as `page._byIdsAnswer` says (`mock`, `offline`,
    `gone`). It replaces `flow-saved-by-id`'s own route.
  - `holdByIds(page)` holds the by-ids answers until released.
  - `byIdsAsked(page, id)` says whether by-ids was asked for `id`.
  - `pollDone(page)` returns a poll's `/api/entries` and `/api/foryou`
    bodies once the app has taken them in; `pollHas(got, ids)` checks
    them for ids.
- `flow-hide-undo-after-poll` (rewritten, clock held), in order:
  1. Hide 101 on For You; the hide's batch goes on `runFor(1000)`; a
     poll leaves 101 out of both lists.
  2. Undo. News, then For You opened again, show 101.
  3. A poll before the Undo's batch leaves 101 out. News, then For You,
     each opened afresh, show 101.
  4. The Undo's batch goes, and the store loses the hide.
  5. The page's fetch drops 101 (the server no longer returns it). A
     poll inside two minutes keeps it in News. After `runFor(120_000)`,
     a poll takes it out of For You and News.
- `flow-hide-all-downvoted-undo`: the clock is held as above; the polls
  are `pollDone` and check both lists.
- `flow-saved-by-id`: 402's answer is held over a second poll, and 402
  must be asked for once.
- `flow-saved-drops-hidden-and-deleted`: by-ids is never asked for the
  hidden 105, and the poll must leave 105 out.
- `flow-saved-keeps-a-dropped-save` (new): above (finding 5).
- `feedOrder.test.ts`: the two `keepIdsFor` cases (finding 2).
- `saved.test.ts`: the negative index (finding 6).

### Mutation checks (re-check round 1)

Each was a scratch edit in a copy of the frontend, then a `vite build`
and `--only` on the flows (`--csp --strict`, CI fonts). The file was then
restored from a copy and checked with `cmp`. Unit cases ran under
strip-types the same way. Where a count is given, the mutant failed in
every run.

| Test | Broken line | Fails on |
|---|---|---|
| `flow-hide-undo-after-poll` | M22: no put-back into `entries` (`putBackHiddenStories`) | HIDE-UNDO: "right after the Undo, News shows 102,…,112, without 101" (2 of 2) |
| `flow-hide-undo-after-poll` | M23: no put-back into `forYou` | "right after the Undo, For You opened again shows 301,102,201,401,104,303,502, without 101" (2 of 2) |
| `flow-hide-undo-after-poll` | M24: `refresh` doesn't lay the story over `forYou` | "after the Undo and a poll that left it out, For You shows …, without 101" (2 of 2) |
| `flow-hide-undo-after-poll` | M25: M23 and M24 together | both For You checks (2 of 2) |
| `flow-hide-undo-after-poll`, `flow-hide-all-downvoted-undo` | M13: `refresh` doesn't lay the story over `entries` | the News checks after the second poll and inside two minutes; HIDE-ALL-UNDO: "News shows 102,104,…,112, without 101 and 103" |
| `flow-hide-undo-after-poll` | M17: the lay-over never expires (`now - r.at > SOURCE_CHANGE_TTL_MS` dropped) | "two minutes after the Undo, For You still shows 101, which no poll returns", and the same for News (2 of 2) |
| both hide-Undo flows | `restoreHiddenEntries` without `putBackHiddenStories` | all four of HIDE-UNDO's News and For You checks, and HIDE-ALL-UNDO |
| `flow-hide-all-downvoted-undo` | the bulk hide only `addIds`; the bulk Undo only `removeIds` (each alone) | HIDE-ALL-UNDO: "…without 101 and 103" |
| `flow-saved-by-id` | M05: no in-flight dedupe (`savedFetchingRef.current.has(id)` → `false`) | SAVED-BY-ID: "with 402's answer still out, a poll left by-ids asked for [\"999999,503\",\"402\",\"402\",\"402\"]" (3 of 3) |
| `flow-saved-drops-hidden-and-deleted` | M04: hidden saves fetched (`hiddenSet.has(id) ||` → `false ||`) | SAVED-DROPS: "by-ids was asked for the hidden 105: [\"503\",\"105\"]" (2 of 2) |
| `flow-saved-keeps-a-dropped-save` | M11: no last-copy fallback | SAVED-DROPPED: "while 103's by-ids answer was out Saved showed 105, expected 105,103" (2 of 2) |
| `flow-saved-keeps-a-dropped-save` | M10: the "gone" exception ignored | "after by-ids found no row for 103 and a poll, Saved shows 105,103, expected 105" (2 of 2) |
| the rewritten Saved flows | the earlier mutants: the effect never fetches; the negative cache never filled; the cache not counted as held; Saved's hidden filter; the deleted-feed clause; the old eviction | each still fails its flow (and `flow-saved-keeps-a-dropped-save` where it applies) |
| `feedOrder.test.ts` | `keepIdsFor` without any one of its four groups | "the keep set App builds is …" (renamed in round 2: "the keep set is …"; and, without the cache, the prune case) |
| tsc (`npm run build`) | App's `keepIdsFor` call without `savedById` | TS2345: "Property 'savedById' is missing" |
| `saved.test.ts` | `reinsert` with no clamp (`at`), or no lower bound (`Math.min(at, list.length)`) | "reinsert: the story goes back at its place, clamped to the list" |

Still unguarded. Each was run against all seven Saved and hide flows
and had 0 failures:

- **Saved's array reuse** (`visibleStarred` returns the same array while
  the same stories are drawn). It only saves Saved a redraw; nothing on
  screen changes.
- **The lay-over's re-hide check** (`hiddenNow.has(id)` in `refresh`). A
  hide made here already drops the story from `restoredRef`
  (`keepHiddenStories`). After a hide from another device, the story
  would stay in `entries` for up to two minutes, but every tab, For
  You, Saved and badge already leave hidden stories out.
- **`toggleHideEntry`'s unhide calling `restoreHiddenEntry`**. The eye's
  unhide is reached only with the desktop keyboard (`h` twice on a
  selected card): phone tabs, search results and Saved never draw a
  hidden card. Without it, the story is missing from tabs not on screen
  until the next poll.
- As before, the `+ added` in the keep cap. `reinsert`'s upper bound is
  equivalent (above).

### Verification (re-check round 1)

- `npm run lint`: 0 errors (the base's 4 warnings). `npm run build`
  passes. Unit tests: 204 of 204 (one keep-set case became two).
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest, the 10 files that read frontend source plus
  `test_entries_by_ids_projection.py`: 187 passed and 2 failed, both
  in the DNS-only baseline list (`test_reserved_category_names.py`). No
  backend file changed.
- The seven Saved and hide flows (the five above, `flow-saved-order`,
  `flow-saved-always`): 5 loops, 0 failures. With 4 extra CPU burners
  (load average 13): 3 runs of the four timing ones, 0 failures.
- Full strict smoke (`--csp --strict`, CI fonts, port 4175) on the final
  build: 176 states and flows (175 plus `flow-saved-keeps-a-dropped-save`),
  0 failures, 0 warnings (604 s, with other lanes' smoke on the same
  container).

Deviations, risks and device checks: unchanged. This round changed
tests, the keep set's call and these notes, not what the app does.

## Review fixes (re-check round 2, "PR4c: re-check fixes, round 2")

Seven findings; all applied, none skipped. Three change what the app
does (1 to 3), four are about the tests (4 to 7). Each new flow was
first run against the unfixed build (`0ee33df`'s App.tsx): the flows for
1, 2 and 3 failed there with the findings' symptoms, and the others
passed. Findings 4 to 7 are gaps the review showed with mutants that
passed every test; each of those mutants now fails a flow (below).

1. **Settings › Hidden › Restore all didn't put the stories back**
   (major, moderate in effect). It only removed the ids, so a story a
   poll had dropped since the hide stayed out of every tab until the
   first poll after the restore's PATCH, up to 60 s; the per-row
   Restore in the same list put it back at once. `onRestoreAllHidden`
   now calls `restoreHiddenEntries(ids)`, the path every other unhide
   takes. `restoreHiddenEntries`' comment lists every caller. New flow
   `flow-hidden-restore-all-after-poll`, and a device check.
2. **A put-back story of a feed deleted on another device made an
   "Other" tab** (and a For You badge one too high) for up to two
   minutes. Only feeds deleted on this device stopped the lay-over, and
   the Undo didn't check feeds at all. Now:
   - `refresh` keeps laying a story over only while its feed is in the
     poll's feeds less those deleted here (`liveSources`, which
     `setSources` now uses too). This one clause replaces the old
     `isDeleted(r.entry.source_id)`: a feed deleted here is left out of
     `liveSources` as before.
   - `putBackHiddenStories` skips a kept story whose feed is no longer
     in `sources` (deleted here or found gone by a poll), and forgets it.
   New flows `flow-hide-undo-feed-gone-after` (the feed goes after the
   Undo reached the server: the lay-over) and
   `flow-hide-undo-feed-gone-before` (it goes between the hide and the
   Undo: the put-back).
3. **A failed For You request inside the two minutes left For You
   showing only the put-back story.** `refresh` laid the story over the
   failed request's empty list as if it were the server's For You. The
   lay-over now goes over `fy` only when `forYouOk`, and the story stays
   in `restoredRef`, so the next good answer still gets it. `gotIds` is
   unchanged: a failed For You is `[]` before the lay-over, so it
   already holds only what answered (the comment says so). New flow
   `flow-hide-undo-foryou-fails`; it also checks the next good answer
   (a fix that dropped the story on a failure fails it).
4. **No test covered retrying a failed by-ids request** (minor). New
   flow `flow-saved-retries-a-failed-fetch`: the first by-ids request
   fails (`'offline'`), the next poll asks for 503 exactly once more,
   and Saved shows it. It declares `expectedFailedLoads:
   ['/api/entries/by-ids']` for smoke rule C8.
5. **The lay-over's "a poll returned it" clause and the feed clause
   were unguarded, and the notes left them out** (minor). New flow
   `flow-hide-undo-until-returned`: after the Undo's batch a poll
   returns 101, then the page's fetch drops it: the next poll takes it
   out of News and For You, inside the two minutes. The feed clause (the
   old `isDeleted` one, now the feed check of finding 2) is guarded by
   `flow-hide-undo-feed-gone-after`. `flow-hide-undo-after-poll`'s
   expiry step is unchanged. The list of lines still unguarded is below.
6. **`flow-saved-by-id`'s unsave Undo ran on real time and accepted
   either place** (minor). The flow now installs the clock and holds it
   from the unsave on. Undo tapped before the unsave's batch must put
   503 back in its old place (402, 105, 503, 103); after `runFor(1000)`
   sent the unsave (the store loses 503), Undo must put it at the top
   (503, 402, 105, 103). Both with by-ids offline and no request. The
   comment says what it asserts.
7. **The keepIdsFor unit test claimed to check the keep set App builds**
   (minor). It is renamed "keepIdsFor (PR4c): the keep set is the tabs,
   For You, the saves and the Saved-by-id cache", with a comment naming
   the flow that checks App's call. The notes' claim is corrected
   (round 1's finding 2 and the paragraph under the first mutation
   table). Beyond the minimum, new flow `flow-seen-prune-keeps-saved`
   checks what App passes: `seen:news` seeded at its cap with 402 (a
   hidden save, never fetched, so only the saves hold it) and 503
   (fetched by id, then unsaved, so only the cache holds it) as its
   oldest marks; opening 101 must prune the oldest mark nothing holds
   instead. It costs one flow of about 6 s.

### Tests added or changed (re-check round 2)

- `flows.cjs` helpers: `pollDone` also gives `forYouOk` (a failed For
  You gives `forYou: []`); `openedIds(page, re)` taps a phone tab and
  returns the ids it draws once it is selected and drawn (cards or "No
  stories yet"); `hideAndPoll(page, context, id, n)` holds the clock,
  hides `n` on For You, sends the batch, waits for the store and polls
  without it; `tapUndo(page)`.
- New flows: `flow-hidden-restore-all-after-poll`,
  `flow-hide-undo-until-returned`, `flow-hide-undo-feed-gone-after`,
  `flow-hide-undo-feed-gone-before`, `flow-hide-undo-foryou-fails`,
  `flow-saved-retries-a-failed-fetch`, `flow-seen-prune-keeps-saved`.
- `flow-saved-by-id`: the clock (finding 6).
- `feedOrder.test.ts`: the rename (finding 7). Still 204 unit tests.
- `README.md` (iphone-audit): the new flows and helpers, and
  `flow-saved-by-id`'s two Undo cases.

### Mutation checks (re-check round 2)

Each was a scratch edit in a copy of the fixed frontend, then `npm run
build` and `--only` on the flows (`--csp --strict`, CI fonts, port
4175), two runs each; every mutant failed both runs as shown. The
worktree was never edited for a mutant.

| Test | Broken line | Fails on |
|---|---|---|
| `flow-hidden-restore-all-after-poll` | Restore all back to `removeIds(PREFERENCE_KEYS.hiddenEntries, ids)` (the fix reverted) | RESTORE-ALL: "after Restore all, News shows 102,…,112, without 101", and the same for For You |
| `flow-hide-undo-feed-gone-after` | the lay-over's feed check (`!feedIds.has(r.entry.source_id)` → `false`) | UNDO-FEED-GONE: "feed 3 is gone (after the Undo), and the strip reads […,\"Other1\",…]", right away and after the next poll |
| `flow-hide-undo-feed-gone-before` | `putBackHiddenStories`' feed check dropped | UNDO-FEED-GONE: "feed 3 is gone (before the Undo), and the strip reads […,\"Other1\",…]" |
| `flow-hide-undo-foryou-fails` | `forYouOk &&` dropped from the For You lay-over (the fix reverted) | UNDO-FY-FAILS: "after For You failed, For You started over with 101, expected no stories" |
| `flow-hide-undo-foryou-fails` | the story dropped from `restoredRef` on a failed For You (`\|\| !forYouOk` in the drop clause) | "when For You answered again (before the Undo's batch), For You shows 301,…,502, without 101" |
| `flow-saved-retries-a-failed-fetch` | the in-flight clear-out in the by-id effect's `.finally` dropped | SAVED-RETRY: "after a failed by-ids and a poll Saved shows 103; by-ids was asked for [\"503\"]" |
| `flow-hide-undo-until-returned` | `gotIds.has(id)` → `(gotIds.has(id) && false)` | UNDO-RETURNED: "a poll returned 101 and the next one left it out, but News still shows it", and the same for For You |
| `flow-saved-by-id` | `setStarred`'s unsave sent at once (`void prefs.flushPending()` after `removeIds`) | SAVED-BY-ID: "offline, Undo before the batch: Saved shows 503,402,105,103, expected 402,105,503,103" |
| `flow-seen-prune-keeps-saved` | App's call with `savedById: new Map()` | SEEN-PRUNE: "pruning seen:news dropped the mark of 503, held only by the by-id cache" |
| `flow-seen-prune-keeps-saved` | App's call with `starred: []` | SEEN-PRUNE: "pruning seen:news dropped the mark of 402, a save no tab or cache holds" |

On the unfixed build (`0ee33df`'s App.tsx with the new flows), the
flows for findings 1, 2 and 3 failed with the same messages, and the
other new flows and the reworked `flow-saved-by-id` passed.

Still unguarded after this round. Each line below was broken and run
once against all 15 Saved and hide flows (round 1's seven, the seven
new ones and `flow-foryou-fails-keeps-tab`), with 0 failures:

- **Saved's array reuse** (round 1): it only saves Saved a redraw.
- **The lay-over's re-hide check** (`hiddenNow.has(id)`, round 1): a
  hide made here already drops the story from `restoredRef`, and every
  tab leaves hidden stories out.
- **`toggleHideEntry`'s unhide** (round 1): reached only with the
  desktop keyboard.
- **The negative cache's clean-up** (`savedGoneRef` loses an id once it
  is unsaved). A story by-ids found gone can't be drawn anywhere, so it
  can't be saved again; the clean-up only keeps the set from growing.
- **The feed check reading `liveSources`, not `rawSources`**. They
  differ only for a poll answered before a feed was deleted here, which
  no flow holds; `removeSource` already drops the feed's stories, and
  the poll it starts lands after.
- As before, the `+ added` in the keep cap; `reinsert`'s upper bound is
  equivalent.

### Verification (re-check round 2)

- `npm run lint`: 0 errors (the base's 4 warnings). `npm run build`
  passes. Unit tests: 204 of 204.
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest on `popping_test`, the 12 files that read frontend
  source plus `test_entries_by_ids_projection.py`: 227 passed and 2
  failed, both in the DNS-only baseline list
  (`test_reserved_category_names.py`). No backend file changed.
- The 15 Saved and hide flows above: 5 loops, 0 failures.
- Full strict smoke (`--csp --strict`, CI fonts, port 4175) on the final
  build: 183 states and flows (176 plus the seven new flows), 0
  failures, 0 warnings (594 s). It ran on `127.0.0.175:4175`:
  `127.0.0.1:4175` was taken by a review agent's leftover `vite preview`
  of a mutated scratch copy, which this session didn't start and left
  running.

Deviations: unchanged. Risks: unchanged (Restore all and the feed and
For You rules only change what is laid over a poll, nothing stored).
Device checks: one added, for Restore all (below and in
`docs/DEVICE_CHECKS.md`).

## Review fixes (re-check round 3, "PR4c: re-check fixes, round 3")

Five findings; all applied, none skipped. Three change what the app
does (1 to 3), two are about the tests (4, 5). Each was reproduced
first: the new flows fail on the round 2 build (`85ada9f`'s App.tsx,
Column.tsx and lib/saved.ts) with the findings' symptoms, and the three
reworked flows pass there (the app was right; the flows were weak).

1. **A put-back story went back a place off in For You when other
   hidden stories were above it** (single Undo, per-row Restore and
   Restore all; moderate in effect: one place, until the first poll
   after the Undo's batch). `keepHiddenStories` kept a raw index into
   `entries` and `forYou`, and those lists either still held stories
   hidden earlier (they are left out only where drawn) or had already
   lost one to a poll, so `reinsertAll` applied indexes taken against
   different lists. Now:
   - A hide keeps each story's place as ids (`placeOf`, `lib/saved.ts`):
     the stories right after it and right before it, nearest first,
     each run up to and including the first one that wasn't hidden
     then, and its index as the last resort.
   - The place is taken against the list with the stories already kept
     put back in their places. So a story hidden earlier that a poll
     has dropped still counts as a neighbour, and two hides next to
     each other come back in the right order whichever is restored
     first. This goes beyond the finding's fix (the nearest story not
     hidden, the one before as a fallback): with only that, hiding 102
     and then 301 just above it, with a poll after each, and restoring
     102 first put 301 under 102 (`flow-two-hides-restore-rows-adjacent`).
   - `putBack` (it replaces `reinsert` and `reinsertAll`) puts each story
     just before the nearest story of its "after" run that the list
     holds, else just after the nearest of its "before" run, else at
     its index. A neighbour put back in the same call is waited for. A
     run the list holds none of goes on along the run of its last story
     when that one is a hidden story still kept. Two stories that wait
     for each other (For You reordered between their hides) still both
     go back: the first by its other side, else its index.
   - The Undo (`putBackHiddenStories`) and `refresh`'s lay-over use the
     same places and the same `putBack`.
   New flows `flow-hide-two-undo-the-second` (the finding's (a)),
   `flow-two-hides-restore-all` and `flow-two-hides-restore-rows` (its
   (b)), and `flow-two-hides-restore-rows-adjacent`; eight `placeOf` and
   `putBack` unit cases.
2. **Undo or Restore while For You's last request had failed made For
   You show only the restored story.** `forYouOkRef` is set next to
   `setForYouLoaded(forYouOk)`. While it is false, `putBackHiddenStories`
   leaves For You's empty list alone and keeps the story in
   `restoredRef`, so the next good For You answer gets it laid over, at
   its place (inside the two minutes; after them the server returns
   it). Every unhide goes through it: the Undo, the eye's unhide, Restore
   and Restore all. New flow `flow-hide-foryou-fails-then-undo`.
3. **Saves fetched by id after Saved was drawn were all drawn at once,
   past the 30-card batches.** Once every card of a list is drawn,
   Column grows its limit with every arrival (`drewAllUnseen`), so a
   tab's marker, C8's row and an open Earlier don't go away for a frame.
   Saved has none of those under its cards, and its saves can land by
   id up to 1000 in one update (the phone reopening on its stored Saved
   tab, a tap on Saved right after launch, Clear all's Undo). Column no
   longer grows the limit that way for Saved, so its stories go through
   the batches however they arrive. This is in Column rather than a
   Saved generation in App: a generation that changed with each by-id
   answer would also start Saved over at 30 cards under a reader
   scrolled further down whenever one save landed. New flow
   `flow-saved-batches-late-saves`.
4. **`flow-saved-drops-hidden-and-deleted` said a hidden save leaves
   Saved "at once" but allowed 3 s of real time.** The flow installs the
   clock and holds it before the hide: Saved must leave 105 out within
   1 s while the hide's batch is still held (the store must not have it
   yet), and only then does `runFor(1000)` send it. Its comment and the
   README row say so.
5. **No flow checked where an Undo puts the story back.** Every put-back
   check only asked whether the story was drawn, and 101, the story
   they used, is For You's first. `flow-hide-undo-after-poll` now hides
   102 (in the middle of For You, so a put-back at the top or at the end
   both fail) and checks that For You, opened afresh after the Undo and
   again after the lay-over poll, reads exactly as before the hide.
   `flow-hide-all-downvoted-undo` downvotes 102 and 103 (it was 101 and
   103) and checks For You's order the same way after its Undo. The new
   flows for findings 1 and 2 check For You's exact order too.

### Tests added or changed (re-check round 3)

- New flows: `flow-hide-two-undo-the-second`,
  `flow-two-hides-restore-all`, `flow-two-hides-restore-rows`,
  `flow-two-hides-restore-rows-adjacent`,
  `flow-hide-foryou-fails-then-undo`, `flow-saved-batches-late-saves`
  (its by-ids rows are made in the flow: 248 synthetic saves).
- Reworked: `flow-hide-undo-after-poll` (102, For You's exact order),
  `flow-hide-all-downvoted-undo` (102 and 103, For You's exact order),
  `flow-saved-drops-hidden-and-deleted` (the clock).
- `saved.test.ts`: the three `reinsert` / `reinsertAll` cases are
  replaced by eight: `placeOf`'s runs; `putBack`'s three rules and its
  clamp; the unchanged list; the finding's (a) and (b), each restore
  order; two hides next to each other; stories that wait for each
  other; a run that goes on along a kept story's. 13 cases in the file,
  209 unit tests in all.
- `README.md` (iphone-audit): the new flows and the reworked rows.

### Mutation checks (re-check round 3)

Each was a scratch copy of the final frontend with one edit (the
worktree was never edited for a mutant), `npm run build`, and `--only`
on the flows (`--csp --strict`, CI fonts, a scratch preview); unit
cases ran under strip-types the same way.

| Test | Broken line | Fails on |
|---|---|---|
| the six new flows | all three app fixes reverted (`85ada9f`'s App.tsx, Column.tsx and lib/saved.ts, with the new flows) | HIDE-TWO-UNDO: "after the Undo of 102, For You opened afresh reads 301,201,102,401,104,303,502, expected 301,102,201,401,104,303,502", and the same after the lay-over poll; TWO-HIDES-RESTORE: "after Restore all of 101 and 102, For You opened afresh reads 101,102,301,201,…, expected 101,301,102,201,…", the same for `-rows`, and for `-rows-adjacent` "…reads 101,301,201,102,…"; FY-FAILS-UNDO: "after an Undo while For You's last request had failed, For You opened afresh shows 102, expected no stories until it answers"; SAVED-BATCHES: "once 248 saves landed by id, Saved draws 250 cards, expected its first 30" and "at the end of its first 30 cards Saved draws 250, expected 60". The three reworked flows pass there |
| `flow-hide-two-undo-the-second`, `flow-two-hides-restore-rows-adjacent`; `saved.test.ts` | `putBack` ignores the neighbours (every story at its index) | HIDE-TWO-UNDO (both checks) and TWO-HIDES-RESTORE (`-rows-adjacent`) as above; 6 of the 7 `putBack` cases |
| `flow-hide-two-undo-the-second` | `refresh`'s lay-over without the neighbours (index only) | "after the Undo of 102 and a poll that left it out, For You opened afresh reads 301,201,102,…" |
| `flow-two-hides-restore-rows-adjacent` | a hide's place taken against the list as it is, without the kept stories put back in it | "after Restore on each row of 102 and 301, For You opened afresh reads 101,102,301,201,…" |
| `flow-hide-foryou-fails-then-undo` | `&& forYouOkRef.current` dropped from the For You put-back | "…For You opened afresh shows 102, expected no stories until it answers" |
| `flow-saved-batches-late-saves` | Column's `&& !isSaved` dropped | "once 248 saves landed by id, Saved draws 250 cards, expected its first 30"; "…Saved draws 250, expected 60" |
| `flow-saved-drops-hidden-and-deleted` | M33: Saved reads a copy of `hiddenSet` that a 1.5 s `setTimeout` updates | "after hiding 105 Saved shows 503,105,103, with the hide's batch not sent yet", and two follow-on failures. The round 2 flow passes it |
| `flow-hide-undo-after-poll`, `flow-hide-all-downvoted-undo` | M34: `putBack` always puts a story at the end; and always at the top | HIDE-UNDO: "right after the Undo, For You opened again reads 101,301,201,401,104,303,502,102, expected 101,301,102,…" and the same after the lay-over poll (at the top: "102,101,301,…"); HIDE-ALL-UNDO: the same. The round 2 flows pass both |

Each mutant failed every run shown; the reverted-fixes build and the
anchor and M33/M34 mutants were also run once on the build before a
last clean-up (a hide-order sort that no test could tell apart, then
removed), with the same failures.

Guarded by unit cases only. Each was broken and run against the 12
hide and restore flows (`flow-hide-*`, `flow-two-hides-*` and
`flow-hidden-restore-all-after-poll`), with 0 failures, so only
`saved.test.ts` catches it:

- `putBack`'s wait for a neighbour put back in the same call (the wait
  dropped): "stories put back together wait for each other" fails, on
  an assertion added for it (both stories' other neighbour gone).
- A run that goes on along a kept story's run (the walk dropped): "a run
  the list holds none of goes on along the run of a kept story" fails.
  App's `keptPlaces` giving nothing passes every flow too.
- The way out when two stories wait for each other (dropped): the unit
  file never finishes (the loop doesn't end), and is cancelled at a
  test timeout.

The flows' restores don't need these: with the places taken among the
kept stories, a story's own runs already hold a neighbour the list has.

Still unguarded, as before: Saved's array reuse, the lay-over's
re-hide check, `toggleHideEntry`'s unhide, the negative cache's
clean-up, the feed check reading `liveSources`, and the `+ added` in
the keep cap.

### Verification (re-check round 3)

- `npm run lint`: 0 errors (the base's 4 warnings). `npm run build`
  passes. Unit tests: 209 of 209 (204, less the three `reinsert` /
  `reinsertAll` cases, plus eight).
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest on `popping_test`, the 12 files that read frontend
  source plus `test_entries_by_ids_projection.py`: 227 passed and 2
  failed, both in the DNS-only baseline list
  (`test_reserved_category_names.py`). No backend file changed.
- The 22 Saved and hide flows (round 2's 15, the six new ones and
  `flow-unsave-last-saved`): 0 failures on the final build.
- Full strict smoke (`--csp --strict`, CI fonts, port 4175) on the final
  build: 189 states and flows (183 plus the six new flows), 187 ok, 0
  warnings (1528 s, with another lane's smoke on the same container).
  Two PR3b flows failed:
  - `flow-mark-all-read`: "SETUP: state could not be reached:
    locator.tap: Timeout 30000ms exceeded". Run again on its own it
    passed (12.7 s): load.
  - `flow-new-arrival-appends`: "with the marker above the screen the
    arrival went in: [7912]", "…the Earlier card being read moved from
    207 to 341" and "…C8's row went". It fails the same way, with the
    same three messages, on its own, on the round 2 build (`85ada9f`'s
    app code) and on the PR3b base `45aac1e` built with its own flows
    and mock (the round 3 review saw the same). So it is not from this
    unit. It passed in round 2's full run earlier the same day, so it
    looks tied to the time of day or the container; it is PR3b's flow
    and left for that unit.

Deviations: one added (Column.tsx, above); the Hide Undo one says how
the place is kept. Risks: two added (below). Device checks: the Undo
and Restore all checks now look at the story's place in For You, and
one is added for Saved's batches (below and in `docs/DEVICE_CHECKS.md`).

## Risks and data-compatibility notes

- Revert is safe: Saved falls back to the stories the tabs loaded, by
  date. Nothing new is stored; `starred_entries` keeps its shape.
- Undo on an unsave ("Removed from Saved · Undo") saves the story again.
  If the unsave was already sent (a 1 s batch), the save appends it and
  it comes back at the top of Saved; if not, the two cancel out and it
  stays in its old place. Putting it back in place every time would need
  a whole-list write of a set key, which PR2b removed.
- A device with many old saves no tab holds fetches them once per load,
  200 per request (1000 saves at most, the key's cap: 5 requests).
- Saved stories fetched by id keep the copy fetched (score, comment
  count, cached summary) until the next load, unless a tab brings the
  story again.
- A put-back story goes back next to the stories that were next to it
  when it was hidden (re-check round 3). For You's order comes from the
  ranker and can change between polls, so until the first poll after
  the Undo's batch returns it, the story sits by its old neighbours
  wherever they are now.
- Saved draws its stories 30 at a time however they arrive (re-check
  round 3). A save that lands at the top of a list already drawn to the
  end of a batch pushes that batch's last card past it until the list
  nears its end, where the next batch loads.

## Open items for later units

- PR4d: the keep set also takes every `extraByTab` row (a new `Held`
  field for `keepIdsFor`; `keepSet` takes any number of groups). With the 2-day window, most saves will come
  from the by-id cache; `flow-saved-by-id` must stay green with the
  window on.
- PR5a: Hide moves to the action sheet and swipe; its Undo should keep
  calling `restoreHiddenEntry` (or `restoreHiddenEntries`, which calls
  `putBackHiddenStories`) so the story comes back after a poll. PR5a's held `never` signal (D25) is separate.
- PR8a: a custom tab built from `entries` gets put-back stories the same
  way.

## Device checks

Added under Pending in `docs/DEVICE_CHECKS.md` (`#<pr>` until the PR has
a number):

- Open Menu → Settings → Saved and note the stories it lists, then close Settings and open the Saved tab. Pass: the tab lists the same stories, except any you also hid, each with its picture where it has one, including ones older than anything in the other tabs.
- Save one story, then another, then open Saved. Pass: the second one is at the top, the first right under it.
- In Saved, tap the bookmark on a story to remove it, then force-quit and reopen the app. Pass: it is gone from Saved and stays gone.
- On For You, hide a story and tap Undo on the toast, then open that story's category tab and go back to For You. Pass: the story is in its tab, and back in For You where it was.
- On For You, note the top three stories. Hide the first, wait a minute with Popping open, hide the third, and wait another minute. Open Menu → Settings → Hidden, tap Restore all, close Settings and open the first story's category tab. Pass: the story is there straight away; back on For You, the three are in the order you noted (a new story may sit between them).
- (If you have a desktop) On the laptop, search for an older story the phone's tabs don't show and save it, then wait a minute with Popping open on the phone. Pass: it shows at the top of Saved on the phone, with its picture.
- (If you have more than 30 saves) Open the Saved tab, force-quit Popping and reopen it. Pass: Saved fills in without the app freezing, and more stories load as you scroll to the end.

## Rebased onto main

Main moved under this unit: PR3b's final version (#45, the re-check
rounds after 45aac1e) and PR3c, seen on scroll (#46). The five commits
were rebased from 45aac1e onto main 8516098 (`git rebase --onto`), each
keeping its message, with the conflict resolutions inside them; a sixth
commit makes the hide flows hold up under seen on scroll, the way PR3c
did for PR3b's flows.

### What conflicted

- First commit, `docs/DEVICE_CHECKS.md`: main's new lines (PR3b's two
  desktop end-row checks, PR3c's six `#46` lines) and PR4c's five
  `#<pr>` lines in the same place. Both kept, main's first.
- Second commit, `docs/DEVICE_CHECKS.md` again: the review fix's wording
  of the first `#<pr>` line ("except any you also hid") beside main's
  lines.
- Third commit, `frontend/scripts/iphone-audit/README.md`, the Helpers
  table: PR3c's rows (`holdClock` to `requestsSettled`) and PR4c's
  (`phoneIds` to `pollDone`) at the same spot. Both kept, PR3c's first.
  The flow table merged on its own, row by row.
- The other files merged on their own in every commit (`App.tsx`,
  `Column.tsx`, `flows.cjs`, `capture.cjs`, `feedOrder.ts` and its
  test, `PROGRESS.md`: main's rows with PR3b #45 and PR3c #46 merged,
  PR4c in review with no number). Checked: for every file, the added and
  removed lines of the rebased stack against main are the same as the
  old stack's against 45aac1e, so the rebase changed no line of PR4c's.
  Fourth and fifth commits: clean.

### How the two units sit together (read, then probed)

- **Saved** is open only in PR3c (`trackSeen` false for Saved, and
  `markSeen` looks ids up in the phone tab's partition, which Saved
  doesn't have). The by-id cache and Saved's order change nothing
  there. Column's rule that Saved's saves go through the batches
  (re-check round 3) sits next to PR3c's new-snapshot scroll (a layout
  effect on `trackSeen`, never on for Saved).
- **The put-back never starts a snapshot.** `putBackHiddenStories` and
  `refresh`'s lay-over only change `entries` and `forYou`. A story the
  drawn tab's snapshot still holds (a hide leaves it in the cache; only
  `isRemoved` keeps it off the screen) is drawn again in its place; one
  a later snapshot lost comes back as an arrival (held while the marker
  is above the screen, PR3b). Neither moves the generation, so PR3c's
  scroll to the top for a new snapshot never runs for an Undo. Probed
  below.
- **The put-back never reaches the collector.** A hidden story's card
  unmounts and unregisters, so the observer can't report it; coming back
  it registers afresh, with no time on screen. `isPending` (a story in
  the collector placed as seen by a new snapshot) can't apply to it. An
  Undo of a hide marks nothing; probed below. Mark all's flush and the
  hide's before-flush are PR3c's and untouched.
- **The keep set.** PR3c's `addSeenMarks` reads the keep set through
  `readRefs`; it is PR4c's `keepIdsFor` (tabs, For You, the saves, the
  by-id cache), so a scroll mark's prune keeps a save too
  (`flow-seen-prune-keeps-saved` runs it through an open; the write
  path is one).

### The flows under seen on scroll

The 22 Saved and hide flows were run on the rebased build. Four failed,
all one way: `flow-hide-undo-after-poll` (twice),
`flow-two-hides-restore-rows-adjacent` and
`flow-hide-foryou-fails-then-undo` read For You afresh as "301, 102,
201, …", without 101. A probe (a scratch flow, not kept) showed why:
the flows hold the clock a second ahead at the start of `run`
(`pauseAt`), which is the dwell for the cards on screen since the load,
so 101 qualified; then the tap on 102's hide button brought it into
view (the list went to 482), 101 left through the top, and the
`runFor(1000)` that sends the hide's batch also drained the collector:
`seen:news` added 101, and For You opened afresh put it under the
marker, folded ("7 not seen"). The flows that hide 101 first passed
only because 102's button then sits inside the list's 651 px with no
scroll: the fixture's layout, not the flow.

Changed (all in the sixth commit; every assertion kept, two added):

- **`holdClockFromLoad(page)`** (new helper): `installClock` and a
  pause before the page loads, for a flow whose tab under test is the
  one the page opens on. No card has a second on screen before the
  flow's first step, and only its `runFor`s give the cards time.
  `holdClock` can't do this: it pauses a second ahead, which is the
  dwell for a card on screen since the load. The page loads held (its
  fetches and renders wait on no timer; PR3c's
  `flow-new-arrival-appends` reloads under a held clock the same way).
- **`hideAndPoll`** no longer pauses the clock itself (the flow's
  `before` holds it from the load) and checks that the hide dropped the
  badge by one and no more: a card read for a dwell and scrolled off
  the top by the tap would be marked, and leave the order the flows
  read whole afresh. A layout change now fails as a precondition with
  that message, not as a wrong put-back.
- **`flow-hide-undo-after-poll`**: held from the load. 102 is scrolled
  to the top of the screen and read for a dwell before the hide (the
  cards above it left the top with no time on screen), so the Undo puts
  back a story that had qualified. Two checks added after the Undo:
  the list isn't started over at its top and the card being read (the
  first one wholly below the top edge, 201) is still on screen (no new
  snapshot: PR3c's scroll to the top didn't run); and after the seen
  batch runs the badge is as before the hide (the put-back 102 counts
  as not seen; the Undo's own 1 s batch is still held, so the next step,
  a poll before the Undo's change, is unchanged). The rest of the flow
  is as it was.
- **`flow-hide-two-undo-the-second`** and the six `hideAndPoll` flows
  (`flow-hidden-restore-all-after-poll`, `flow-hide-undo-until-returned`,
  `flow-hide-undo-feed-gone-after` / `-before`,
  `flow-hide-undo-foryou-fails`, `flow-two-hides-restore-all` / `-rows` /
  `-rows-adjacent`, `flow-hide-foryou-fails-then-undo`): `before` holds
  the clock from the load in place of `installClock`; their own
  `pauseAt` lines are gone. Nothing else in them changed.
- README: the rows above, the two helpers.

Unchanged, and why:

- `flow-hide-all-downvoted-undo` opens under Settings (`modalOpen`: the
  list is covered and no dwell runs); closing it starts the dwells on a
  held clock, and nothing scrolls before the flow's last `runFor`.
- The Saved flows (`flow-saved-*`, `flow-unsave-last-saved`): Saved
  isn't tracked.
- `flow-seen-prune-keeps-saved` runs in real time and opens 101 on
  News; a card read for a second and scrolled off by the open adds one
  more `seen:news` mark, which prunes one more of the 998 marks nothing
  holds, so its four checks hold either way.
- `flow-foryou-fails-keeps-tab`: no count is read.

### What the probe found: Chromium's scroll anchoring

With 102 exactly at the top of the screen (`scrollCardToTop`), Chromium
anchors the scroll on 201, the first card wholly on screen. The hide
then pulls the list back by 102's height to keep 201 in place, which
shows the short 301 under the top edge for the hide's second (it
qualifies), and the Undo puts 102 back exactly where it was (0..244,
the list at 598 again) by pushing 301 out above the top: a scroll past
in the tracker's eyes, and 301 is marked. 102, the restored story,
never is, and the list is where the hide found it. WebKit has no scroll
anchoring, so on the phone the hide leaves the list where it is (201
moves up, 301 stays above the top) and the Undo pushes 201 down again:
nothing crosses the top edge and nothing is marked. No app change: the
tracker can't tell a re-layout from a scroll (PR3c, re-check round 1),
Chromium at phone width isn't the device, and turning anchoring off for
the list would change how arrivals and PR3b's holds feel in Chromium,
another unit's call. The flow keeps a sliver of 301 on screen
(`scrollListBy(page, -30)` after the scroll to 102), so Chromium anchors
on 301 and neither the hide nor the put-back moves the cards below; its
comment says why. Without the sliver the probe, which waits for the
observers after the hide, saw the mark every time (the badge read 7);
the flow's own badge check passed three runs without it, because the
report of 301 coming on screen races the hide's `runFor(1000)` when
nothing waits for it, and the sliver takes the race out.

### Verification

The local loop on `stack-pr4c` at the sixth commit (the five rebased
commits on main 8516098), the frontend's `node_modules` linked in,
Postgres database `popping_test`, `vite preview` on 127.0.0.1:4175
(started with nohup, stopped by its PID). The container restarted once
in the middle (the three loops had finished); the preview was started
again and the full smoke ran after it.

- `npm run lint`: 0 errors (the 4 react-refresh warnings main has).
  `npm run build` (tsc, then vite): clean. `git diff --exit-code
  package.json package-lock.json`: clean.
- Unit tests (`node --experimental-strip-types --test
  'scripts/unit/*.test.ts'`): 238 of 238 (main's 222 and this unit's
  16).
- Backend pytest on `popping_test` with `PYTHONPATH` at the worktree,
  the 12 files that read frontend source plus
  `test_entries_by_ids_projection.py`: 230 passed, 2 failed, both the
  DNS-only baseline (`test_reserved_category_names.py`). No backend file
  changed.
- The 22 Saved and hide flows (`flow-saved-*`, `flow-hide-*`,
  `flow-two-hides-*`, `flow-hidden-restore-all-after-poll`,
  `flow-seen-prune-keeps-saved`, `flow-unsave-last-saved`,
  `flow-foryou-fails-keeps-tab`), `--csp --strict`, CI fonts:
  - on the rebased build before this commit: 4 failures (the four
    above);
  - with this commit: 0 failures (254 s), and `flow-hide-undo-after-poll`
    alone 3 more times after its last edit: 0 failures;
  - on a build with `SEEN_DWELL_MS = 50` (a scratch copy; the worktree
    wasn't edited): 22 of 22, 0 failures (241 s), so none of them
    depends on how long its steps take;
  - looped 3 times on the final build: 0 failures each time (220, 221
    and 200 s a loop). No flake.
- Mutation check for the new snapshot check: a scratch build whose
  `putBackHiddenStories` ends with `resetSnapshot()` fails
  `flow-hide-undo-after-poll` twice in two runs ("the Undo's put-back
  started For You over: the list is at its top"; "… moved the list (now
  at 0): 201, being read, is off the screen"). The badge check's mutant
  is the anchoring case above (the sliver taken out, with a wait for the
  observers): "reads 7, expected 8".
- Full strict smoke (`--csp --strict`, CI fonts), every state and flow,
  on the final build: 198 states and flows, 0 failures, 0 warnings
  (1170 s, after the container's restart).
- Security checklist: unchanged. No route, fetch or server-side read
  changed; the sixth commit is flows, their README rows and these notes.
