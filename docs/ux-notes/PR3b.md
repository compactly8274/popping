# PR3b: Read model: open marks seen, marker, Earlier, badges (frontend track)

Started stacked on PR3a (`claude/ux-pr3a`, itself on PR7a #42 and main's
PR2b #40), built as if those were merged. Rebased onto main `e1f0390`
once PR3a merged (#43): it branches from main, with no unmerged parent.

## Plan (written before the code)

The unit is "plan first". The plan below was written before any code, from
an inventory of every call site the spec's delete and rewire lists name.
The spec's line numbers are from main 4be8766, before PR1r, PR1b, PR1c,
PR2b, PR7a and PR3a, so everything was found again by name.

### Inventory (stack-pr3b at 7015ffd)

`frontend/src/App.tsx`

| Spec item | Where it is now | What happens |
|---|---|---|
| `HIDDEN_RESET_MS` (2 min) | :95 | becomes `RETURN_RESET_MS` (10 min), C1 |
| `readEntriesKey` | :179 | goes (new code never writes `read_entries:*`) |
| `MAX_SEEN_ENTRY_IDS`, `capSetFIFO` | :181-197 | go |
| `SectionCollapse`, `ColumnSections` types | :200-206 | go |
| `setLastViewed`, `clearLastViewed` aliases | :762-766 | go |
| `setColumnSections` alias, `columnSections` | :749, :771-774 | go |
| `lastViewedRef`, `columnSectionsRef` and their effects | :790-803 | go |
| `seenEntryIdsRef` | :850 | goes; its uses at :1145, :1257-1273, :1864-1868, :2326-2330 go |
| `hiddenAtRef` | :852, :1260-1273, :1501 | its `seenEntryIdsRef` use goes; C1's time away moves to the update check's stamp (see C1) |
| `columns` memo's `totalCount` | :1072-1094 | goes ("X of Y" goes) |
| `globalReadIds` | :1120-1126 | becomes `seenIds`: every `seen:*` row and every legacy `read_entries:*` row |
| `newCountByColumn` | :1143-1160 | goes (badges come from the partition) |
| `unreadIdsByColumn` | :1172-1182 | goes |
| `sectionsByColumn` | :1193-1206 | goes, replaced by `useFeedOrder` |
| `refresh()`'s seen-set and toast | :1257-1273, :1301-1304 | the seen-set goes; the manual toast reads "N new" / "Nothing new" from the ids held before the request, and a manual refresh resets the snapshots |
| `resetLocalState` keys | :1376-1411 | also deletes `seen:*` rows, and drops their queued writes |
| visibility handler | :1499-1510 | keeps the poll; C1 runs from the update check's return (below) |
| `unmarkColumnRead` | :1851-1871 | becomes Mark all's Undo: `removeIds` of exactly the added ids per `seen:<category>` key, then a reset |
| `unmarkEntryRead`, `markEntryRead`, `toggleEntryRead` | :1878-1933 | go |
| `toggleHideEntry`'s mark-read and its Undo's unmark | :1964-2000 | go: a hide no longer marks the story read (after PR3b a story is used up only by opening it or by Mark all) |
| keyboard `m` | :2183-2193, deps :2270 | goes |
| ArrowUp / ArrowDown | :2167-2177 | walk the partition: unseen, then Earlier |
| `markColumnRead` | :2295-2349 | becomes Mark all (keeps its name: slice 21 pins it) |
| `setColumnSection`, `sectionsCollapsedCache`, `sectionsCollapsedFor` | :2552-2602 | go |
| SearchResults `readIds` / `onMarkRead` | :2979, :2982 | become `onOpenEntry` |
| desktop For You row `unread` / `onMarkRead` | :3080-3091 | go; the row's cards get `onActivate` |
| desktop Column wiring | :3139-3189 | `sections` from the partition; `newCount`, `onRefresh`, `onMarkRead`, `onMarkEntryRead`, `totalCount`, `sectionsCollapsed`, `onToggleSection` go; `unseenCount`, `nextTab`, `generation`, `onMarkAll`, `onOpenEntry`, `onOpenTab`, `onHoldChange` come |
| phone tab badge | :3238-3272 | the unseen count, `99+`, `aria-label="<tab>, N new"`, none on Saved |
| phone Column wiring | :3319-3371 | as the desktop wiring |

`frontend/src/components/Column.tsx`: props `newCount` (:73), `onMarkRead`
(:82), `onRefresh` (:89), `onMarkEntryRead` (:97), `totalCount` (:106),
`sectionsCollapsed` / `onToggleSection` (:159-162); the header's
`role="button"`, `tabIndex`, handlers and `title` (:336-378, :414-421); the
chip (:434-467); "X of Y" (:468-473); the Fresh / History sections
(:600-712) and `ColumnSection` (:731-861); the comparator lines (:1008,
:1019, :1026). The ⋯ already shows on For You (PR1c).

`frontend/src/components/Card.tsx`: `mountTime` (:382-390); `readRevealRef`
(:314, :529-531, :866-875); `commitMarkRead` (:543-555) and its commit
branch (:684); the right-swipe clamp (:654); the context menu's "Mark as
read" (:727-735); the unread ring (:840-844); the action bar's `onMarkRead`
term (:1280); ✓ (:1379-1414); `CheckIcon` (:1522-1550); the `onMarkRead`
prop (:51-61). `unread` becomes `dimmed`. `onActivate` (:46) is called only
by the headline (:933-940); it is threaded to the Thumbnail (:1742-1798),
the Reddit-thread link (:1018-1045), Listen (:1093-1114) and the context
menu's "Open in new tab" (:815-824).

`frontend/src/components/SearchResults.tsx`: `readIds` / `onMarkRead`
(:28-32, :52-53, :95, :98).

`frontend/src/components/ShortcutsSheet.tsx`: the `m` row (:43).

`frontend/src/lib/preferences.tsx`: `seen:*` rows are ignored by
`decodeState` (:242); `setLastViewed`, `clearLastViewed` and
`setColumnSections` lose their last caller.

`backend/tests/test_slice21_column_memo.py`: the parametrize list (:94-115),
HANDLERS (:141-151) and `test_sections_collapsed_uses_memo_cache`
(:178-202). `test_frontend_slice19_empty_column_source_status.py:138`: the
`<Column\b[^>]*>` regex stops at the first `>`, so no prop with an arrow may
come before `sourcesByName=` at either Column site.

`frontend/scripts/iphone-audit/flows.cjs`: `flow-reset-keeps-saves` taps ✓
(:1824); FN's toast regex (:2252); `flow-desktop-layout` and
`flow-auto-append` gain their PR3b steps.

### Design

**`lib/feedOrder.ts` (pure, no imports).**
- `partitionTab({entries, isSeen, isRemoved, order, snapshot, hold, reset})
  → {unseen, earlier, snapshot, unseenCount, unseenIds}`. `entries` come in
  the tab's sort (App's `columns` memo sorts once). `order` is the tab's
  order as a string (its sort now; PR4d adds the window): a different
  `order` from the snapshot's starts a new snapshot, so a Sort change resets
  the tab whichever device made it.
- New snapshot (none, `reset`, or another `order`): unseen holds what isn't
  seen, earlier what is, both in the entries' order. The generation is the
  old one plus one.
- Kept snapshot: ids keep their group and place; ids new to the input
  append to the end of unseen, unless `hold`, which keeps them in `held`
  (counted, not drawn) until a call without `hold`. Each call refreshes the
  cached Entry of every id the input has; an id the input no longer has is
  drawn from its cached Entry until the next snapshot. `isRemoved` is
  applied to every id, cached ones too, at output time, so a hidden story
  leaves at once and an Undo puts it back in its place.
- The output arrays are rebuilt each call and compared element by element
  with the last ones: equal, the last arrays come back (Column's and Card's
  memos compare by reference); one Entry replaced by a poll, a new array.
- `unseenCount`: unseen-group ids not seen, held arrivals included.
  `unseenIds` is that list (Mark all marks exactly these).
- `countUnseen(entries, isSeen)` for tabs that aren't partitioned.
- `seenTest(ids, firstRunAt)`: D33. A story counts as seen when its id is
  marked, or when it was fetched more than 48 h before `firstRunAt`.
- `seenKey(category)`: `seen:<category>`.
- `nextTab(tabs, current, count)`: C8, the next shown tab after `current`
  (wrapping) with a count above 0, never Brief or Saved.

**`lib/useFeedOrder.ts` (React glue).** Holds `Map<tabKey, Snapshot>` in a
ref and partitions the tabs App draws on every render. A tab not drawn in a
render is marked for reset, so a tab switch resets both tabs (the one left
and the one opened). Nothing is kept until the app is ready (preferences
answered and the first refresh done), so the first snapshot sees the stored
marks. `resetSnapshot(key?)` (one tab or all) and `setHold(key, hold)` are
stable; `latest(key)` reads the last partition from the ref, for Mark all.

**Seen.** `seenIds` is the union of every `seen:*` row and every legacy
`read_entries:*` row. `preferences.tsx` decodes `seen:*` rows into
`seenEntries` (the per-column map pattern). New marks go only to
`seen:<category>` through `addIds(key, ids, keep)`, where `keep` is every
fetched id (entries, For You, saves): PR2b's `pruneIds` drops ids outside it
first. PR4c and PR4d add the Saved-by-id cache and `extraByTab` rows to it.

**D33.** `STORAGE_KEYS.firstRunAt`, read and written through
`safeGetItem` / `safeSetItem`; when storage throws, this load's time. Nothing
is written to the server.

**Opening.** `openEntry(entry)` (stable, reads refs) calls `addIds` under
the story's `seen:<category>` key synchronously and `noteStoryOpened()`
(PR1r's open stamp, which C1's guard reads). Both Columns, SearchResults and
the desktop For You row pass it; Card calls `onActivate` from the headline,
the Thumbnail, the Reddit link, Listen and the context menu's "Open in new
tab". Opening still records `click` as today; no `dwell` is sent.

**Mark all.** `markColumnRead(key)` marks the partition's `unseenIds` under
each story's `seen:<category>` key, resets the tab, and shows "Marked N as
read" with Undo. The Undo removes exactly those ids per key and resets the
tab again. The ⋯'s first row (44px, disabled at 0, absent on Saved) and the
end row's "N not seen · Mark as read" both call it.

**C1.** `useUpdateCheck(isDesktop, onReturn)`: the hook already stamps the
hide and measures time away with `Date.now`. On every return it starts
PR1r's check first (as before, only in a production build), then calls
`onReturn({away, openedBefore})`. App: at least `RETURN_RESET_MS` away and
not hidden within 2 s of an open → reset every snapshot and scroll the phone
list (and the desktop wrapper) to the top, then the return's silent refresh
runs (App's own visibility listener, registered after the hook's). The
refresh that return starts resets the snapshots again when it lands, so the
stories that arrived while away are sorted in, not appended.

**Column.** The header is the label, "N new" on md+ and ⋯. The list is the
first 30 unseen cards and the `[data-more]` sentinel; once every unseen card
is drawn, the `[data-caught-up]` row (✓ "You're all caught up" at 0, a 44px
"N not seen · Mark as read" button above 0), C8's 44px next-tab row (only
when caught up), and the folded 44px `button[data-earlier][aria-expanded]`
"Earlier (N)". Earlier draws dimmed cards 30 at a time. None of the rows is
`role="status"` or a `<section>`. Saved has no rows. An empty tab keeps
`EmptyColumnMessage`. Column owns the two limits and `earlierOpen`, resets
them when `generation` changes, opens Earlier when `selectedId` is in it,
and grows the limits to reach `selectedId`. It reports whether its marker is
above the viewport (`onHoldChange`, an IntersectionObserver on the marker
with `sentinelRoot` as root), which holds new arrivals back (Safari has no
scroll anchoring). Data props `unseenCount`, `nextTab` and `generation` join
`_columnPropsEqual` and slice 21.

**Removals.** ✓, swipe-right, `m`, the context menu's "Mark as read", the
header tap, the chip, "X of Y", Fresh / History and their folds, the unread
ring, `lastViewed` writes, `toggleEntryRead`, the hide toggle's mark-read.

**Hook points left for later units.** PR3c: Column's `setCardRef` is where
`trackSeen` registers unseen cards; `addSeenMarks(entries)` in App is the
one path seen marks take, which PR3c's collector calls; `useFeedOrder`
keeps `setHold`. PR4c / PR4d: the keep set, `order` (window) and the
partition counts within the window. PR5b: `isRemoved` (muted feeds). PR8a:
`resetSnapshot(key)` for a custom tab's feed edit. PR6b: the manual refresh
toast is the one PR6b replaces with its status row.

**Tests.** `feedOrder.test.ts` (each partition rule, the reference rules,
the generation, the order reset, D33, `nextTab`); slice 21; the flows and
smoke states the spec lists; the harness stubs `<a target="_blank">` clicks
(nothing leaves the page, as with `window.open`).

**Risks checked in the plan.** A category longer than 55 characters can't
take a `seen:` key (PR2a's 60-character limit). `sources.category` has
been `VARCHAR(40)` since migration 0001 (`backend/app/models.py:49`,
`alembic/versions/0001_initial.py:34`), so no category in use can be
longer than 40 and every `seen:` key fits (45 at most). No VPS query is
needed.

## What changed

Opening a story now uses it up. Each tab shows the stories not seen yet,
then "You're all caught up" (or "N not seen · Mark as read"), then the seen
ones folded under "Earlier (N)". The list stays still while you read, and
the tab badges count what isn't seen yet. ✓, swipe-right, `m`, the context
menu's "Mark as read", the header tap, the "+N new" chip and Fresh /
History are gone, so a story is used up only by opening it or by Mark all
(PR3c adds scrolling past it on the phone).

- `frontend/src/lib/feedOrder.ts` (new, pure, no imports): `partitionTab`
  (the snapshot rules), `countUnseen`, `seenTest` (the marks plus D33's
  48-hour baseline), `seenKey`, `nextTab` (C8).
- `frontend/src/lib/useFeedOrder.ts` (new, React glue): one snapshot per
  tab in a ref; `partition(tabs, {isSeen, isRemoved, ready})` once per
  render; stable `resetSnapshot(key?)`, `setHold(key, hold)` and
  `latest(key)`. A tab not drawn in a render is reset, which is the tab
  switch's reset of both tabs. Nothing is kept until the preferences have
  answered and the first refresh is done.
- `frontend/src/lib/preferences.tsx`: `seen:*` rows decode into
  `state.seenEntries` (per category, the per-column map pattern);
  `PREFERENCE_KEYS.seen`. `setLastViewed`, `clearLastViewed` and
  `setColumnSections` went with their last caller (Reset still deletes
  those rows; they are still decoded so it can find them).
- `frontend/src/lib/storage.ts`: `STORAGE_KEYS.firstRunAt` (D33). Reset
  wipes every `popping.*` key (PR1c), this one too, so after a Reset the
  device starts like a new one: its baseline is the reload's time, and
  the `seen:*` rows are gone with it.
- `frontend/src/lib/useUpdateCheck.ts`: `useUpdateCheck(isDesktop,
  onReturn)`. Every return starts PR1r's check first (only in a production
  build, as before), then reports `{away, openedBefore}` to `onReturn`.
  The listeners now attach in a dev build too (the check doesn't run
  there). `OPEN_GUARD_MS` is exported.
- `frontend/src/App.tsx`:
  - `seenIds` (every `seen:*` and legacy `read_entries:*` row) and
    `isSeen` (with D33's `firstRunAt`) replace `globalReadIds`;
    `newCountByColumn`, `unreadIdsByColumn` and `sectionsByColumn` go,
    and `useFeedOrder` partitions the drawn tabs (the phone's tab; on the
    desktop every grid column, and the hidden phone tab). Saved isn't
    partitioned: it lists every save above no marker.
  - `isRemoved`: hidden, or from a feed no longer in `sources` (PR5b adds
    muted feeds).
  - `countFor(col)`: the partition's count for a drawn tab, `countUnseen`
    of its stories otherwise; the badge (`99+`, `aria-label="<tab>, N
    new"`, none on Saved), the marker, the md+ "N new" and C8 use it.
  - `addSeenMarks(stories)`: the one path seen marks take, grouped by
    `seen:<category>`, one `addIds` per key with the keep set (entries,
    For You, saves). `openEntry(entry)` calls it synchronously and stamps
    `noteStoryOpened()`. Both Columns, SearchResults and the desktop For
    You row pass it as `onActivate`.
  - `markColumnRead(key)` is Mark all: the partition's `unseenIds`,
    marked, the tab reset, "Marked N as read" with Undo;
    `unmarkColumnRead(key, added)` removes exactly those per key and
    resets the tab.
  - C1: `onReturn` resets every tab and scrolls the phone list and the
    desktop wrapper to the top after `RETURN_RESET_MS` (10 min) away,
    unless the page hid within 2 s of an open; the refresh that return
    starts resets the tabs again when it lands, unless the user did
    anything first (review fix 2).
  - A refresh that isn't silent (Refresh, `r`) resets every tab when its
    stories land and toasts "N new" (ids held before the request) or
    "Nothing new". Polls never reset.
  - C8: `nextTabFor(key)` walks `columns` (`orderColumns().shown`), one
    cached object per tab while its value holds; `openTab` jumps there
    like the Menu's Jump to column.
  - The arrow keys walk the partition (unseen, then Earlier).
    `dropSelection(id)` (stable) clears the selection a Column folded
    Earlier over (re-check round 1).
  - Gone: `HIDDEN_RESET_MS`, `readEntriesKey`, `MAX_SEEN_ENTRY_IDS`,
    `capSetFIFO`, `seenEntryIdsRef`, `hiddenAtRef`, the section-fold
    types, refs and handlers (`setColumnSection`, `sectionsCollapsedFor`),
    `lastViewedRef` and the `lastViewed` write, `markEntryRead`,
    `unmarkEntryRead`, `toggleEntryRead`, the `m` key, `manualRefresh`,
    the columns memo's `totalCount`. `toggleHideEntry(id)` no longer
    marks the story read.
  - Reset deletes `seen:*` rows too, and drops their queued writes.
- `frontend/src/components/Column.tsx`: props `sections` (`{unseen,
  earlier}`), `unseenCount`, `nextTab`, `generation`, `onOpenEntry`,
  `onMarkAll`, `onOpenTab`, `onHoldChange`, `onSelectionFolded`; gone `newCount`,
  `totalCount`, `sectionsCollapsed`, `onToggleSection`, `onMarkRead`,
  `onMarkEntryRead`, `onRefresh`. The header is the label, "N new" on md+
  (`data-new-count`) and ⋯, whose first row is Mark all as read
  (`data-mark-all`, 44px, disabled at 0, absent on Saved). The list:
  unseen cards 30 at a time, then the `[data-caught-up]` row, C8's
  `[data-next-tab]` row (caught up only), the folded
  `button[data-earlier][aria-expanded]`, and Earlier's dimmed cards 30 at
  a time. C8's row is named like the tab in the strip ("News, 12 new").
  Each card run is a `div[data-group]`. Both limits and
  `earlierOpen` reset on `generation`; Earlier opens when the keyboard
  selection moves into it, and the limits grow to reach it; folded with
  the selection inside (its button, or a new snapshot), it drops the
  selection through `onSelectionFolded` (re-check round 1). An
  IntersectionObserver on the marker (root `sentinelRoot`), made again
  for every snapshot (re-check round 1), reports whether it is above the
  screen; meanwhile the rows under the marker stay as drawn (review fix
  1).
  `ColumnSection` became `CardList`.
- `frontend/src/components/Card.tsx`: ✓ and `CheckIcon`, the right-swipe
  reveal and commit, `commitMarkRead`, `mountTime`, the context menu's
  "Mark as read", the unread ring and `onMarkRead` are gone; `unread` is
  `dimmed`. `onActivate` is called by the headline, the Thumbnail
  (`onOpen`), the Reddit-thread link, Listen and the context menu's "Open
  in new tab".
- `frontend/src/components/SearchResults.tsx`: `onOpenEntry` replaces
  `readIds` / `onMarkRead`.
- `frontend/src/components/ShortcutsSheet.tsx`: the `m` row is gone.
- `backend/tests/test_slice21_column_memo.py`: the comparator list drops
  `newCount`, `totalCount`, `sectionsCollapsed` and adds `unseenCount`,
  `nextTab`, `generation`; HANDLERS drop `setColumnSection` and
  `sectionsCollapsedFor`; `test_sections_collapsed_uses_memo_cache` is
  deleted.
- Tests and harness: `frontend/scripts/unit/feedOrder.test.ts`; the flows
  and smoke states below; the flow harness keeps a new-tab link's click on
  the page (recorded in `opened`); `docs/DEVICE_CHECKS.md`;
  `frontend/scripts/iphone-audit/README.md`.

## Deviations from the spec

- **`onOpenEntry` takes the Entry, not the id.** The mark goes under the
  story's category, so App needs the story's feed; with the Entry in
  hand there is no id lookup across tabs, For You and search.
- **C1 runs from the update check's return report.** `useUpdateCheck`
  already stamps the hide and measures time away; it now hands every
  return to App (`onReturn`) after starting its check, which is what
  "run PR1r's update check first" asks. App's `hiddenAtRef` went with its
  last reader, so there is one hide stamp for both rules. Beyond the
  spec: the refresh the return starts resets the tabs once more when it
  lands (reset only before it, the stories that came in while away would
  be appended at the end of the unseen ones, not sorted first, which C1
  promises); the desktop wrapper scrolls to the top too; the open guard
  applies on both layouts (PR1r's guard stays phone-only for the reload).
  That second reset is called off once the user does anything after the
  return (review fix 2): a touch, click, key or wheel, a list scrolled
  off its top, or a story opened by any path. (`click` itself joined the
  listeners in re-check round 2; before that a click with no pointer or
  touch event, as a desktop screen reader in Firefox sends, was missed.) The spec's "Polls never
  reset it" and D3 ("it stays where it is until you leave the tab") then
  hold: the late refresh's stories append or wait like a poll's.
- **A Sort change resets through the snapshot's `order`**, not a
  `resetSnapshot` call: the tab's order string (its sort and its "Hide if
  older than", the window until PR4d) is part of the snapshot, so a sort
  changed on another device resets too. The source filter's column adds
  the filter and the scope its fetch loaded to its order: it is another
  tab per filter, and until the filter's own fetch lands it shows that
  filter's stories from the unfiltered fetch (`flow-filter-one-feed-status`
  showed the difference).
- **The tab-switch reset is the hook's**: a tab not drawn in a render is
  reset, so a switch resets the tab left and the tab opened without App
  tracking switches.
- **An arrival already seen goes at the end of Earlier**, not the end of
  the unseen ones: it isn't new (marked on another device first).
- **C8's row shows only under "You're all caught up"**, as C8 words it
  ("under a caught-up tab's marker"); under "N not seen · Mark as read"
  there is no next tab yet. On the desktop it scrolls to that column.
  So that this choice never moves the list (review fix 1), the marker's
  form and C8's row stay as last drawn while the marker is above the
  screen, and catch up when it is back on screen or the tab starts over.
- **Earlier and the keyboard selection.** The spec has Column open
  Earlier "itself when `selectedId` is in it" and fold it again "on every
  snapshot", which conflict when a snapshot starts over with the
  selection inside. Earlier opens when the selection moves into it
  (review fix 4), every new snapshot folds it, and a fold with the
  selection inside (the Earlier button, or a new snapshot: Refresh, Mark
  all, its Undo, C1) drops the selection, so no shortcut acts on a card
  that isn't drawn and the next arrow starts at the column's first card
  (re-check round 1). A selection that a reset moves into Earlier (Mark
  all with an unseen card selected) still opens it.
- **Saved has no read model**: no partition, no marker, no Mark all row,
  no badge, no dimming ("Saved has no marker").
- **A hide no longer marks the story read** (the eye button and `h` did);
  the spec's removal list doesn't name it, but after PR3b a story is used
  up only by opening it or by Mark all, and Hide's Undo now puts it back in
  its place.
- **`markColumnRead` keeps its name** (slice 21 pins it); it is Mark all.
- **Mark all's Undo shows no second toast** ("Column restored." went).
- The md+ "N new" is CSS (`hidden md:inline`): the spec says "on md+",
  and Column doesn't know the layout.
- **The longest category was checked in the schema, not on the VPS.** The
  spec's Risks ask to check the longest category in use before merging;
  `sources.category` is `VARCHAR(40)` since migration 0001, so every
  `seen:` key is 45 characters at most and no device-check query is
  needed.
- `flow-foryou-fails-keeps-tab` was rewritten: a failed For You poll now
  leaves its stories in the tab until it starts over (the "a poll drops a
  story" rule), so the flow checks they stay, then switches away and back
  to see the empty tab and the phone still on it.
- `flow-scroller-rotate` lost its `column_sections:nhl` seed: it kept the
  phone Column's memo holding across the flip, which it now does anyway.

## Tests

- Unit `frontend/scripts/unit/feedOrder.test.ts` (18 cases): a new
  snapshot (unseen, then Earlier, each in the tab's order); a story seen
  after it keeps its group and place and stops counting; arrivals at the
  end of the unseen ones; an arrival already seen at the end of Earlier;
  arrivals held (and counted) while the marker is above the screen, then
  let go in arrival order; a poll-dropped story stays from its kept copy
  until the next snapshot; removed stories leave at once (cached ones too)
  and come back in place; a hidden or deleted snapshot story leaves while
  a poll-dropped one stays; a removed story leaves (and stops counting)
  even while the input still has it; unchanged input returns the same
  arrays; one updated story comes back in a new array; the generation
  goes up on a reset and only then; a reset re-sorts; an order change
  (sort, and the window as PR4d will encode it) starts a new snapshot;
  D33; `countUnseen`; `seenKey`; `nextTab`.
- pytest `test_slice21_column_memo.py`: `[unseenCount]`, `[nextTab]` and
  `[generation]` replace `[newCount]`, `[totalCount]` and
  `[sectionsCollapsed]`; HANDLERS drop `setColumnSection` and
  `sectionsCollapsedFor`; `test_sections_collapsed_uses_memo_cache` is
  deleted.
- New flows (390 unless said; the README has each step): 
  `flow-open-marks-seen` (a Column, search, the desktop For You row, and
  the keepalive PATCH when the page hides straight after an open; a
  middle-click on the headline, and from re-check round 1 on the
  Reddit-thread link and Listen),
  `flow-badge-cap` (99+, the tab's name with the full count, none at 0),
  `flow-caught-up` (from re-check round 3 the tap on the end row leaves
  the focus alone), `flow-next-tab` (C8, the row's name, a hidden tab
  never named),
  `flow-mark-all-read` (from re-check round 3 its `marked` screenshot
  after the keyboard Mark all, not before the Undo tap), `flow-new-arrival-appends` (append, and hold
  while the marker is above the screen), `flow-return-after-10-min` (C1:
  9 min nothing; 11 min reset, the arrival first and the list at its top;
  an open straight before leaving: nothing; from the review fixes, an open
  or a scroll before a slow return's refresh lands: nothing moves; from
  re-check round 1, a finger resting on the list too, and the first
  return's refresh held until the return's own scroll event; from
  re-check round 2, a click alone opening Earlier, a bare key and a
  wheel turn, the open made a dispatched middle-click so only the open
  guards keep it, and the scroll case waiting for its scroll event),
  `flow-refresh-resets` (a poll appends without a toast; Refresh starts
  the tab over, "1 new" then "Nothing new", and folds an open Earlier;
  from re-check round 1, a Refresh past the old batch draws the first 30
  and the sentinel; from re-check round 2, the same on NHL when the old
  limit already reaches the new snapshot's unseen stories),
  and from the review fixes `flow-arrival-batch-edge` (an arrival past a
  whole batch never takes the marker or Earlier off the page) and
  `flow-earlier-keys-fold` (1280: Earlier folds with the keyboard
  selection inside, on `r` and with its button, and the selection goes
  with it; from re-check round 2 an `h` sent in the same task as the
  button's fold), and from re-check round 1 `flow-return-from-earlier`
  (C1 from deep in Earlier: the hold is let go, the end row follows the
  opens and an arrival goes in at once).
- Changed flows: `flow-auto-append` ends with the marker;
  `flow-desktop-layout` adds a Mark all step (and from re-check round 2
  News's end row pressed with the keyboard far down the page; from
  re-check round 3 Deals's end row clicked with the mouse there, and
  Security's pressed the way NVDA or JAWS in Firefox press it); `flow-reset-keeps-saves`
  seeds `seen:news` and opens a story where it tapped ✓;
  `flow-foryou-fails-keeps-tab` and `flow-scroller-rotate` as under
  Deviations. FN's toast regex is `/^(\d+ new|Nothing new)$/`, looked for
  in the toast (`[role="status"]`) only, since the md+ header's "N new"
  matches it too. `flow-sort-best` and FM pass unchanged.
- Harness: a click on a new-tab link (`a[target="_blank"]`) that the app
  didn't handle itself is recorded in `opened` and kept on the page
  (bubble phase), so opening a story leaves the flow on Popping.
  Helpers: `withArrivals`, `arrivalNhl`, `groupIds`, `markerText`,
  `badge`, `textNow`, `openStory`, `seenAdded`, `seenRemoved`, from
  re-check round 1 `restFinger`, and from re-check round 2 `markerAbove`
  and `scrolledTo`.
- Smoke states (390 and 320): `caught-up`, `not-seen-row`,
  `earlier-open`: rows 44px, the marker not a status region and not a
  nested `<section>`, Earlier's cards at opacity 0.6.

## Verification

Local loop on `stack-pr3b`, based on origin/main `e1f0390` (PR3a #43;
origin/main had not moved at commit time), with the frontend's
`node_modules` linked in, Postgres 16 database `popping_test_f`, and
`vite preview` on 127.0.0.1:4173.

- **No mutant left behind.** The work resumed after a container restart in
  the middle of a mutation run. Before anything else, every original line
  of the four mutation batches (45 edits) was found exactly once in the
  tree, and `Column.tsx` matched the last batch's backup copy byte for
  byte.
- **Review of the WIP against the spec.** It turned up, and this pass
  fixed: a unit mutant that had survived (a removed story drawn while the
  input still has it; new case 9); three placeholder rows in the mutation
  table, filled from a re-run of that batch; C8's row read as "Podcast dot
  2" to VoiceOver (it now has the tab strip's name, "Podcast, 2 new", and
  `flow-next-tab` checks it); a device check that said no badge reads 99+,
  which a busy tab can rightly show (reworded to what D33 promises); the
  category-length VPS query (not needed, see Deviations); and comments
  that still named ✓, the "+N new" chip or the "refreshed" toast.
- `npm run lint`: 0 errors (the 4 react-refresh warnings main has).
  `npm run build` (tsc, then vite): clean. `git diff --exit-code
  package.json package-lock.json`: clean.
- Unit tests (`node --experimental-strip-types --test
  'scripts/unit/*.test.ts'`): 193 of 193 pass (`feedOrder.test.ts`: 18).
- Backend pytest, the whole suite: 1712 passed, 13 failed; the 13 are
  exactly the DNS-only failures listed in the container's baseline. The
  ten tests that read frontend source (slice 21, slice 19, the feed editor
  and preference contracts, and the rest that open `frontend/`), run
  again after the last edit: 210 passed.
- Strict smoke (`smoke.cjs --csp --strict`), every state and flow, on the
  final build: 0 failures, 0 warnings (169 runs, 421 s). The same run on
  the WIP build before this pass's edits: 0 failures (419 s).
- The PR3b flows and states with every flow this unit changed or leans on
  (`flow-open-marks-seen`, `flow-badge-cap`, `flow-caught-up`,
  `flow-next-tab`, `flow-mark-all-read`, `flow-new-arrival-appends`,
  `flow-return-after-10-min`, `flow-refresh-resets`, `flow-auto-append`,
  `flow-desktop-layout`, `flow-reset-keeps-saves`,
  `flow-foryou-fails-keeps-tab`, `flow-scroller-rotate`,
  `flow-fn-quiet-poll`, `flow-sort-best`, `flow-filter-one-feed-status`,
  `flow-desktop-keys-past-batch`, and `caught-up`, `not-seen-row`,
  `earlier-open` at 390 and 320): 23 runs, looped 5 times, 0 failures
  each time (74 to 76 s a loop).
- Screenshots (`--out`, not committed) of `caught-up`, `not-seen-row`,
  `earlier-open` at 390 and 320 and `flow-next-tab`'s row: the marker,
  C8's row and "Earlier (N)" close the list as one end, and Earlier's
  cards are dimmed.
- Security checklist: no route, fetch or server-side read changes. The
  new `seen:*` rows go only through PR2b's PATCH (`addIds` /
  `removeIds`) under PR2a's allowlisted prefix, and are read back through
  `coerceIds`.
- WebKit results come only from CI.

### Mutation checks (test → broken line → failing rule)

Each was a scratch edit, then a rebuild, `--only` on the flows (or the
unit file, or the pytest file), a restore from a backup copy and a byte
compare. Every one failed where the table says (a unit test by its place
in `feedOrder.test.ts`).

| Test | Broken line | Fails on |
|---|---|---|
| `flow-open-marks-seen` | Column's cards get no `onActivate` | OPEN-SEEN: "opening story 101 sent no PATCH adding it to seen:news" |
| `flow-open-marks-seen` | SearchResults' cards get no `onActivate` | OPEN-SEEN: "opening a search result sent no PATCH adding 601 to seen:nhl" |
| `flow-open-marks-seen` | the desktop For You row's cards get no `onActivate` | OPEN-SEEN: "opening a card in the desktop For You row sent no PATCH adding 301 to seen:security" |
| `flow-open-marks-seen` | the open's mark deferred by 1.5 s | OPEN-SEEN: "hiding right after an open sent no keepalive PATCH of seen:news"; "story 102 ... never reached the store" |
| `flow-open-marks-seen`, `flow-caught-up` | the count ignores seen marks | OPEN-SEEN: the badge reads "12", expected "11"; CAUGHT-UP: the end row reads "40 not seen" after two opens |
| `flow-open-marks-seen`, `flow-next-tab` | the badge counts every story | OPEN-SEEN: badge "12"; NEXT-TAB: no next-tab row under the caught-up NHL tab |
| `flow-badge-cap` | `{count > 99 ? '99+' : count}` → `{count}` | BADGE: "the NHL tab's badge reads "103" with 103 unseen, expected "99+"" |
| `flow-badge-cap` | the tab's `aria-label` dropped | BADGE: "the NHL tab is named null, expected "NHL, 103 new"" |
| `flow-caught-up` | the end row's tap does nothing | CAUGHT-UP: "after Mark as read 40 stories are still above the marker" |
| `flow-caught-up` | C8's row shown while stories are unseen | CAUGHT-UP: "C8's row shows while stories are unseen" |
| `flow-next-tab` | C8's row without its `aria-label` | NEXT-TAB: "the row is named null, expected "Podcast, 2 new"" |
| `flow-next-tab` | `nextTab` walks the hidden tabs too | NEXT-TAB: "with Podcast hidden the row reads "Podcast · 2", expected "Security · 9"" |
| `flow-caught-up`, `flow-mark-all-read` | Undo doesn't reset the tab | CAUGHT-UP: "Undo brought nothing back above the marker"; MARK-ALL: after Undo nothing above the marker, Earlier still there |
| `flow-mark-all-read`, `flow-desktop-layout` | Mark all doesn't reset the tab | MARK-ALL: "Mark all left stories above the marker"; DESK-LAYOUT: "the NHL column still draws 60 cards above the marker" |
| `flow-new-arrival-appends` | arrivals go first | ARRIVAL: above the marker `[7901,601,602]`, expected the newest at the end |
| `flow-new-arrival-appends` | App ignores the hold | ARRIVAL: "with the marker above the screen the arrival went in" |
| `flow-return-after-10-min` | the open guard ignored | RETURN: "back 11 min after opening a story the list moved"; "is at 0, expected 400" |
| `flow-return-after-10-min` | `RETURN_RESET_MS` 2 min | RETURN: "back after 9 min the list moved" |
| `flow-return-after-10-min` | no reset on return, none when its refresh lands | RETURN: the arrival is last, not first; "the opened story is not folded under Earlier (1)" |
| `flow-return-after-10-min` | no second reset when the return's refresh lands | RETURN: `[102,…,112,7951]`, expected the arrival first |
| `flow-return-after-10-min` | no scroll to the top | RETURN: "the list is at 232, expected its top" |
| `flow-refresh-resets` | Refresh doesn't reset | REFRESH: the new stories at the end, not first; Earlier empty |
| `flow-foryou-fails-keeps-tab` | a poll-dropped story leaves at once | FY-FAIL: "the failed poll changed For You from 8 stories to 0" |
| `flow-foryou-fails-keeps-tab` | a tab switch resets nothing (the hook) | FY-FAIL: "For You kept its stories after its request failed and the tab started over" |
| `flow-filter-one-feed-status` | the filter column's order without the filter and its scope | FILTER-STATUS: the empty one-feed filter shows the unfiltered fetch's stories, not the feed's error |
| smoke `caught-up` | the marker not rendered | SETUP: the state can't be reached |
| smoke `not-seen-row` | the marker row `role="status"` | READ-MODEL: "is a status region" |
| smoke `earlier-open` | Earlier's cards not dimmed | READ-MODEL: opacity `["1","1"]`, expected 0.6 |
| unit | no reuse of the previous arrays | 10 (unchanged input) |
| unit | D33 off | 15 |
| unit | an order change keeps the snapshot | 14 |
| unit | `nextTab` doesn't wrap | 18 |
| unit | the hold ignored | 5 |
| unit | an arrival already seen goes into the unseen ones | 4 |
| unit | the generation not bumped | 12, 14 |
| unit | removed stories drawn once the input dropped them (kept copies) | 7, 8 |
| unit | removed stories drawn while the input still has them | 9 (this case was added after an earlier run left the mutant alive) |
| unit | removed stories still counted | 7, 9 |
| pytest | the comparator ignores `generation` / `unseenCount` / `nextTab` | `test_column_comparator_checks_prop[generation]` / `[unseenCount]` / `[nextTab]` (each run alone) |
| `flow-new-arrival-appends` (review fix 1) | `keepEnd` always false: the marker's form and C8's row follow the count and the next tab while the marker is above the screen | ARRIVAL: "with the marker above the screen a News story arrived: the Earlier card being read moved from 341 to 393", "C8's row came in"; for the NHL arrival "moved from 207 to 155", "C8's row went" |
| `flow-return-after-10-min` (review fix 2) | the second reset ignores what was done since the return (no input or scroll listeners, no disarm in `openEntry`, `afterReturn = armed`) | RETURN: "story 7951, opened before the return's refresh landed, moved: the list starts [7952,102,104]"; "the opened story went under Earlier: "Earlier (3)", was "Earlier (2)""; the scroll case too |
| `flow-return-after-10-min` (review fix 2) | no scroll listener (a list scrolled with no touch doesn't call it off) | RETURN: "scrolled before the return's refresh landed, the list became [7953,7952,102,…], expected [7952,102,…,7953]"; "the list is at 486" |
| `flow-open-marks-seen` (review fix 3) | the headline's `onAuxClick` dropped | OPEN-SEEN: "a middle-click on story 103's headline sent no PATCH adding it to seen:news"; the badge reads "11", expected "10" |
| `flow-earlier-keys-fold` (review fix 4) | Earlier opened on every render while the selection is in it (`!earlierOpen` for `selectedId !== openedFor`) | EARLIER-KEYS: "with the selection inside, the Earlier button left it expanded true with 2 cards drawn" |
| `flow-arrival-batch-edge` (review fix 5) | no `drewAllUnseen` growth (`drewAllUnseen ? unseenTotal : 0` → `0`) | BATCH-EDGE: "the marker and Earlier left the page when the 31st story arrived" |
| `flow-caught-up` (review fix 6) | the end row's focus not handed to the marker (the effect's `focus` call removed) | CAUGHT-UP: "after Mark as read the focus is on BODY, expected the marker", and the same from the keyboard (since re-check round 3 only the keyboard step expects the hand-over; the tap step expects none) |
| `flow-mark-all-read` (review fix 6) | the ⋯ row's Mark all doesn't focus ⋯ | MARK-ALL: "after Mark all the focus is on BODY, expected ⋯", and the same from the keyboard |
| `flow-refresh-resets`, `flow-caught-up` (review fix 8) | `setEarlierOpen(false)` deleted from the generation reset | REFRESH: "after the second Refresh Earlier is expanded true with 1 cards drawn, expected it folded"; CAUGHT-UP: "after the second Mark as read Earlier is expanded true with 30 cards drawn" |
| `flow-badge-cap` (review fix 9) | `col.key === SAVED_KEY ? 0 :` dropped in `countFor` | BADGE: "the Saved tab has a badge "3", named "Saved, 3 new"" |
| `flow-badge-cap` (review fix 9) | `!isSaved` dropped at the marker guard | BADGE: "Saved has a marker", "Saved has C8's row" |

## Review fixes (second commit, "PR3b: review fixes")

The review confirmed nine findings (two were the same Earlier-fold bug).
Every one was fixed; none was skipped.

1. **A held arrival on a caught-up tab moved the Earlier card being
   read.** The count includes held arrivals (spec), so it went from 0 to
   1 and C8's row unmounted above the screen; another tab's first story
   could equally mount it there. Column now keeps its marker observer's
   answer in state (`held`) as well as reporting it to App. While it is
   true, the rows between the marker and Earlier stay as last drawn
   (`end`: the count the marker reads, and C8's tab), and they catch up
   when the marker is back on screen (the moment App lets the held
   arrivals in) or the tab starts over (a new `generation`). The
   deviation "C8's row only under You're all caught up" stays, so the
   CAUGHT-UP assertion and the badge's count are unchanged. (Re-check
   round 1: the marker's observer is now made again for every snapshot,
   so a return that jumps the marker from above the screen to below it
   lets the hold go.)
2. **C1's second reset could move a story opened right after a return.**
   The reset armed by a long return is now called off
   (`disarmReturnReset`) by any input after the return: capture
   listeners for `pointerdown`, `touchstart`, `keydown` and `wheel`, and
   a `scroll` of a `[data-scroller]` off its top (VoiceOver scrolls
   without touches; the return's own scroll to the top leaves it at 0,
   so it doesn't count), installed only while armed. `openEntry` calls
   it off too, and when the refresh lands it resets only if no story was
   opened since the return by any path (`lastStoryOpenAt()` against the
   return's stamp, which also covers Card's own `noteStoryOpened`). It
   is cleared in every case, so a called-off reset never fires later.
   The return's late stories then append or wait like a poll's.
3. **A middle-click opened a story without marking it seen.** The
   headline, the Reddit-thread link and Listen get `onAuxClick`: on
   button 1 it runs the same `noteOpen` as their `onClick` (record the
   `click`, then `onActivate`), without `preventDefault`, so the
   browser's own background-tab open goes ahead on the
   `safeExternalUrl` href. The flow harness records an `auxclick` on a
   new-tab link (`middle: true`) and keeps it on the page.
4. **"Earlier (N)" wouldn't fold with the keyboard selection inside**
   (two findings). Column opens Earlier when the selection moves into it
   (`openedFor`, cleared when the selection leaves Earlier), not on every
   render; `aria-expanded` and the drawn cards follow `earlierOpen` alone.
   The limit growth for the selection applies only while Earlier is
   open. (Re-check round 1 corrected the rest: `openedFor` is no longer
   cleared on a new snapshot, which reopened Earlier, and a fold with the
   selection inside drops the selection instead of leaving it on an
   undrawn card that `h`, `b` and `s` still acted on.)
5. **An arrival on a 30-card boundary unmounted the marker and an open
   Earlier for a frame.** Column keeps `drewAllUnseen` (set once every
   unseen card has been drawn in the generation, cleared on a new one);
   while it is set the unseen limit grows to every unseen card in the
   same render, so only a snapshot's first load goes through the
   sentinel. The render-phase adjustments now skip the pass in which a
   new snapshot resets the limits (React renders again before drawing),
   so they never act on the old snapshot's values.
6. **Both Mark-as-read controls dropped focus to `<body>`.** The caught-up
   marker is `tabIndex={-1}`. When the end row is pressed with the
   keyboard or VoiceOver (it held focus, or a click with `detail` 0;
   since re-check round 3, a click with `detail` 0, or one on the
   focused button that no pointerdown started), an
   effect moves the focus to the new marker once the tab has started
   over, only if the focus fell to `<body>`, and without scrolling
   (re-check round 2: then brought into view if it isn't all inside the
   scroller's box, which in the desktop grid it can be far from). The
   ⋯ row returns the focus to ⋯ under the same condition. No
   `role="status"` was added; strict smoke C7 still passes.
7. (Same as 4.)
8. **No flow checked that Earlier folds on a new snapshot of the same
   Column.** `flow-refresh-resets` opens "Earlier (1)" and the second
   Refresh must fold it; `flow-caught-up` opens "Earlier (2)" after
   Undo and the second Mark as read must fold it. Deleting
   `setEarlierOpen(false)` from the generation reset now fails both.
9. **The Saved checks were vacuous.** `flow-badge-cap` seeds three
   unseen saves (601-603): Saved must have no badge and no counted name,
   and once tapped no marker, C8 row, Earlier button or group, dimmed
   card, or Mark all in its ⋯. Both mutants (Saved counted in
   `countFor`; the marker drawn on Saved) now fail it.

New flows: `flow-arrival-batch-edge` (5) and `flow-earlier-keys-fold`
(4, at 1280). Extended: `flow-new-arrival-appends` (1),
`flow-return-after-10-min` (2: an open, and a scroll with no touch,
before a slow return's refresh lands), `flow-open-marks-seen` (3),
`flow-caught-up` (6, 8), `flow-mark-all-read` (6), `flow-refresh-resets`
(8), `flow-badge-cap` (9). The README describes each.
`docs/DEVICE_CHECKS.md` gains two lines for what only Safari and
VoiceOver can show: reading Earlier past the line while stories arrive,
and VoiceOver's landing after "Mark as read". Every new check
was mutation-checked (the rows marked "review fix" in the table above);
all eleven mutants failed where expected.

Verification of the review fixes, on the same base (`e1f0390`; origin/main
has since gained only `6ae69ae`, a plan-only docs commit), with the same
local loop (`popping_test_f`, `vite preview` on 127.0.0.1:4173):

- `npm run lint`: 0 errors (the same 4 react-refresh warnings).
  `npm run build`: clean. `git diff --exit-code package.json
  package-lock.json`: clean.
- Unit tests: 193 of 193 pass (no library changed).
- Backend pytest, the whole suite: 1712 passed, 13 failed, exactly the
  container's DNS-only baseline (the tests that read frontend source,
  slice 21 and slice 19 among them, pass).
- Strict smoke (`--csp --strict`), every state and flow: 0 failures, 0
  warnings (171 runs, 440 s). The focusable marker passes C7.
- The PR3b flows and states with the two new flows, every flow this
  unit changed or leans on, and PR1r's `flow-reload-on-new-build` (it
  shares the return path): 20 flows and 3 states at 390 and 320, 26
  runs, looped 5 times: 0 failures each time (113 to 117 s a loop).
- Mutation checks: eleven mutants, each a scratch edit, rebuild, `--only`
  on its flows, restore from a backup copy and a byte compare; all
  failed (table above).

## Review fixes (re-check round 1, "PR3b: re-check fixes, round 1")

The re-check confirmed seven findings. Every one was fixed; none was
skipped. Findings 2 and 3 share a cause (the keyboard selection left on an
Earlier that folded) and are fixed together.

1. **After a long return, the marker's hold stayed on with the marker
   below the screen** (minor). The return's instant jump to the top took
   a marker that was above the screen to below it with no frame in
   between, so its IntersectionObserver never called back (it reports
   crossings), and the marker element was the same one, so its effect
   never ran again: `held` and App's hold stayed true. The end row kept
   an old count (review fix 1 freezes it while `held`) and new stories
   waited, then went in above the row the moment it was scrolled to,
   pushing it off the screen. The marker's observer now also depends on
   `generation`, so every new snapshot (C1, Refresh, Mark all, Undo)
   makes a fresh one, whose first callback reports where the marker
   really is. New `flow-return-from-earlier`.
2. **Folding Earlier with the keyboard selection inside left an
   invisible selected card that `h`, `b` and `s` still acted on**
   (minor), and Enter did nothing (App's handler took it from the
   focused Earlier button). A folded Earlier now never keeps the
   selection: Column calls `onSelectionFolded(id)` when the selected card
   is in Earlier and Earlier is folded (its button, or a new snapshot),
   and App's stable `dropSelection` clears it while it is still that
   card. The call is in a layout effect: from a passive effect App's
   update waited behind the next key, and the flow showed `h` and `b`
   still acting on the card that went (the `useEffect` mutant failed 3 of
   3 runs here, but only through a timing window: re-check round 2 found
   it surviving 12 of 31 runs and made the flow send `h` in the same
   task as the fold). The next ArrowDown starts at the column's first
   card.
   `flow-earlier-keys-fold` presses `h`, `b` and `s` after the fold and
   checks that nothing was sent, toasted, hidden or expanded, then that
   Enter on the focused Earlier button opens it.
3. **A new snapshot reopened an Earlier the user had folded with the
   selection inside** (minor). The new-snapshot block no longer clears
   `openedFor`: a selection that didn't move doesn't open Earlier again,
   so every snapshot folds it, as the spec says, and fix 2 drops the
   selection. A selection a reset moves into Earlier (Mark all on the
   selected unseen card) still opens it (`openedFor` was not that card).
   With fix 2 alone the reported steps (fold, then `r`) already stay
   folded, so the flow checks the case only this fix covers: `r` with the
   selection inside an open Earlier folds it.
4. **The return flow never tested the input listeners on their own**
   (major, tests). `flow-return-after-10-min` has a third slow-return
   case: a finger resting on the top card (`restFinger`: a CDP
   `touchStart`, then a `touchCancel`, so no tap and no scroll) when the
   refresh lands. The list must be the reset one with the late story at
   its end, at its top, Earlier as it was. Case 1's open is now a
   `dispatchEvent('click')` (no pointer or touch, as VoiceOver does), so
   the open guards are tested with the input listeners in place.
   (Re-check round 2 added `click` to the listeners, so that open now
   calls the reset off by itself; case 1 became a dispatched middle-click,
   the one open that reaches `openEntry` with none of the inputs.) The two
   open guards back each other up: `openEntry` stamps the open the
   landing checks, so a story opened from a card is kept by either one,
   and only removing both fails (each alone has no visible effect on a
   card open; the landing's check alone also covers a related-coverage
   link, which stamps an open without `openEntry`).
5. **The `scrollTop > 0` exemption was tested only by a race** (major,
   tests). The gate route now holds the first 11-minute return's request
   too, and the flow lets it go only after the list has sent the scroll
   event of the return's own jump to the top (from 400), as on an iPhone,
   where the refresh always takes longer than a frame. The mutant fails 5
   of 5 runs. Each slow return now leaves with the list at 200, so its
   check that the list is at 0 on the way back proves the tab started
   over (an open guard grown to 30 s now fails the second slow return).
6. **Middle-click was tested on the headline only** (minor, tests).
   `flow-open-marks-seen` middle-clicks story 104's Reddit-thread link
   (`seen:news`, badge 9) and a podcast episode's Listen (`seen:podcast`),
   each recorded as a middle open.
7. **The `!newSnapshot` guards had no test** (minor, tests).
   `flow-refresh-resets` ends at the top of News with all 13 unseen cards
   drawn, brings 20 stories with Refresh ("20 new"), and checks the tab
   draws its first 30 and the "load more" sentinel, not all 33.

New flow: `flow-return-from-earlier` (1). Changed: `flow-earlier-keys-fold`
(2, 3), `flow-return-after-10-min` (4, 5), `flow-open-marks-seen` (6),
`flow-refresh-resets` (7). New harness helper `restFinger`. The README
describes each. `docs/DEVICE_CHECKS.md` gains a line for fix 1 (the flows
run on Chromium only, and the return's jump is the same on Safari).

### Mutation checks (re-check round 1)

Each mutant was a scratch edit in a copy of `frontend/` (the worktree
untouched), a rebuild, `vite preview` on 4173 and `--only` on its flow;
the copy was restored after each.

| Test | Broken line | Fails on |
|---|---|---|
| `flow-return-from-earlier` (1) | the marker observer's dependencies without `generation` | RETURN-EARLIER: after two opens the end row reads "7 not seen", expected 5; the arrival waits; the row reads 7, expected 6; "the end row, scrolled to at 555, moved to 741" (2 of 2 runs here; under load it survived 2 runs in 8 until re-check round 2 checked the hold before leaving) |
| `flow-earlier-keys-fold` (2) | no `onSelectionFolded` call | EARLIER-KEYS: after `r` ArrowDown selected 603 in Earlier, expected 601; with Earlier folded `h`, `b` and `s` sent `never` and `bookmark` for 603, "Earlier (1)", a toast; Enter on the focused Earlier button did nothing |
| `flow-earlier-keys-fold` (2) | the call in `useEffect`, not `useLayoutEffect` | EARLIER-KEYS: `h` and `b` sent `never` and `bookmark` for 602, "Earlier (1)", a toast (3 of 3 runs here; it survived 12 of 31 in re-check round 2, which pinned it) |
| `flow-earlier-keys-fold` (3) | `setOpenedFor(undefined)` back in the new-snapshot block | EARLIER-KEYS: "r with the selection inside left Earlier expanded true with 2 cards drawn, expected it folded" |
| `flow-earlier-keys-fold` (review fix 4, again) | Earlier opened on every render while the selection is in it | EARLIER-KEYS: `r` and the Earlier button both left it expanded, and the shortcuts acted on 603 |
| `flow-return-after-10-min` (4) | no input listeners while armed | RETURN: "with a finger on the list when the return's refresh landed, the list became [7954,7953,7952,102,…], expected [7953,7952,102,…,7954]" (2 of 2 runs) |
| `flow-return-after-10-min` (4) | neither open guard (no disarm in `openEntry`, `afterReturn` ignores opens) | RETURN: "story 7951, opened before the return's refresh landed, moved"; "Earlier (3)", was "Earlier (2)" |
| `flow-return-after-10-min` (4) | only `openEntry`'s disarm, or only the landing's open check, removed | passes (each backs the other up; see fix 4) |
| `flow-return-after-10-min` (5) | `&& t.scrollTop > 0` dropped | RETURN: "back after 11 min the stories above the marker are [102,…,112,7951], expected the arrival first" (5 of 5 runs) |
| `flow-return-after-10-min` (5) | `OPEN_GUARD_MS` 30 s | RETURN: "the second slow return did not start the tab over at its top" |
| `flow-return-after-10-min` (review fix 2, again) | no scroll listener | RETURN: the scroll case's list and position, as before |
| `flow-open-marks-seen` (6) | the Reddit-thread link's `onAuxClick` dropped | OPEN-SEEN: "a middle-click on story 104's Reddit thread sent no PATCH adding it to seen:news"; badge "10", expected "9" |
| `flow-open-marks-seen` (6) | Listen's `onAuxClick` dropped | OPEN-SEEN: "a middle-click on episode 401's Listen sent no PATCH adding it to seen:podcast" |
| `flow-refresh-resets` (7) | the three `!newSnapshot &&` guards removed | REFRESH: "after a Refresh that brought 20 to 13 unseen the tab draws 33 unseen cards without a "load more" sentinel" (2 of 2 runs; this case kills only the `needUnseen` guard: the `drewAllUnseen` one got its own case in re-check round 2) |

### Verification (re-check round 1)

On the same base (`e1f0390`), with the same local loop (`popping_test_f`,
`vite preview` on 127.0.0.1:4173, started and stopped by PID).

- `npm run lint`: 0 errors (the same 4 react-refresh warnings).
  `npm run build`: clean. `git diff --exit-code package.json
  package-lock.json`: clean.
- Unit tests: 193 of 193 pass (no library changed).
- Backend pytest, the ten tests that read frontend source (slice 21,
  slice 19, the feed editor and preference contracts, and the rest that
  open `frontend/`): 210 passed. `onSelectionFolded` is a callback, so
  slice 21's comparator list is unchanged, and it sits after
  `sourcesByName=` at both Column sites (slice 19's regex).
- Strict smoke (`--csp --strict`, fonts-ci.conf), every state and flow:
  0 failures, 0 warnings (172 runs, 487 s).
- The PR3b flows and states, every flow this unit changed or leans on,
  the new `flow-return-from-earlier`, and `flow-desktop-select-follows-tab`,
  `flow-reload-on-new-build` and `flow-reload-desktop-button` (the
  keyboard selection and the return path): 23 flows and 3 states at 390
  and 320, 29 runs, looped 3 times: 0 failures each time (162 to 164 s a
  loop).
- Mutation checks: the table above; every mutant of a fix failed its
  flow, the two single open guards pass as explained under fix 4.

## Review fixes (re-check round 2, "PR3b: re-check fixes, round 2")

The re-check confirmed nine findings: two in the app, seven in the
tests. Every one was fixed; nothing was skipped. Findings 3 and 4 share a
cause (a fixed sleep stood in for the marker observer's callback) and
share the fix. Finding 1's fix changed what case 1 of the return flow
tests, so that case changed too.

1. **C1's second reset was not called off by a click with no pointer or
   touch event** (minor). The finding modelled it as VoiceOver on an
   iPhone; the verifier showed that WebKit sends a simulated touch first
   there (React's root has touch listeners), so the real case is a
   desktop screen reader in Firefox (NVDA, JAWS), which sends only mouse
   events. Opening Earlier, the end row, C8's row, a save or a hide that
   way before a slow return's refresh landed was undone by the second
   reset. `click` joins `RETURN_INPUTS` (window, capture, passive, only
   while armed), which the comments and these notes already said it was
   in. `flow-return-after-10-min` has a fourth slow return: "Earlier (N)"
   opened with `dispatchEvent('click')` before the refresh lands; Earlier
   stays open, the late story is at the end, the list at its top. With
   `click` listened to, case 1's dispatched click on a story now calls
   the reset off by itself, so case 1 opens the story with a dispatched
   middle-click (an `auxclick`, button 1), which reaches `openEntry`
   with none of the inputs: the open guards are still tested alone (the
   "neither open guard" mutant still fails).
2. **The end row's keyboard Mark as read on the desktop left the focus
   thousands of pixels above the screen** (minor). In the desktop grid
   the other columns of a row keep it, and the page, as tall as before,
   so the shorter column's new marker could be far above the wrapper's
   view, focused with `preventScroll`: no focus to see, and the next Tab
   jumped the page. After `marker.focus({ preventScroll: true })` Column
   now calls `marker.scrollIntoView({ block: 'nearest' })` when the
   marker isn't all inside `sentinelRoot`'s box. On the phone the list
   has shrunk to the marker, so nothing moves there (`flow-caught-up`'s
   focus checks passed unchanged; re-check round 3 changed its tap
   check, since a tap no longer hands the focus over). `flow-desktop-layout` presses News's end
   row with Enter while NHL's 60 cards keep the row tall, and checks the
   focused marker is inside the wrapper's box. `docs/DEVICE_CHECKS.md`
   gains a line for it.
3. **`flow-return-from-earlier` assumed the hold was on after a fixed
   sleep** (major, tests). If the marker observer's callback hadn't come
   by the time the page hid, the hold was never on, and the flow passed
   with fix 1 of re-check round 1 reverted. The flow now waits on
   `markerAbove(list)`: an IntersectionObserver made after the scroll, on
   the same root, resolves on its first report of the marker above the
   screen. The app's observer saw the crossing in the same rendering
   update or an earlier one, so its callback runs in the same
   notification task or an earlier one: nothing the flow sends next is
   handled before it has run and the hold (a ref) is on. The `io-no-generation` mutant now fails 9 of 9 runs, three
   copies at once at load 12.
4. **`flow-new-arrival-appends` flaked under load** (major, tests): its
   `sleep(400)` was shorter than the observer's callback delay, so a poll
   landed with the hold still off. All three scrolls that put the marker
   above the screen now wait on `markerAbove`. On the same source at load
   13 with three copies at once, HEAD's flows failed 5 of 9 runs here
   and the new ones 0 of 9.
5. **`flow-return-after-10-min`'s scroll case waited a fixed 300 ms for
   the scroll event** (minor, tests). `scrolledTo(list, 300)` scrolls and
   resolves once the list's scroll event has been sent (the app's
   capture listener on the document hears it first), and only then lets
   the refresh in. In the same runs as 4, HEAD's scroll case failed 5 of
   9 and the new one 0 of 9. The finding's optional app-side change (skip
   the second reset when the scroller isn't at its top when the refresh
   lands) was not made: a finger scroll sends `touchstart`, which calls
   the reset off at once; only a scroll with no touch (VoiceOver) relies
   on the scroll event, whose window is one frame; and no flow could pin
   such a change, since the race it would cover can't be made to happen
   on purpose.
6. **The `useLayoutEffect` requirement for `onSelectionFolded` was
   pinned only by timing** (minor, tests). The flow now folds Earlier
   with `b.click()` inside one `evaluate` and sends `h` from a microtask
   queued after it: React flushes the click's update in an earlier
   microtask, and only a drop made in that flush (a layout effect, whose
   sync update and App's re-registered key listener land in the same
   flush) is in time. `b` and `s` follow with the keyboard as before.
   The `useEffect` mutant now fails 8 of 8 runs (two copies at once, load
   13); the real code passes. The round-1 notes' "3 of 3" is corrected
   above.
7. **`foldedWithout`'s "a card is still selected" check could not fail
   once Earlier was folded** (minor, tests): a folded Earlier draws none
   of its cards, so no card has `tabindex="0"` whether or not App dropped
   the selection. The check is gone; the drop is read from what the keys
   do. After `r`, the next ArrowDown selects 601 (as before); after the
   button's fold, the same-task `h`, then `b` and `s`, act on nothing,
   and after Enter has reopened Earlier the next ArrowDown selects 601
   (an ArrowDown straight after the fold would select 601 and give `h`,
   `b` and `s` a card to act on). `dropSelection`'s own-id guard
   (`cur === entryId`) stays as a guard and is noted as such in App:
   Column calls it from a layout effect of the very commit in which that
   card is selected, so no path reaches it with another selection and no
   flow can pin it (the verifier's unconditional mutant passed the three
   flows that fold or move the selection).
8. **`flow-refresh-resets` killed only the `needUnseen` guard** (minor,
   tests). Removing `!newSnapshot` from the `drewAllUnseen` line passed
   every flow. The flow now ends on NHL with 61 unseen stories, scrolled
   to its end once (60 drawn, the sentinel), 601 opened, then Refresh: the
   new snapshot's 60 unseen fit the old limit, so without the guard the
   pass that resets the limits also sets `drewAllUnseen` and the next
   render draws all 60. It must draw its first 30 and the sentinel. This
   is the finding's case (old limit at least the new total, more than one
   batch, not every unseen card drawn before) with one open in place of
   fifteen marks from another device. The `no-guard-drewall` mutant fails
   2 of 2 runs. The finding's remark that the `earlierLimit` guard can't
   change behaviour was not re-checked here; the round-1 row is reworded
   so it no longer claims each guard is covered.
9. **The `keydown` and `wheel` listeners had no test** (minor, tests).
   Two more slow returns: a bare Shift with the focus on the page, and the
   wheel turned up over the list at its top (a `wheel` event, waited for
   in the page, that scrolls nothing). Each must leave the reset list
   with the late story at its end and the list at its top. Dropping
   `keydown`, or `wheel`, from `RETURN_INPUTS` now fails its case 2 of 2
   runs.

Changed: `App.tsx` (`RETURN_INPUTS`, the arm comment, `dropSelection`'s
comment), `Column.tsx` (the end row's focus hand-over), `flows.cjs`
(`markerAbove` and `scrolledTo`; `flow-return-after-10-min`,
`flow-return-from-earlier`, `flow-new-arrival-appends`,
`flow-earlier-keys-fold`, `flow-refresh-resets`, `flow-desktop-layout`),
the README rows for those flows and helpers, `docs/DEVICE_CHECKS.md`.

### Mutation checks (re-check round 2)

Each mutant was a scratch copy of `frontend/` with one edit (the
worktree untouched), built, served on port 4173 of its own loopback
address, run with `--only` on its flow and stopped by PID. "HEAD's flows"
is the same source run with `flows.cjs` from 76f31bd.

| Test | Broken line | Fails on |
|---|---|---|
| `flow-return-after-10-min` (1) | `click` dropped from `RETURN_INPUTS` | RETURN: "with Earlier opened by a click alone before the return's refresh landed, the list became [7955,7954,…,112], expected [7954,…,112,7955]"; "Earlier … folded: expanded false with 0 cards drawn" (2 of 2) |
| `flow-return-after-10-min` (1) | neither open guard (no call-off in `openEntry`, the landing ignores opens) | RETURN: "story 7951, opened before the return's refresh landed, moved"; "Earlier (3)", was "Earlier (2)"; the late story not at the end (2 of 2) |
| `flow-desktop-layout` (2) | the `scrollIntoView` dropped | DESK-LAYOUT: "the focused marker is at -3086..-3042, outside the wrapper's 113..800" (2 of 2) |
| `flow-return-from-earlier` (3) | the marker observer's dependencies without `generation` | RETURN-EARLIER: the end row reads 7, expected 5; the arrival waits; the row reads 7, expected 6; the end row moved from 555 to 741 (9 of 9, three copies at once, load 12) |
| `flow-new-arrival-appends` (4) | App ignores the hold | ARRIVAL: "the arrival went in: [601,602,7901,7902]", and "[7912]"; the card being read moved (2 of 2) |
| `flow-new-arrival-appends` (4) | `keepEnd` always false | ARRIVAL: the Earlier card moved from 341 to 393, "C8's row came in"; moved from 207 to 155, "C8's row went" (2 of 2) |
| `flow-new-arrival-appends`, `flow-return-after-10-min` (4, 5) | none: HEAD's flows on this source | 8 of 9 runs failed at load 13, three copies at once (the arrival flow 5 of 9, the scroll case 5 of 9); the new flows 0 of 9 in the same runs |
| `flow-return-after-10-min` (5) | no scroll listener | RETURN: "scrolled before the return's refresh landed, the list became [7953,7952,…]"; "the list is at 486" (2 of 2) |
| `flow-earlier-keys-fold` (6) | the `onSelectionFolded` call in `useEffect` | EARLIER-KEYS: "h, b and s sent [{602, never}]", "Earlier (1)", a toast (8 of 8, two copies at once, load 13) |
| `flow-earlier-keys-fold` (7) | no `onSelectionFolded` call | EARLIER-KEYS: "after the fold ArrowDown selected 603 in Earlier, expected 601"; `h` and `b` acted on 603; Enter on the Earlier button did nothing (2 of 2) |
| `flow-refresh-resets` (8) | `!newSnapshot &&` dropped from the `drewAllUnseen` line | REFRESH: "after a Refresh to 60 unseen with 60 drawn before it, the NHL tab draws 60 unseen cards without a "load more" sentinel" (2 of 2) |
| `flow-return-after-10-min` (9) | `keydown` dropped from `RETURN_INPUTS` | RETURN: "with a key pressed before the return's refresh landed, the list became [7956,7955,…]" (2 of 2) |
| `flow-return-after-10-min` (9) | `wheel` dropped from `RETURN_INPUTS` | RETURN: "with the wheel turned before the return's refresh landed, the list became [7957,7956,…]" (2 of 2) |

### Verification (re-check round 2)

On the same base (`e1f0390`), with the same local loop (`popping_test_f`,
`vite preview` on port 4173, started and stopped by PID). 127.0.0.1:4173
was held by an orphaned preview left over from an earlier verifier of
this unit, which this pass did not stop, so every preview here bound
127.0.0.2:4173 (the mutants 127.0.0.3 to .6, same port).

- `npm run lint`: 0 errors (the same 4 react-refresh warnings).
  `npm run build`: clean. `git diff --exit-code package.json
  package-lock.json`: clean.
- Unit tests: 193 of 193 pass (no library changed).
- Backend pytest, the ten tests that read frontend source (slice 21,
  slice 19 and the rest): 210 passed. No prop, comparator entry or
  handler changed.
- Strict smoke (`--csp --strict`, fonts-ci.conf), every state and flow:
  1 failure in 172 runs (701 s, load about 4 to 7 from other agents):
  `flow-desktop-keys-past-batch`, "the selected card was not scrolled
  into view", a 3 s wait on the app's scroll to the keyboard selection,
  which nothing here touches. Re-run once: it passed. The only edit
  after that build was a comment in `App.tsx`; the rebuilt bundle
  differs from the tested one only in its build stamp.
- The PR3b flows and states (the 23 flows and 3 states of re-check round
  1 at 390 and 320), looped 3 times on the final build: loop 1 had 1
  failure, `flow-reset-keeps-saves` at setup ("clock.pauseAt: Cannot
  fast-forward to the past": the flow reads the page's time, then pauses
  the clock 1 s later, and load overran that second); it passed when
  re-run once. Loops 2 and 3: 0 failures (189 to 270 s a loop).
- Mutation checks: the table above; every mutant failed its flow, and
  the flows the fixes made deterministic failed every run under load.

## Review fixes (re-check round 3, "PR3b: re-check fixes, round 3")

The re-check confirmed two minor findings, one in the app and one in the
tests. Both were fixed; nothing was skipped.

1. **A mouse click on the desktop end row could jump the page thousands
   of pixels** (minor). Chrome and Edge (and Firefox on Windows and
   Linux) focus a `<button>` on mousedown, so the end row's `onClick`
   saw `e.currentTarget === document.activeElement` and took a mouse
   click for a keyboard press. Round 2's `scrollIntoView` then brought
   the new marker, far above the screen in the desktop grid, into view:
   the page moved under the pointer and the longer column next to it
   lost its place, which Undo doesn't give back. Before round 2 the same
   mistake was harmless (the focus moved with `preventScroll`). Now a
   `pointerdown` on the button records its `pointerType`
   (`endPointerRef`), and the click reads and clears it. A press counts
   as the keyboard's or a screen reader's when its click has `detail` 0
   (a key, or a reader's simulated click: VoiceOver, TalkBack, NVDA or
   JAWS in Chrome), or when it lands on the focused button and no
   pointerdown started it (NVDA and JAWS in Firefox send mouse events
   only, as round 2 found). A mouse, pen or finger press never does, so
   it neither hands the focus over nor scrolls. The finding's first
   suggestion, `:focus-visible`, was not used: a probe on a bare button
   in this Chromium showed that a button focused with Tab keeps
   `:focus-visible` through a mouse click or a tap on it, so a click
   after a Tab would still have jumped the page; and
   `matches(':focus-visible')` throws where the selector is unknown (iOS
   before 15.4). The second suggestion (the last pointerdown's type) is
   what was done.
   This changes the phone too: Chromium also focuses the button on a
   tap, so `flow-caught-up`'s tap used to hand the focus to the marker.
   A tap now leaves the focus alone, as on an iPhone, where a tap never
   focuses the button. VoiceOver on the iPhone keeps the hand-over as
   long as its double-tap reaches the page as a simulated click with
   `detail` 0, which is how WebKit sends an accessibility press; this
   container has no WebKit, so the VoiceOver device check is still the
   proof (see the risk under Verification below).
   Tests: `flow-desktop-layout` clicks Deals's end row (in NHL's grid
   row, next to News) with the mouse, scrolled to the wrapper's bottom
   edge: the wrapper must not move and the focus must not go to Deals's
   marker. After News's Enter step, Security's end row is focused and
   sent a click with `detail` 1 and no pointerdown (the press NVDA and
   JAWS make in Firefox): the focus must go to Security's marker.
   `flow-caught-up`'s tap must leave the focus off the marker; its
   keyboard step still expects the hand-over. `docs/DEVICE_CHECKS.md`
   gains a mouse line next to the keyboard one.
2. **`flow-mark-all-read` tapped Undo after its 5 s toast had gone when
   screenshots were on** (minor, tests). `shot('marked')` sat between
   the "Marked 12 as read" toast and the Undo tap, and with `--out` a
   screenshot under load takes 3 to 5 s. The shot moved to after the
   second Mark all (from the keyboard), where no Undo follows, so it
   still shows the marked tab and its toast. The `seenAdded` wait stays
   before Undo, so the add PATCH is seen before the removal. No other
   PR3b flow has a screenshot between a toast and its Undo: all of them
   pass with every screenshot slowed by 4 s (below).

Changed: `Column.tsx` (the end row's `onPointerDown`, its `onClick` test
and the comments), `flows.cjs` (`flow-desktop-layout`, `flow-caught-up`,
`flow-mark-all-read`), the README rows for those three flows,
`docs/DEVICE_CHECKS.md`, and the earlier text here that described the
old test (review fix 6, round 2's fix 2, the Tests section and the
review-fix mutation row for `flow-caught-up`).

### Mutation checks (re-check round 3)

Each mutant was a scratch copy of `frontend/` with one edit (the
worktree untouched), built, served on port 4173 of its own loopback
address, run with `--only flow-desktop-layout,flow-caught-up` and stopped
by PID. The flow check used this build with `flows.cjs` from HEAD
(1302051) in a scratch copy.

| Test | Broken line | Fails on |
|---|---|---|
| `flow-desktop-layout`, `flow-caught-up` (1) | round 2's test: `detail` 0 or the focus on the button | DESK-LAYOUT: "a mouse click on Deals's end row moved the wrapper from 4673 to 2614", "after a mouse click on Deals's end row the focus went to its marker"; CAUGHT-UP: "after a tap on Mark as read the focus went to the marker" (2 of 2) |
| `flow-desktop-layout` (1) | `detail` 0 alone (the focused-button case dropped) | DESK-LAYOUT: "after a screen reader's click on Security's end row the focus is on BODY (false), expected its marker" (2 of 2) |
| `flow-caught-up` (1) | only a mouse excluded (`pointer !== 'mouse'`) | CAUGHT-UP: "after a tap on Mark as read the focus went to the marker" (2 of 2) |
| `flow-mark-all-read` (2) | HEAD's flow (the shot before Undo), `--out` | SETUP: "locator.tap: Timeout 30000ms exceeded" in 2 of 4 runs at load 1.5 to 3; with `shot()` slowed by 4 s in a scratch `smoke.cjs`, 2 of 2. The new flow: 0 of 4, and 0 of 2 slowed |

### Verification (re-check round 3)

On the same base (`e1f0390`), with the same local loop (`popping_test_f`,
`vite preview` on port 4173, started and stopped by PID). 127.0.0.1:4173
was still held by an orphaned preview from an earlier verifier of this
unit (as in round 2), which this pass did not stop, so the final build
was served on 127.0.0.2:4173 and the mutants on 127.0.0.3 to .5 (same
port).

- `npm run lint`: 0 errors (the same 4 react-refresh warnings).
  `npm run build`: clean. `git diff --exit-code package.json
  package-lock.json`: clean.
- Unit tests: 193 of 193 pass (no library changed).
- Backend pytest, the ten tests that read frontend source (slice 21,
  slice 19 and the rest), on `popping_test_f`, run again after the last
  edit: 210 passed. No prop, comparator entry or handler changed; the
  end row's `onPointerDown` is an inline handler on its own button.
- Strict smoke (`--csp --strict`, fonts-ci.conf), every state and flow,
  on the final build: 0 failures, 0 warnings (172 runs, 564 s, load
  about 3 to 4).
- The PR3b flows and states (the 23 flows and 3 states of re-check round
  1 at 390 and 320, 29 runs), looped 3 times on the final build: 0
  failures each time (177 to 183 s a loop).
- The same 29 runs with `--out` and every screenshot slowed by 4 s (a
  scratch `smoke.cjs`): 0 failures (446 s), so no other PR3b flow waits
  on a timer across a screenshot.
- `flow-desktop-layout`, `flow-caught-up` and `flow-mark-all-read` with
  `--out` on the final flows: 0 failures.
- Mutation checks: the table above; every mutant failed its flow.
- Risk: the VoiceOver hand-over on the iPhone now rests on the click
  having `detail` 0. WebKit's accessibility press is a simulated click,
  whose `detail` is 0; if a VoiceOver double-tap ever reached the page
  as a tap with a pointerdown and `detail` 1, the focus would fall to
  `<body>` as it does after a finger's tap, and VoiceOver would not land
  on "You're all caught up". Nothing here can run WebKit, so the
  existing VoiceOver device check is what decides it.
- A pointerdown that never becomes a click (a drag off the button)
  leaves its record until the next click on the end row, which then
  counts as a pointer's press. Only a press that relies on the focused
  button (NVDA or JAWS in Firefox) could be misread that way, and then
  the focus falls to `<body>`, as with no hand-over at all.

## Open items for later units

- PR3c: register unseen cards in Column's `setCardRef` when `trackSeen`;
  drain the seen-on-scroll collector through App's `addSeenMarks`
  (grouped by category, with the keep set) and `registerBeforeFlush`;
  count the open sheets in `modalOpen`. The marker element
  (`[data-caught-up]`) and its observer are where the end rule can hook.
- PR4c: add the Saved-by-id cache to `keepIds`; Saved stays outside the
  partition.
- PR4d: put the window in the columns memo's `order` (it already holds
  "Hide if older than"), add `extraByTab` rows to `keepIds`, and count
  within the window (`countFor`).
- PR5a: the eye button and the left swipe are still Card's; "Hide" keeps
  its Undo-in-place through `isRemoved`.
- PR5b: muted feeds join `isRemoved` (category tabs, For You, search).
- PR6b: the manual refresh toast ("N new" / "Nothing new") is the one the
  status row replaces; `resetSnapshot()` on a manual check stays.
- PR7b: the tab sheet's Mark all row calls `markColumnRead(key)` (disabled
  at `countFor(col) === 0`).
- PR8a: `resetSnapshot(key)` after a custom tab's feed edit; each custom
  tab's rows join `keepIds`.
- `ARCHITECTURE.md`: no change. The snapshots are browser memory and
  `firstRunAt` is a localStorage value; nothing server-side.

## Data compatibility and rollback

- New marks go only to `seen:<category>` (PR2a's PATCH-only prefix, cap
  1000). Legacy `read_entries:*` rows are read, never written, so an old
  bundle that still PUTs them can only overwrite its own rows.
- Revert restores ✓. Marks made under `seen:*` are invisible to the old
  build, so stories used up here show as unread there until they age out.
- `firstRunAt` is per device and local; a revert leaves it unused.
- A category longer than 55 characters couldn't take a `seen:` key (PR2a
  rejects keys over 60), but `sources.category` is `VARCHAR(40)` and has
  been since migration 0001, so none is.
- As a git revert, PR3b stands alone until PR3c merges; it deploys only
  together with PR3c (D33).
