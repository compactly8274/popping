# PR3c: Seen on scroll (frontend track)

Stacked on PR3b (`claude/ux-pr3b`, #45, in its re-check), built as if it
were merged. PR3b and PR3c deploy together (D33).

## Plan (written before the code)

The unit is "plan first". The spec's line numbers are from main 4be8766,
before PR1r through PR3b, so every anchor was found again by name on
`stack-pr3c` at 45aac1e.

### Anchors

| Spec anchor | Where it is now | Holds? |
|---|---|---|
| Card's stable ref callback (Card.tsx:326-335) | `stableCardRef` in `CardInner`, passes `(el, prev)` | yes: runs only on attach and detach |
| Card's on-mount `view` (Card.tsx:499-506) | the `recordBatched({type: 'view'})` effect in `CardInner` | yes, goes |
| Column's `setCardRef` (Column.tsx:226-232) | `setCardRef` in `ColumnInner`, with PR3b's "PR3c registers ... here" note | yes |
| the marker and its observer | `marker` state (`ref={setMarker}` on both forms of `[data-caught-up]`) and the hold observer | yes |
| `_columnPropsEqual` / slice 21 | Column.tsx, `backend/tests/test_slice21_column_memo.py` | yes |
| App's `addSeenMarks` | App.tsx, "the one way seen marks are written" | yes |
| `useFeedOrder.setHold` | App passes `onHoldChange` → `setHold` | yes; PR3c needs no change there (the end rows already stay put while held) |
| PR2b's `registerBeforeFlush` | `usePreferences().registerBeforeFlush`; `leave()` and `flushPending()` run it before reading the queue | yes |
| phone scroller in state (PR1b) | `phoneScroller`, set through the phone Column's `scrollerRef` (undefined at md+) | yes |
| the phone `<main>` stays mounted at md+ | `main.md:hidden`, phone Column `key={phoneCol.key}` | yes |
| `drawerOpen`, Settings, ShortcutsSheet, TabsEditor | `drawerOpen`, `settingsOpen`, `shortcutsOpen`, `tabsEditorOpen` in App | yes |
| the always-mounted Drawer with `aria-modal` | Drawer.tsx:281 | yes (so `modalOpen` is App's boolean, not a selector) |

### Design

**`lib/seen.ts` (pure, no imports, timers injected).**
- `SEEN_DWELL_MS = 1000`, `SEEN_BATCH_MS = 300`, `SEEN_THRESHOLDS` (0,
  0.1 … 1).
- `createSeenTracker(deps)`: the registry and the rules, with no DOM.
  - The registry: `Map<entryId, Element>` and the marker element. It
    outlives observers. `card(id, el | null, prev?)` and
    `marker(el | null, prev?)` always edit it, and call `observe` /
    `unobserve` only while an observer is attached. A detach passes the
    element going away, so a stale detach can't drop a newer element.
  - `attach(observer)` observes every registered element (and the
    marker); `detach(observer)` disconnects and clears every timer and
    every card's dwell state. `handle(entries, from)` ignores entries
    from an observer that is no longer attached (IntersectionObserver may
    deliver queued entries after `disconnect`).
  - Qualifying: half visible (`isIntersecting` and
    `intersectionRect.height ≥ 0.5 × min(card, root)`) for
    `SEEN_DWELL_MS` straight; dropping under half clears the timer.
  - Seen: a qualified card leaving through the top (`!isIntersecting`,
    `boundingClientRect.bottom ≤ rootBounds.top`), counted only when
    `target.isConnected`, `boundingClientRect.height > 0` and
    `rootBounds.height > 0`. Leaving through the bottom doesn't count.
  - End rule: the marker half visible for `SEEN_DWELL_MS` turns it on:
    every qualified card on screen is seen, and so is each card that
    qualifies while it stays on. The marker dropping under half (or a new
    marker element) turns it off.
  - Pauses: a dwell timer (a card's or the marker's) that fires while
    `pageHidden()` or `modalOpen()` is true restarts instead.
  - `takeRecords()` handles the observer's pending entries at once (the
    hide flush).
  - `onSeen(ids)` once per batch of entries (one IntersectionObserver
    callback, or the end rule), each id at most once per attach.
- `createSeenBatch(deps)`: `markSeen`'s collector. `add(items)` keeps
  them by id; the first add starts a `SEEN_BATCH_MS` timer that drains
  everything collected in one call; `flush()` drains now.

**`lib/useSeenObserver.ts` (React glue).**
`useSeenObserver(root, {modalOpen, onSeen})`: one tracker per App (state),
one IntersectionObserver per root (an effect on `root`), thresholds
`SEEN_THRESHOLDS`. `modalOpen` and `onSeen` are read through refs. Returns
a stable registry `{card, marker, takeRecords}`.

**App.**
- `useSeenObserver(isDesktop ? null : phoneScroller, {modalOpen, onSeen})`.
- `modalOpen = drawerOpen || settingsOpen || shortcutsOpen ||
  tabsEditorOpen`, with a marked place for PR5a's ActionSheet and PR8a's
  TabSheet.
- `onSeen(ids)`: each id is looked up in the shown phone tab's latest
  partition (`latestPart(key).snapshot.cache`); one already seen
  (`isSeen`, read through a ref) is skipped; the rest record `view`
  (`recordBatched`) at that moment and go to the collector.
- The collector (`createSeenBatch`, `SEEN_BATCH_MS`) drains into
  `addSeenMarks`: one `addIds` per `seen:<category>` key per batch.
- Flush on hide: `registerBeforeFlush(() => { seen.takeRecords();
  batch.flush() })`, so a card that left the top just before the page hid
  is in the keepalive PATCH.
- Mark all flushes the collector first, so its Undo removes only what
  Mark all added (a story seen on scroll stays seen).
- Open marks keep bypassing the collector (`openEntry` → `addSeenMarks`).
- The phone Column gets `trackSeen={phoneCol.key !== SAVED_KEY}` and
  `seenRegistry={seen}`. The phone Column is keyed by tab, so `trackSeen`
  never changes for a mounted one. Desktop Columns, SearchResults, the
  desktop For You row and Brief get neither (open only, C2).

**Column.** `trackSeen` (data prop, joins `_columnPropsEqual` and slice
21) and `seenRegistry` (stable, ignored by the comparator like the
callbacks; read through a ref). `setCardRef(id, track)`: attach registers
when `track` (unseen group and `trackSeen`); detach always unregisters
with the element going away. The marker registers from an effect on
`[trackSeen, marker]`.

**Card.** The on-mount `view` effect goes; `recordBatched` leaves its
imports.

### Tests

- Unit `scripts/unit/seen.test.ts`: zero rects; a detached target; a
  hidden page; `modalOpen` true; a closed always-mounted dialog that does
  not pause; a root change; the end rule; plus qualify-then-exit, a
  fling, an interrupted dwell, a tall card, leaving through the bottom,
  stale entries from an old observer, `takeRecords`, the collector; and
  the hook case (register, tear the observer down, recreate it, the card
  is observed again) on the tracker the hook wraps.
- pytest slice 21: `trackSeen` joins the comparator list.
- Flows (clock installed in `before`, held, moved with `runFor`):
  `flow-scroll-marks-seen`, `flow-view-on-seen`,
  `flow-seen-flush-on-hide`, `flow-landscape-no-marks`, and the flung
  case in `flow-caught-up`.
- PR3b's flows and smoke states that read the unseen count with cards or
  the marker on screen now race the dwell in real time. They get a held
  clock where they need one (found by running every flow and state, not
  guessed), so they keep testing PR3b's rules deterministically.

### Risks checked in the plan

- No `role="status"`, no `<section>`, no new sheet: smoke rules hold.
- No new process, worker or port; IntersectionObserver on the main thread
  with no library.
- Desktop: no tracking, no `view` events from cards any more (spec: the
  on-mount view goes; desktop is open only). (Changed by the review
  fixes: opening a story records its `view`, so the desktop's views come
  from opens.)

## What changed

On the phone, a story read on screen and scrolled off the top is used up,
like an opened one: it stays where it is, the badge and the end row count
it out, and its mark goes to `seen:<category>` in one PATCH per batch. The
last cards on screen with the end row count once it has been there a
second (the end rule), so a tab read to its end says "You're all caught
up" and a tab flung through says "N not seen · Mark as read". The `view`
event moves from drawing a card to that moment.

- `frontend/src/lib/seen.ts` (new, pure, no imports): `SEEN_DWELL_MS`
  (1000), `SEEN_BATCH_MS` (300), `SEEN_THRESHOLDS`;
  `createSeenTracker(deps)` (the registry, the qualify / seen / end rules,
  the pauses, `attach` / `detach` / `handle` / `takeRecords` /
  `setCovered`); `createSeenBatch(deps)` (the collector).
- `frontend/src/lib/useSeenObserver.ts` (new, React glue):
  `useSeenObserver(root, {modalOpen, onSeen})` → a stable
  `{card, marker, takeRecords}`. One tracker per App; one
  IntersectionObserver per root (thresholds every tenth); covered while
  the page is hidden or a sheet is open.
- `frontend/src/App.tsx`:
  - `modalOpen = drawerOpen || settingsOpen || shortcutsOpen ||
    tabsEditorOpen`, with the place for PR5a's ActionSheet and PR8a's
    TabSheet marked.
  - `markSeen(ids)`: each id looked up in the phone tab's latest
    partition; one already seen is skipped; the rest record `view`
    (`recordBatched`) and go to the collector, which drains into
    `addSeenMarks` (one `addIds` per `seen:<category>` key).
  - `useSeenObserver(isDesktop ? null : phoneScroller, …)`.
  - `registerBeforeFlush(() => { takeRecords(); flush() })`: the hide,
    `pagehide`, `beforeunload` and the reload's flush write what is
    collected first.
  - `markColumnRead` (Mark all) flushes the collector first and counts
    (and undoes) only the marks it added.
  - The phone Column gets `trackSeen={phoneCol.key !== SAVED_KEY}` and
    `seenRegistry`.
- `frontend/src/components/Column.tsx`: `trackSeen` (data prop, in
  `_columnPropsEqual`) and `seenRegistry` (stable, read through a ref,
  ignored by the comparator like the callbacks). `setCardRef(id, track)`
  registers an unseen card when `track`, and every detach unregisters with
  the element going away; the marker (either form) registers from an
  effect on `[trackSeen, marker]`.
- `frontend/src/components/Card.tsx`: the on-mount `view` effect is gone.
- `frontend/src/lib/interactions.ts`: the header says where views come
  from now (comment only).
- `backend/tests/test_slice21_column_memo.py`: `trackSeen` joins the
  comparator list.
- Tests and harness: `frontend/scripts/unit/seen.test.ts`; the flows and
  smoke changes below; `frontend/scripts/iphone-audit/README.md`;
  `docs/DEVICE_CHECKS.md` (Gate 1 refined phone-first, PR3c's lines under
  Pending as `#<pr>`); `docs/ux-notes/PROGRESS.md`.

## Deviations from the spec

- **Covered dwells stop and start over** (beyond the spec's pause rule,
  which is kept). The spec restarts a dwell timer that fires while the
  page is hidden or `modalOpen` is true. On iOS a hidden page's timers are
  frozen and fire late, just after the page is back, when it is no longer
  hidden: a card on screen for a moment before the user left would
  qualify. So the hook also tells the tracker when the list is covered
  (`setCovered`: the page hidden, or a sheet open): every running dwell
  stops, and the cards still half on screen start theirs over when it is
  uncovered. `flow-seen-flush-on-hide` and two unit cases guard it.
- **The marker joins the seen observer through Column's `marker` state**,
  not through PR3b's hold observer: the end rule needs "half visible for
  the dwell" on the 11-threshold observer, and the hold observer answers
  only "above the screen or not". `useFeedOrder.setHold` and the hold
  observer needed no change: the end rows already stay as drawn while the
  marker is above the screen, so a count changed by a scroll mark moves
  nothing.
- **`trackSeen` is true for the phone Column whatever the width.** The
  phone Column stays mounted, hidden, on a desktop; the registry keeps its
  cards there so a turn back under 768px observes them again without a
  remount (the spec's registry), and no observer runs on the desktop
  (`root` null). The desktop Columns never get it.
- **Ids are looked up in the phone tab's partition**
  (`latestPart(key).snapshot.cache`), which also keeps Saved out a second
  time (Saved has no partition), and covers a story a poll dropped.
- **Mark all flushes the collector first**, and its toast counts what it
  marked (`added`), not the partition's count a render old; with nothing
  left to mark it starts the tab over with no toast. Not in the spec;
  without it, a card seen on scroll just before Mark all was undone by
  Mark all's Undo. Every other snapshot started while the collector holds
  a story places that story as seen (the partition's `isPending`, re-check
  round 1; the second commit's flushes at each reset are gone).
- **A tab that tracks seen on scroll starts its list at its top with each
  new snapshot** (re-check round 1). The spec scrolls to the top only on
  C1's return; a Refresh, a new Sort or age window reset in place. On the
  phone a reset in place took the stories read off the top out from above
  the screen, so the cards being read jumped up past the top, were marked
  as scrolled past, and the reader's place was lost. Column does it in a
  layout effect, for every reset of the phone tab; the desktop grid and
  Saved don't track and are unchanged.
- **A story used up on screen calls off C1's second reset** (re-check
  round 1), as an open does (PR3b). The end rule uses cards up with no
  touch, scroll or key, which were the only things that called it off.
- **The marker's focus goes on to its other form inside a snapshot**
  (re-check round 1). PR3b hands the focus on only after the end row's own
  keyboard press; seen on scroll can now change the row's form with the
  focus on it (the end rule using up the last cards).
- **PR3b's flows and smoke states that count unseen stories with cards
  or the marker on screen now hold the page's clock.** Found by building
  with `SEEN_DWELL_MS = 50` and running every flow and state: in real time
  the end rule (or a scroll past) used their cards up after a step that
  took over a second. Held: `caught-up`, `not-seen-row`, `earlier-open`
  (smoke states), `flow-auto-append`, `flow-new-arrival-appends`,
  `flow-arrival-batch-edge`, and `flow-caught-up` (rewritten, below).
  `flow-h1-keepalive` queued its view from a card drawn on load; it now
  waits twice the dwell in real time and scrolls a card off the top.
- **`flow-caught-up` counts with the end rule.** Flung to its end (clock
  held) it still reads "40 not seen"; after the dwell the k cards on
  screen with the marker are used up, and the rest of the flow (two
  opens, Mark as read, Undo, Earlier) runs with 40 − k, 38 − k and
  "Earlier (2 + k)". k is read from the page (3 at 390×844).
- **One flow beyond the spec's list:** `flow-saved-open-only` (Saved
  scrolled through marks nothing and sends no view). Search and Brief
  have no Column, so nothing there can register.
- `flows.cjs` reads `SEEN_DWELL_MS` and `SEEN_BATCH_MS` from
  `src/lib/seen.ts`, so a Gate 1 retune is one edit.

## Tests

- Unit `frontend/scripts/unit/seen.test.ts` (28 cases): the thresholds;
  qualify then off the top; a fling; an interrupted dwell; a card taller
  than the list (half of the list, not of itself); leaving through the
  bottom; zero rects (display:none, a zero-height list); a detached
  target; a hidden page; `modalOpen` true; a closed always-mounted dialog
  that does not pause (a fake `document` holds an `aria-modal` element;
  only App's boolean counts); covered and uncovered (a dwell the hide cut
  in two starts over; a card and the marker that came on screen while
  covered start theirs once uncovered); the end rule (every qualified
  card on screen; one that qualifies while the marker stays; it stops
  when the marker leaves; a new marker element starts over); seen once
  per observer; one callback, one `onSeen`; a root change (disconnect,
  timers and dwell gone, old entries dropped); a dwell under way when the
  observer goes; the hook case (register, tear the observer down, a card
  registered meanwhile, recreate it: both observed, and marks happen); a
  late detach; `takeRecords`; the collector; and (review fixes) the
  marker's own dwell: it starts over when it ends behind a sheet or a
  hidden page (26), a cover stops it and uncovering starts it over (27),
  and it doesn't start while covered (28).
- pytest `test_slice21_column_memo.py`: `[trackSeen]`.
- New flows (390, the page's clock installed and held, moved with
  `runFor`): `flow-scroll-marks-seen`, `flow-view-on-seen`,
  `flow-seen-flush-on-hide`, `flow-saved-open-only`,
  `flow-landscape-no-marks`, and (review fixes) `flow-seen-tab-switch`.
  The README has each step.
- Changed flows: `flow-caught-up` (the flung-card case and the end rule),
  `flow-auto-append`, `flow-new-arrival-appends`,
  `flow-arrival-batch-edge` (clock held), `flow-h1-keepalive` (its view
  comes from a card seen on scroll). Smoke states `caught-up`,
  `not-seen-row`, `earlier-open`: clock held from the tap on the tab.
  Review fixes: `flow-scroll-marks-seen` reads the badge before and after
  the seen batch runs; `flow-view-on-seen` opens the top card first;
  `flow-new-arrival-appends` waits for the hold with `observersSettled`.
- Flow helpers: `holdClock`, `observersSettled` (a fresh
  IntersectionObserver's first report comes in the same task as every
  older one's, so the app's have run; no animation frame needed, which a
  held clock never delivers), `listCards`, `scrollListBy`,
  `scrollCardToTop`, `dwell`, `writeSeen`, `requestsSettled` (every
  preference and interaction request answered, so a check that nothing
  was written can follow; found when a mutant survived a negative check
  that ran before its PATCH reached the route).

## Verification

Local loop on `stack-pr3c`, on `claude/ux-pr3b` at 45aac1e (fetched before
the commit: it had not moved, and main doesn't hold PR3b yet), the
frontend's `node_modules` linked in, Postgres 16 database
`popping_test_f2`, `vite preview` on 127.0.0.1:4174.

- `npm run lint`: 0 errors (the 4 react-refresh warnings main has).
  `npm run build` (tsc, then vite): clean. `git diff --exit-code
  package.json package-lock.json`: clean.
- Unit tests (`node --experimental-strip-types --test
  'scripts/unit/*.test.ts'`): 218 of 218 (`seen.test.ts`: 25).
- Backend pytest, the whole suite: 1713 passed, 13 failed; the 13 are
  exactly the container's DNS-only baseline (slice 21 with `[trackSeen]`,
  slice 19 and the other tests that read frontend source pass).
- Strict smoke (`--csp --strict`), every state and flow, on the final
  build: 0 failures, 0 warnings (176 runs, 503 s). Run again after the
  two last flow edits: one failure, `flow-fb-address-commits-on-leave`
  ("expected 1 PATCH after Enter and a blur, saw 2"; a feed-editor flow
  this unit doesn't touch, with the load average at 6 on 4 CPUs while
  other sessions ran); run alone three times after: ok each time.
- The same suite on a build with `SEEN_DWELL_MS = 50`: 0 failures (176
  runs), after the fixes under "Flow determinism".
- The PR3c flows and every flow and state this unit changed or that
  leans on the read model (`flow-scroll-marks-seen`, `flow-view-on-seen`,
  `flow-seen-flush-on-hide`, `flow-saved-open-only`,
  `flow-landscape-no-marks`, `flow-caught-up`, `flow-auto-append`,
  `flow-new-arrival-appends`, `flow-arrival-batch-edge`,
  `flow-h1-keepalive`, `flow-open-marks-seen`,
  `flow-return-after-10-min`, `flow-mark-all-read`, `flow-badge-cap`,
  `flow-scroller-rotate`, and `caught-up`, `not-seen-row`, `earlier-open`
  at 390 and 320): 21 runs, looped 5 times, 0 failures each time (79 to
  87 s a loop); the two flows edited after that, 5 more times each: 0
  failures.
- The PR3c flows at `SEEN_DWELL_MS` 700 and 1500 (the Gate 1 retunes):
  pass.
- Mutation checks: 32 mutants (table below); 31 fail where expected, one
  survives by design (a second guard behind it), and its double mutant
  fails.
- Security checklist: no route, fetch or server-side read changes. Scroll
  marks go through PR2b's PATCH (`addIds`) under PR2a's allowlisted
  `seen:` prefix, like an open; views go through the existing interaction
  batch.
- Self-review on the unit's three lenses before the commit: the
  observer's lifecycle (tab switch, rotation, a root not yet in state,
  React's effect order, stale entries after `disconnect`), visibility and
  the flush order (the leave flush runs `takeRecords` and the collector
  before reading the queue; dwells stop while covered), and flow
  determinism (held clocks, `observersSettled`, `requestsSettled`, the
  50 ms and retune runs). The unit's review agents run after this.
- WebKit results come only from CI.

### After the review fixes

The same local loop on the fixed tree, still on PR3b at 45aac1e (PR3b's
re-check, 76f31bd, is on its branch but not under this one yet; see the
rebase check below), database `popping_test_f2`, preview on
127.0.0.1:4174.

- `npm run lint`: 0 errors (the same 4 warnings). `npm run build`:
  clean. `package.json` and the lockfile: unchanged.
- Unit tests: 221 of 221 (`seen.test.ts`: 28).
- Backend pytest, the whole suite: 1713 passed, 13 failed, the same 13
  as the DNS-only baseline (no backend file changed; the tests that read
  frontend source pass).
- Strict smoke, every state and flow: 0 failures, 0 warnings (177 runs,
  635 s, load average 7 to 13 on 4 CPUs while the backend suite ran
  beside it).
- The PR3c flows and the read-model flows and states
  (`flow-seen-tab-switch`, `flow-scroll-marks-seen`, `flow-view-on-seen`,
  `flow-seen-flush-on-hide`, `flow-saved-open-only`,
  `flow-landscape-no-marks`, `flow-caught-up`, `flow-auto-append`,
  `flow-new-arrival-appends`, `flow-arrival-batch-edge`,
  `flow-h1-keepalive`, `flow-open-marks-seen`,
  `flow-return-after-10-min`, `flow-mark-all-read`, `flow-badge-cap`,
  `flow-scroller-rotate`, `flow-sort-best`, `flow-refresh-resets`,
  `flow-next-tab`, and `caught-up`, `not-seen-row`, `earlier-open` at
  390 and 320): looped 5 times, 0 failures each time (112 to 134 s a
  loop).
- `flow-new-arrival-appends` alone, 10 more times: 0 failures (load
  average 3.8 to 5).
  The reviewer's failures came at a load average of 13 to 23 (other
  sessions' CPU burners); this container ran at 1.5 to 5 here, so these
  runs show the flow still passes, not that the old race is gone. That
  rests on the wait itself: `observersSettled` resolves after Column's
  observer has set the hold, where `sleep(400)` only hoped it had.
- The four flows this commit adds or edits (`flow-seen-tab-switch`,
  `flow-view-on-seen`, `flow-scroll-marks-seen`,
  `flow-new-arrival-appends`) on builds with `SEEN_DWELL_MS` 50, 700 and
  1500: 0 failures each.
- Mutation checks: 10 more mutants (the rows marked "review fixes" in the
  table below), each a scratch edit, a rebuild where it is app code, an
  `--only` run (or the unit file), and a restore compared byte for byte:
  all 10 fail where expected.
- Rebase onto PR3b's re-check: `git merge-tree --merge-base 45aac1e
  76f31bd <this tree>` merges `flows.cjs` (with the
  `flow-new-arrival-appends` change) and `App.tsx` cleanly. It reports
  two conflicts, `frontend/scripts/iphone-audit/README.md` (the flow
  table, where 76f31bd adds `flow-return-from-earlier` beside PR3c's
  rows) and `frontend/src/components/Column.tsx`, and the first PR3c
  commit alone reports the same two: they are the rebase's to resolve.
  This commit adds one README row to that same table block and doesn't
  touch Column.tsx.
- Security checklist: unchanged. No route, fetch or server-side read; the
  `view` an open now records goes through the existing interaction batch.

### Mutation checks (test → broken line → failing rule)

Each was a scratch edit, then a rebuild, `--only` on the flows (or the
unit file, or the pytest), and a restore compared byte for byte. The last
full run was on the final code; the two flows edited after it were run
again against the mutants that reach them (F1-F4, F7, F11, F13, F15).
Unit cases are by their place in `seen.test.ts`.

| Test | Broken line | Fails on |
|---|---|---|
| unit | `leftThroughTop` without `isConnected` | 8 (a detached target) |
| unit | `leftThroughTop` without the zero-height guards | 7 (zero rects) |
| unit | no pause check when a card's dwell fires | 9, 10 (hidden page, sheet open) |
| unit | the pause read from `[aria-modal="true"]` | 10, 13 (the closed always-mounted dialog) |
| unit | `detach` keeps the dwell | 19 (a root change) |
| unit | `attach` observes nothing registered | 19, 21 (the hook case) |
| unit | no end rule | 12, 14, 15, 16, 23 |
| unit | half of the card, not of min(card, list) | 5 (a tall card; strengthened after it first survived: 1500px showing 800 passed both ways, 2000px doesn't) |
| unit | entries from a detached observer taken | 19 |
| unit | leaving through the bottom counts | 6 |
| unit | the batch waits from the last story (debounce) | 25 |
| unit | a card qualifying under the end rule waits | 15 |
| unit | `setCovered` does nothing | 11, 12 |
| unit | a dwell starts while covered | 12 |
| unit | uncovering restarts no card | 11, 12 |
| `flow-scroll-marks-seen`, `flow-caught-up` | `markSeen` never adds to the collector | SCROLL-SEEN: "scrolling [602,603,7001,7002] off the top sent []"; CAUGHT-UP: the end row reads "40 not seen", expected "37 not seen" |
| `flow-scroll-marks-seen` | `modalOpen` always false | SCROLL-SEEN: "a card scrolled off straight after the Menu, which was open through the dwell, was marked: [601]" |
| `flow-scroll-marks-seen` | `modalOpen` also true while `[aria-modal="true"]` exists (the closed Drawer) | SCROLL-SEEN: "scrolling [602,603,7001,7002] off the top sent []" |
| `flow-scroll-marks-seen`, `flow-caught-up` | a card qualifies on sight (no dwell) | SCROLL-SEEN: 601 marked, the fling's cards marked; CAUGHT-UP: "34 not seen", expected "37 not seen" |
| `flow-view-on-seen` | Card's on-mount `view` back | VIEW-ON-SEEN: "views sent for cards only drawn: [601,602,603,7001,…]" |
| `flow-view-on-seen` | `markSeen` records no `view` | VIEW-ON-SEEN: "story 601 was seen on scroll and no view was sent" |
| `flow-seen-flush-on-hide` | the before-flush does nothing | SEEN-HIDE: "hiding right after a card left the top sent no keepalive PATCH of seen:nhl"; "story 601 … never reached the store" |
| `flow-seen-flush-on-hide` | the hook never covers the list | SEEN-HIDE: "[7004,7005,7006,7007] counted on a dwell the hide cut in two" (it first survived: the check ran before the PATCH reached the route; `requestsSettled` came from that) |
| `flow-landscape-no-marks` | a new observer doesn't observe the registered cards | LANDSCAPE: "back at 390, two cards scrolled off the top sent []" |
| `flow-landscape-no-marks` | the phone list kept as the root on the desktop, and the zero-rect guards gone | LANDSCAPE: "sideways, a seen: PATCH went: … add [601,602,603]" |
| `flow-caught-up` | no end rule | CAUGHT-UP: "the end row reads "40 not seen · Mark as read", expected "37 not seen · Mark as read"" |
| `flow-caught-up` | the marker never registers | CAUGHT-UP: the same |
| `flow-scroll-marks-seen` | Mark all without the collector flush | SCROLL-SEEN: "no "Marked 31 as read" toast"; "Mark all's Undo took back story 7012, seen on scroll before it" |
| `flow-scroll-marks-seen` | Column registers no card | SCROLL-SEEN: "scrolling [602,603,7001,7002] off the top sent []" |
| `flow-saved-open-only` | `trackSeen` on Saved, alone | **survives**: `markSeen` looks ids up in the phone tab's partition and Saved has none, a second guard |
| `flow-saved-open-only` | `trackSeen` on Saved, and a lookup that finds Saved's stories | SAVED-OPEN-ONLY: "scrolling Saved marked stories: … add [7001,7002] …"; "scrolling Saved sent views" |
| pytest | the comparator ignores `trackSeen` | `test_column_comparator_checks_prop[trackSeen]` |
| unit (review fixes) | no pause check when the marker's dwell fires | 26 ("modal: the end rule ran behind it"); pausing for only one of the two flags fails it on the other ("hidden: …") |
| unit (review fixes) | `setCovered(true)` keeps the marker's timer | 27 ("the marker's dwell stopped") |
| unit (review fixes) | the marker's dwell starts while covered (`!covered` gone from `onMarker`) | 28 ("a dwell started under the cover") |
| unit (review fixes) | uncovering restarts no marker dwell | 12, 27, 28 |
| unit (review fixes) | the three above at once (the reviewer's mutant, which passed all 25 cases before) | 26, 27, 28 |
| `flow-seen-tab-switch` (review fixes) | `selectTab` doesn't flush the collector | SEEN-SWITCH: "story 101, seen on scroll just before the tab switch, is above the marker" |
| `flow-seen-tab-switch` (review fixes) | `startTabsOver` doesn't flush (a Refresh that lands inside the window) | SEEN-SWITCH: "story 102, seen on scroll just before a Refresh, is above the marker" |
| `flow-seen-tab-switch` (review fixes) | `setPrefsFor` doesn't flush (a new Sort) | SEEN-SWITCH: "story 104, seen on scroll just before a new Sort, is above the marker" |
| `flow-view-on-seen` (review fixes) | `openEntry` records no `view` | VIEW-ON-SEEN: "story 601 was opened and no view was sent" |
| `flow-scroll-marks-seen` (review fixes) | `markSeen` calls `addSeenMarksRef.current(stories)` instead of `seenBatch.add(stories)` (it skips the batch) | SCROLL-SEEN: "the badge dropped before the seen batch ran: it reads "36", expected "40"" |

### Flow determinism

PR3c makes time on screen matter on the phone, so every flow and state
was also run against a build with `SEEN_DWELL_MS = 50`: a flow that passes
there doesn't depend on how long its steps take. The first such run
found the PR3b flows and states listed under Deviations; they hold the
clock now. The PR3c flows were also run at 700 and 1500 (the Gate 1
retunes; `flows.cjs` reads the constant from `seen.ts`). Two steps were
tied to 1000 and are fixed: the 50 ms run failed the cut-in-two step
(600 / 500 ms, now scaled from the constant), and the 1500 run failed the
fling (it counted on `writeSeen`'s 1.45 s as a dwell; it now runs an
explicit `dwell` first).

## Review fixes (second commit)

The unit's review agents (observer lifecycle; visibility and flush
order; flow determinism) confirmed five findings. All five are fixed;
none was skipped.

1. **A reset inside the collector's 300 ms put a story just read above
   the marker of the tab it started.** A tab switch starts both tabs over
   (PR3b), and the destination's new snapshot was built in the render the
   tap caused, while the story read off the top a moment before was still
   in the collector: it went into unseen and, once its mark landed, stayed
   there (ids in a snapshot keep their group), drawn as new but counted
   out of the badge and the end row. Nothing was lost or written twice.
   Now everything that starts a snapshot over writes the collector first,
   so the marks and the reset land in one render:
   - `selectTab` (the strip, the Menu's Jump to and C8's row through
     `jumpToCategory`, and the resolve effect) calls `seenBatch.flush()`
     first. The collector and `addSeenMarksRef` moved up beside the
     phone's tab, so `selectTab` can reach it; it still drains into the
     latest `addSeenMarks` (the ref is set where that is declared).
   - `startTabsOver(key?)`, a small App-side wrapper: flush, then PR3b's
     `resetSnapshot`, which is unchanged. Every reset App asks for goes
     through it: Refresh / `r` when its stories land (a fetch that returns
     inside 300 ms), Mark all and its Undo, C1's return (already flushed
     by the hide; harmless).
   - `setPrefsFor` flushes before a Sort or age-window change: the tab's
     `order` starts its new snapshot in the next render.
   - Left alone: a tab that disappears (the render that shows the tab
     taking its place comes before the resolve effect's `selectTab`) and a
     change of the source filter (PR8b removes it). Both start from a sheet
     (Settings or the Menu), and the collector drains 300 ms after its
     first story, long before a change made there.
   - Test: `flow-seen-tab-switch` (new): For You's top story read and
     scrolled off, its category's tab tapped before the collector's timer
     (clock held): it is under the marker, in Earlier, and `seen:news`
     adds it once. The same for a card read off just before a Refresh lands
     and just before ⋯ → Best. Mutants: each of the three flushes removed
     alone fails its own step (table above).
   - Re-check round 1 replaced the three flushes (and `startTabsOver`)
     with one rule in the partition, which also covers the paths left
     alone here; see "Review fixes (re-check round 1)".
2. **An opened story recorded no `view`** (and the desktop none at all),
   against D3's "recorded at that moment". `openEntry` now records it
   (`recordBatched`, next to `addSeenMarks`), on every layout and from
   every way out (tabs, Saved, search, the desktop For You row).
   `recordBatched` keeps one view per story per session, so an open and a
   scroll past, in either order, send one. `markSeen` keeps its skip of a
   story already seen: opened ones recorded their view when opened, one
   marked on another device recorded it there, and Mark all isn't a read.
   Brief draws no story cards or links (its text is the AI's), so
   nothing there opens a story. Test: `flow-view-on-seen` opens the top card with
   nothing scrolled (its `click` and exactly one `view`), then scrolls it
   and the next card off the top (one `view` for the next, none more for
   the opened one). Mutant: no `view` in `openEntry`. The "desktop records
   no view" line under Open items now reads "the desktop's views come from
   opens".
3. **No test covered the end rule's own pause.** The marker's dwell timer
   restarts when it fires behind a sheet or a hidden page, a cover stops
   it, and it doesn't start while covered; the reviewer's mutant removing
   all three passed every case and flow. Unit cases 26-28 pin each one
   (table above: each alone, and the three together, fail). The optional
   `flow-caught-up` step (the Menu held open with the marker on screen) is
   not added: with the cards and the marker coming on screen together it
   passes against that mutant too (the cards' own dwells stop under the
   cover, so nothing is qualified when the marker's timer fires), and a
   step that qualifies the cards first depends on where the end row falls
   at 390×844. The unit cases test the rule directly.
4. **App's use of the 300 ms collector had no test.** In
   `flow-scroll-marks-seen`'s "read, then scrolled off the top" step the
   badge is read after a 200 ms real-time wait with the clock held (React
   renders through MessageChannel, which the clock doesn't hold; a frame
   wait wouldn't do, rAF is held): it still reads 40. Then the seen batch
   runs (`SEEN_BATCH_MS + 50`) and the badge drops before prefSync's 1 s
   PATCH batch has gone; the existing PATCH checks follow. Mutant:
   `markSeen` writing straight to `addSeenMarks` fails with "the badge
   dropped before the seen batch ran".
5. **`flow-new-arrival-appends` waited for the hold with real-time
   sleeps.** The three `sleep(400)` after a scroll meant to put the marker
   above the screen are now `observersSettled(page)`: it resolves in the
   notification task in which Column's marker observer has already called
   `setHold`, so the hold is set before `poll()` brings the arrival in
   (Column's `held` state isn't in the DOM, so this is the thing to wait
   on). The `sleep(400)` inside `poll()` waits for something else and
   stays. PR3b's re-check (76f31bd) doesn't touch this flow's lines, so
   the change carries over unchanged when PR3c is rebased onto it
   (checked with `git merge-tree`, below). PR3b's round 2 later made the
   same waits `markerAbove(list)`, which the rebase onto the final PR3b
   keeps (below).

## Rebased onto the final PR3b (third commit)

PR3b moved on under this unit: its re-check rounds 2 and 3, a rebase
onto main 0ff72b1 and a progress commit (`stack-pr3b` at 7d06cbf). The
two PR3c commits were rebased onto it (`git rebase --onto stack-pr3b
76f31bd`), each keeping its own message; the conflict resolutions are in
them. A third commit makes PR3b's newer flows hold up under seen on
scroll.

### What conflicted

- First commit (four files):
  - `docs/DEVICE_CHECKS.md`: PR3b's two new desktop end-row checks and
    PR3c's five `#<pr>` lines in the same place. Both kept, PR3b's first.
  - `frontend/scripts/iphone-audit/README.md`: the flow table, merged row
    by row against the old base. PR3b's newest text for the rows only it
    changed (`flow-desktop-layout`, `flow-earlier-keys-fold`,
    `flow-return-after-10-min`, `flow-return-from-earlier`,
    `flow-refresh-resets`, `flow-h1-pref-store`, `markerAbove`,
    `scrolledTo`); PR3c's for its own rows and helpers. Two rows changed
    on both sides were merged by hand: `flow-caught-up` (PR3c's end-rule
    text, with round 3's "a tap is a finger's press: the focus doesn't go
    to it") and `flow-new-arrival-appends` (round 2's `markerAbove` text,
    with PR3c's held clock).
  - `frontend/scripts/iphone-audit/flows.cjs`, `flow-caught-up`: PR3c's
    comment, counts (`rest.length`, `38 − k`) and the clock moved for the
    writes; round 3's focus rule. PR3c's "the focus goes to the marker
    after the tap" check is gone and round 3's "a tap doesn't hand the
    focus to the marker" check stays.
  - `frontend/src/components/Column.tsx`: PR3c's end-rule marker effect
    and round 3's rewritten refocus effect (pointer type, the scroll into
    `sentinelRoot`'s box) sit next to each other; both kept as they were.
  - `App.tsx` merged by itself (round 2's `click` in `RETURN_INPUTS` and
    the `dropSelection` comment beside the collector, `startTabsOver` and
    the before-flush). The commit's size is unchanged (14 files, +1986
    −67).
- Second commit (two files):
  - README: `flow-view-on-seen`'s new text and the `flow-seen-tab-switch`
    row beside round 2's rows.
  - `flows.cjs`, `flow-new-arrival-appends`: review fix 5 had replaced
    the three `sleep(400)` with `observersSettled(page)`; round 2 replaced
    them with `markerAbove(list)`. PR3b's is kept: like
    `observersSettled` it resolves once the app's marker observer has
    reported (an observer made then, on the list, reports in the same
    notification task or a later one), and it also fails when the marker
    never went above the screen. The clock stays held across this flow's
    reload (checked: `Date.now()` didn't move over 2.5 s of real time
    after it), so its second half still runs held.

### PR3b's flows since 45aac1e

Every flow `git log -p 45aac1e..stack-pr3b -- flows.cjs` adds or
changes was read for a way seen on scroll can use cards up (a card on
screen for the dwell and then scrolled off the top, or cards on screen
with the marker), and the set was run on a build with `SEEN_DWELL_MS =
50`. That run failed `flow-open-marks-seen`, `flow-return-from-earlier`
and `flow-refresh-resets` (the three the earlier full smoke had named).

Changed:

- **`flow-open-marks-seen`**: the clock is held from the tap on News. In
  real time the middle-click on 104's Reddit link scrolled the list
  (Playwright brings the link into view) and took 101 and 102, read for
  over a second, off the top: the badge read 8, not 9. Held, each open
  drops the badge at once and prefSync's batch (`runFor`) then adds
  exactly the three opened, checked as a set. An `observersSettled`
  after that middle-click lets the app's observer see the cards leave
  (unread) before the clock moves: without it the dwell timers fired
  for cards already off screen and the late report then counted them
  (102 was marked that way in 4 runs of 4). The search's 300 ms wait and
  its open's PATCH run on `runFor`. The story opened as the page hides
  is checked unmarked before its open. On Podcast (two unseen episodes
  with the marker on screen) the badge drops by one at the middle-click
  and the PATCH follows on `runFor`, so the end rule can't stand in for
  the open.
- **`flow-refresh-resets`**: `installClock`, and the clock held from the
  tap on News. In real time the tap on Earlier (at the end of the list)
  waited out the "1 new" toast for 3 s, then scrolled the read cards off
  the top and left the last ones on screen with the marker: 9 unseen
  before the third Refresh, not 13, and so 29 drawn, not 30 and the
  sentinel. The NHL part scrolls read cards off the top too, and its
  Refresh must start over with 60 unseen (the old limit). Held, the
  toast stays up over the middle of Earlier, so that tap goes to its left
  end (`tap` passes options to `locator.tap`).
- **`flow-return-from-earlier`**: the clock held from the tap on NHL. The
  six unseen cards sit on screen with the marker at first, and the last
  ones with it again when the end row is scrolled to: in real time the
  end rule read "5 not seen" there, not 6.
- **`flow-return-after-10-min`**: the clock stays as it was (the C1
  steps move it with `runFor` and `away`). Its first scroll, to 400,
  comes after `runFor(3000)`, so every card it takes off the top was read
  and is used up; the first 11-minute return now expects those under
  Earlier with the opened story (`listCards(...).above` after
  `observersSettled`; none at 390×844, so the numbers are the old ones).
  The later steps compare with the list each reset drew, so a card read
  off before a slow return changes nothing they check.

Unchanged, and why:

- `flow-desktop-layout`, `flow-earlier-keys-fold`: 1280 only; the
  desktop is open only (no observer).
- `flow-caught-up`, `flow-new-arrival-appends`: held since PR3c's first
  commit; round 2 and 3's edits (the focus rule, `markerAbove`) run held.
- `flow-mark-all-read`: News never scrolls in it and its marker stays
  below the screen while unseen cards are drawn, so nothing can be used
  up; round 3 only moved a screenshot.
- `flow-h1-pref-store` (PR2c, via main): no read model.

### The keepalive failure: test timing, not the flush order

"hiding right after an open sent no keepalive PATCH of seen:news" came
from the flow, not from the app:

- In the real-time flow, `seen:news` held `[101,103,104,102]` before 102
  was opened: 102 had been read and scrolled off the top (the Reddit
  middle-click's scroll), and the seen batch had written it. Opening it
  then had nothing to add (`addSeenMarks` skips a story already marked),
  so nothing was queued and the hide had nothing to send. The step's
  other check (102 in the store) passed for the same reason. Held, 102 is
  unmarked when it is opened (now checked first) and the hide's
  keepalive PATCH carries it.
- The flush order holds: the before-flush runs the observer's
  `takeRecords`, then drains the collector, and only then does prefSync
  read its queue, where an open's mark already is (opens go straight to
  `addSeenMarks`). A probe (not kept) read cards and scrolled them off
  the top into the collector (105, 106, 107), opened another story (108)
  and hid the page at once, clock held: one keepalive PATCH of
  `seen:news` carried `[108,105,106,107]`. With the collector's flush
  taken out of the before-flush it carried `[108]`, so the probe tells
  the two apart. No app change.
- The race the flow showed (a dwell timer firing while the report of the
  card leaving is still on its way) is the flow's `runFor` jumping a
  second at once; on a device the report comes within a frame.

### Verification

The local loop on `stack-pr3c` at the third commit, on `stack-pr3b` at
7d06cbf, the frontend's `node_modules` linked in, Postgres database
`popping_test_f2`, `vite preview` on 127.0.0.1:4174.

- `npm run lint`: 0 errors (the 4 react-refresh warnings main has).
  `npm run build`: clean. `package.json` and the lockfile: unchanged.
- Unit tests: 221 of 221.
- Backend pytest, the eight files that read frontend source
  (`test_slice21_column_memo.py`, `test_frontend_feed_editor_rules.py`,
  `test_frontend_slice15_add_generic_scrape.py`,
  `test_frontend_slice19_empty_column_source_status.py`,
  `test_preferences_category_layout.py`, `test_preferences_delta.py`,
  `test_slice30_safe_external_url.py`,
  `test_source_create_sitemap_link_pattern.py`): 172 passed.
- Strict smoke (`--csp --strict`), every state and flow: 0 failures, 0
  warnings (178 runs, 670 s, load average 5 to 7 on 4 CPUs while other
  sessions' smokes ran).
- The four flows this commit changes, the two the rebase merged
  (`flow-caught-up`, `flow-new-arrival-appends`), the other read-model
  flows (`flow-badge-cap`, `flow-next-tab`, `flow-mark-all-read`,
  `flow-arrival-batch-edge`, `flow-earlier-keys-fold`,
  `flow-desktop-layout`, `flow-sort-best`, `flow-scroller-rotate`,
  `flow-auto-append`, `flow-h1-keepalive`), the six PR3c flows and the
  states `caught-up`, `not-seen-row`, `earlier-open` at 390 and 320 (28
  runs): looped 3 times, 0 failures each time (143 to 156 s a loop). No
  flake.
- The same 28 runs on a build with `SEEN_DWELL_MS = 50`: 0 failures. The
  four changed flows at 700 and 1500: 0 failures.
- Mutation checks, each a scratch edit, a rebuild, an `--only` run and a
  restore compared byte for byte; all five fail where expected, so the
  held flows still catch what they were written for:

| Test | Broken line | Fails on |
|---|---|---|
| `flow-open-marks-seen` | Card's middle-click notes no open | "after a middle-click the News badge reads "11", expected "10"" (and the Reddit, PATCH set and Podcast checks) |
| `flow-open-marks-seen` | `openEntry` marks from a `setTimeout(0)` | "after an open the News badge reads "12", expected "11""; "hiding right after an open sent no keepalive PATCH of seen:news"; "story 102, opened as the page hid, never reached the store" |
| `flow-return-from-earlier` | the hold observer ignores a new snapshot (`generation` out of its deps) | "with the marker below the screen the arrival waits"; "the end row, scrolled to at 555, moved to 741" |
| `flow-refresh-resets` | a new snapshot keeps the drawn limit (no `setLimit(BATCH)`) | "after a Refresh to 60 unseen with 60 drawn before it, the NHL tab draws 60 unseen cards without a "load more" sentinel" |
| `flow-refresh-resets` | a new snapshot keeps `drewAllUnseen` | "after a Refresh that brought 20 to 13 unseen the tab draws 33 unseen cards without a "load more" sentinel" |

## Review fixes (re-check round 1)

The re-check's reviewers confirmed five findings (4 against this unit,
1 against PR3b's flows). Four are fixed; the fifth is skipped as PR3b's
(below). Each of the four is reproduced by its new test with the fix
taken out (the mutation table below), with the symptoms the reviewers
reported (for fix 2 the very ids and offsets: 7006-7009 marked, the list
left at 1516 showing 7014-7017).

1. **C1's second reset moved stories the end rule had just used up into
   a folded Earlier, with no input.** A long return arms a second reset
   for when its refresh lands; only an input (`RETURN_INPUTS`), a scroll
   off the top or an open called it off. The end rule uses up the cards
   on screen with the end row with none of those, so on a tab whose last
   unseen cards fit on one screen, a refresh slower than the dwell and
   the seen batch (normal for an iPhone resuming) landed, found the reset
   still armed, and its snapshot put the cards just used up under a
   folded "Earlier (N)". This broke D3 ("stays where it is until you
   leave the tab or pull to refresh").
   - Fix: `markSeen` calls `disarmReturnReset()` when it hands a story to
     the collector (and returns early when there is none), as `openEntry`
     does for an open. A story used up on screen, scrolled past or by the
     end rule, now calls the second reset off. `disarmReturnReset` joins
     `markSeen`'s dependencies; it is stable.
   - Test: `flow-return-after-10-min` gains a last step. The clock is held
     from the tap on NHL (its three stories and its end row on one
     screen), and the tab is left at once. Back after 11 min with the
     return's entries request held, the dwell and the seen batch run with
     no input, and the row reads "You're all caught up". When the request
     is let go, the three are still above the marker and there is no
     Earlier.
2. **A Refresh or a new Sort/age window on a scrolled phone list marked
   the cards on screen and jumped the reading place.** The tracker can't
   tell a scroll from a re-layout: any qualified card that ends up above
   the list's top counts as scrolled past. A reset in place (the list
   left where it was) took every story read off the top out from above
   the screen, so the cards being read jumped up past the top and were
   marked, and unseen cards no one had reached sat above the screen.
   With PR3b alone only opened stories left the group, and nothing was
   marked by it.
   - Fix: Column starts the list of a tab that tracks seen on scroll
     (`trackSeen`) at its top with each new snapshot. It is a layout
     effect that runs after the commit's DOM changes and refs, before the
     browser draws or reports any intersection, so no frame shows the
     jump and no observer sees the cards cross the top. The list's
     element comes through a new `setList` callback ref, which also hands
     it to `scrollerRef` as before (it changes with `scrollerRef`, so App
     still gets null when the width crosses 768px). This covers every
     reset of the phone tab: a Refresh landing, a Sort or age-window
     change, the filter's own fetch landing, Mark all and its Undo
     (already at the top in practice: after Mark all the list is three
     short rows), C1 (which scrolls there itself), and later units' resets
     (PR6b's check, PR8a's feed edit). The desktop grid and Saved don't
     track, so they keep PR3b's behaviour.
   - At the top, the unseen stories start with the ones being read (for
     the same sort; a Refresh's new stories come first), so the reader
     keeps their place in the story order, if not the pixel offset.
   - Why not the other option (the tracker forgetting its state at a new
     generation): it stops the false marks but leaves the reader on
     cards they hadn't reached, with unseen ones above the screen.
   - Test: `flow-seen-reset-in-place` (new). NHL with 40 stories, clock
     held: three screens read off the top (marked), the next screen read
     for a dwell, then the header Refresh. No mark goes for the cards
     being read, the list is at its top, the stories read off the top are
     under the marker, and the cards being read lead the unseen ones, on
     screen. Then two more screens and ⋯ → Best: no mark for the cards
     being read, and the list at its top.
3. **The end rule turned the focused end row into "You're all caught up"
   and the focus fell to `<body>`.** The row's two forms are two elements
   (a `<button>` and a `<div>`), and PR3b's hand-over (the refocus
   effect) runs only after the row's own keyboard press.
   - Fix: Column's marker ref is a callback (`setMarker`) around the
     state setter. When the element going away holds the focus and a new
     marker element comes in the same commit, within the same snapshot,
     the new one is focused with `preventScroll`. A new snapshot (the
     row's own press, any reset) is left to PR3b's refocus effect, so a
     finger or mouse press still hands nothing on (round 3).
   - "Within the same snapshot": the render's generation against the
     last committed one, which the layout effect of fix 2 moves on after
     the commit's refs.
   - It relies on React's commit order: a detached ref is called with
     null before its node leaves the page (so `document.activeElement` is
     still the old row), and refs are set before layout effects. A marker
     that goes with no new one in that commit hands nothing on later (the
     layout effect clears the flag).
   - It also covers the other swap inside a snapshot: an arrival turning
     a focused "You're all caught up" into "1 not seen · Mark as read".
   - Test: `flow-caught-up` gains a last step. On Podcast its two unseen
     episodes are on screen with the end row, which is focused. After the
     dwell and the seen batch, the row reads "You're all caught up" and
     the focus is on `DIV[data-caught-up]`.
4. **Turning the phone past 768px inside the collector's 300 ms drew the
   story just read above the marker of its grid column.** The grid's
   columns weren't drawn, so they start snapshots in the render the turn
   causes. The story read off For You's top was still in the collector,
   so it went into unseen and stayed there once its mark landed (ids
   keep their group). It was drawn as new while counted out. The second
   commit's flushes covered three paths. This one, and the others its
   notes left alone (the resolve effect, the source filter), had none.
   - Fix: one rule instead of a flush at each call site. A snapshot that
     starts while a story is still in the collector places it as seen.
     - `createSeenBatch` gets `has(id)`.
     - `partitionTab` takes an optional `isPending`. A new snapshot, and
       an arrival, place a pending story under the marker. The counts
       still wait for the mark, so a written batch is still one change of
       the badge (`flow-scroll-marks-seen` checks that the badge holds
       until the batch runs).
     - `useFeedOrder` passes `isPending` through, and App gives it
       `(e) => seenBatch.has(e.id)`. The collector moved down beside the
       partition, its first reader.
   - The second commit's flushes in `selectTab` and `setPrefsFor`, and
     the `startTabsOver` wrapper, are gone. Their call sites are PR3b's
     `resetSnapshot` again. Mark all keeps its own flush, which decides
     what it counts and what its Undo takes back. The hide's before-flush
     stays too.
   - Tests:
     - Unit: a `feedOrder.test.ts` case (pending in its own snapshot keeps
       its place and counts; a reset puts it under the marker and stops
       counting it; its mark then moves nothing; a first snapshot and an
       arrival place it the same). `seen.test.ts`'s collector case now
       checks `has`.
     - `flow-seen-turn-mid-batch` (new): For You's top story read off
       the top, then the viewport turned to 844×390 before the seen batch
       runs. The story is not above its column's marker, it is in that
       column's Earlier, and `seen:news` adds it once.
     - `flow-seen-tab-switch` now runs on the new rule. Each step reads a
       story no earlier step used. The first mutant run showed why: with
       the rule broken, the tab switch left 101 above the marker, the
       Refresh step read 101 again (already marked, so nothing was
       pending), and it passed. Each step now stands alone.
5. **No flow guards C1's immediate reset on return**: skipped. The
   finding and its verdict both place it in PR3b. The gap is in PR3b's
   `slowReturn` and PR3b's mutation tables, and it reproduces at PR3b's
   own head, 7d06cbf. PR3c's commits don't cause it and don't touch
   `slowReturn`. This unit's rules say not to do another unit's work, so
   it is left for PR3b's re-check with the reviewers' suggested check: in
   `flow-return-after-10-min`'s first slow return, before `land()`, fail
   when `shown` still holds `inView`; the same in
   `flow-return-from-earlier` with its return's entries request gated;
   then re-run the `onReturn` mutant.

Also changed: `docs/DEVICE_CHECKS.md` gets one `#<pr>` line, for Refresh
read a few screens down a tab. The README has the new and changed flow
rows. Deviations and Open items above are corrected for fixes 1 to 4.

### Verification

The local loop on `stack-pr3c` (on `stack-pr3b` at 7d06cbf), the
frontend's `node_modules` linked in, database `popping_test_f2`,
`vite preview` on 127.0.0.1:4174 (started with nohup, stopped by its PID).

- `npm run lint`: 0 errors (the 4 react-refresh warnings main has).
  `npm run build`: clean. `package.json` and the lockfile: unchanged.
- Unit tests: 222 of 222.
- Backend pytest, the eight files that read frontend source (listed
  under the third commit's Verification): 172 passed.
- Strict smoke (`--csp --strict`), every state and flow: 0 failures, 0
  warnings (180 runs, 610 s, another lane's smoke running beside it).
  The only source change after it is one App comment line; the runs
  below are on the final build.
- The five flows this round adds or changes, the other PR3c flows, the
  read-model flows and the states `caught-up`, `not-seen-row`,
  `earlier-open` (30 runs): looped 3 times, 0 failures each time (148
  to 155 s a loop).
  - The third loop first stopped on a harness error, not a failure:
    `route.fetch: socket hang up` on the page's first GET to the
    preview, which was up and answering. `flow-seen-reset-in-place`, the
    flow it stopped in, then ran alone 3 times (ok each time), and the
    whole third loop again: 0 failures.
- The five flows on builds with `SEEN_DWELL_MS` 50, 700 and 1500: 0
  failures at 50 and 1500.
  - At 700, one run of the five failed in PR3b's part of
    `flow-return-after-10-min`: "the second slow return did not start
    the tab over at its top", before any step this round adds.
  - It didn't come back in 15 more runs at 700 (12 of the flow alone, 3
    of the five), with a probe on `slowReturn`'s result.
  - That part of the flow runs on the page's own clock in real time, so
    a poll is free to land around it. One that lands with the step's
    arrival would make the reset put that arrival first. This is a guess
    that no run confirmed; the flow's slow returns are PR3b's.
- Mutation checks: the table below.
- Security checklist: unchanged. No route, fetch or server-side read.

### Mutation checks

Each was a scratch edit with a script, then a rebuild and a preview
restart for app code, an `--only` run (or the unit file), and a restore
checked by SHA-256. All fail where expected.

| Test | Broken line | Fails on |
|---|---|---|
| unit | `partitionTab` places by `isSeen` alone (`isPending` ignored) | the new `feedOrder.test.ts` case |
| unit | the collector's `has` always false | the collector case (`has: the stories still waiting`) |
| `flow-return-after-10-min` | `markSeen` doesn't call off the second reset | RETURN: "[601,602,603], used up on screen by the end rule before the return's refresh landed, moved when it landed: above the marker now [], Earlier "Earlier (3)""; "… has "Earlier (3)", expected none" |
| `flow-seen-reset-in-place` | no scroll to the top on a new snapshot | SEEN-RESET: "a Refresh marked [7006,7007,7008,7009], on screen being read and never scrolled past"; "after a Refresh the list is at 1516"; "after a Refresh [7014,7015,7016,7017] are on screen"; "after a new Sort the list is at 744" |
| `flow-caught-up` | the marker's ref never records the focus | CAUGHT-UP: "the end rule turned the focused end row into "You're all caught up" and the focus is on BODY, expected the marker" |
| `flow-caught-up` | the hand-over without the same-snapshot check | CAUGHT-UP: "after a tap on Mark as read the focus went to the marker" (round 3's rule) |
| `flow-seen-tab-switch`, `flow-seen-turn-mid-batch` | App passes no `isPending` | SEEN-SWITCH: 101 (tab switch), 102 (Refresh) and 103 (Sort) "… is above the marker", and Earlier lacks 103; SEEN-TURN: "story 101, read off the top of For You just before the turn, is drawn above the news column's marker"; "the news column has no Earlier" |

The second commit's three flush mutants (`selectTab`, `startTabsOver`,
`setPrefsFor` without the flush) no longer apply: those flushes are gone,
and the last row above breaks the rule that replaced them.

## Open items for later units

- PR5a: the card's ActionSheet open state joins `modalOpen` (App, the
  marked place). A card hidden with the left swipe leaves the partition,
  unmounts and unregisters; nothing else to do.
- PR8a: the TabSheet joins `modalOpen`. Custom tabs are phone Columns:
  `trackSeen` stays `key !== SAVED_KEY`, and their stories are looked up in
  their own partition.
- PR6b: a pull moves the list down from its top, so nothing leaves
  through the top; `resetSnapshot()` on a check remounts what changes
  group and re-registers it, and the phone list starts at its top with
  the new snapshot (Column, re-check round 1), where a pull leaves it
  anyway.
- PR8a: a feed edit on a custom tab resets its snapshot; the phone list
  starts at its top with it (the same Column rule), so nothing read on
  screen is marked by the re-layout.
- Gate 1: a retune of `SEEN_DWELL_MS` is one edit in `src/lib/seen.ts`;
  the flows read it.
- Known limit: a `view` recorded by the hide's `takeRecords` waits in the
  interaction queue until the page is back (the interaction module's own
  hide handler runs before the preferences' leave flush). Views are
  advisory; the seen mark itself goes in the keepalive PATCH.
- The desktop's views come from opens (`openEntry` records the `view`,
  on every layout); the phone's from opens and scrolling past. Drawing a
  card records none.
- `ARCHITECTURE.md`: no change. The tracker, the registry and the
  collector are browser memory; nothing server-side.

## Data compatibility and rollback

- Scroll marks are ordinary `seen:<category>` marks (PR3b's key, PATCH
  only, cap 1000, pruned against the keep set). Nothing new is stored.
- Revert leaves PR3b's model: open or Mark all only. The marks made by
  scrolling stay valid and keep counting as seen.
- As a git revert PR3c stands alone; it deploys with PR3b (D33).
