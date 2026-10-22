# PR7a: Tab order and hiding (frontend track)

An "Edit" pill at the end of the phone's tab strip, and an "Edit tabs" row in
the Menu, open a sheet where each tab has ↑, ↓, Move to top and a Show switch.
Changes apply at once and sync between devices. Brief stays first; Saved and
For You are always tabs now, and an empty one says "No stories yet". Until the
owner moves a tab, the order is the same as before (Brief, Saved, For You,
then categories by label); after a move, a new category goes at the end.

This unit continued a paused WIP commit. The WIP had the pure library, the
preference key, the allowlist entry, `{key, label}` columns, `Grouped.tsx`,
`TabsEditor.tsx`, `FirstFeedButton`, the picker order and the backend and unit
tests. It had no flows, no smoke state and no docs, and nine existing flows
failed against it. "Review of the WIP" below lists what was kept and what was
changed.

## What changed

- `frontend/src/lib/categories.ts` (pure; one value import of `columns.ts`
  with its `.ts` extension):
  - `CategoryLayout = {v: 1, order, hidden}`, `TabItem = {key, label}`.
  - `normalizeLayout(raw)`: null unless it is a version 1 object. It drops
    non-string, empty, duplicate and over-long (200) keys and Brief, and
    keeps at most 500 keys per list.
  - `fullOrder(available, layout)`: the stored order (keys of tabs that are
    gone for now are kept, so a category that empties and comes back keeps
    its place and stays hidden), then the tabs it doesn't name in the default
    order (Saved, For You, then by label). `moveKey` works on this list, and
    a move stores it.
  - `orderColumns(available, layout) → {all, shown, hidden}`: Brief first,
    then `fullOrder`. `shown` drops the hidden tabs; `all` feeds the editor.
  - `moveKey(order, key, 'up' | 'down' | 'top', present?)`: one place up or
    down past the next tab that is present (a tab that is gone for now is
    stepped over), or to the top (right after Brief). It returns a copy, and
    the copy is unchanged at the ends.
  - `categoryOptions(…, {order})`: "Your categories" follows the tab order,
    then the rest by label. Without `order`, label order as before.
- `frontend/src/lib/preferences.tsx`: `category_layout` in
  `PREFERENCE_KEYS`, decoded with `normalizeLayout` (null when absent), and
  `setCategoryLayout(layout)`, which is one `sync.put`, so PR2b's per-key write
  sequence keeps a stale GET from undoing it.
- `backend/app/routes/preferences.py`: `category_layout` on the allowlist as
  a global, non-set key (PUT only; PATCH gets 400). The server never reads
  the value.
- `frontend/src/App.tsx`:
  - `availableTabs` always holds Saved and For You (empty or not), then one
    tab per category with stories (`byCategory`'s keys, as today; PR4d adds
    the active-sources union). Each column is `{key, name, label, entries}`.
    `label` is what the tab shows. `name` is only the id its stored rows use
    (`read_entries:<name>`, `column_prefs:<name>`, …), unchanged so stored
    data stays readable by every build.
  - `tabOrder = orderColumns(availableTabs, categoryLayout)`. `shown` is the
    strip, the desktop grid, the fresh-device pick (through `resolveTab`) and
    the Jump list. Hiding a tab doesn't take its stories out of For You.
  - Every per-column map (`newCountByColumn`, `unreadIdsByColumn`,
    `sectionsByColumn`, `columnRefs`), the React keys and the For You special
    cases use `key`.
  - `keyColumns`: the columns the arrow keys walk. On the desktop these are
    the grid's columns (For You is the row above the grid, with no
    selection); on the phone, the strip's. The desktop grid renders
    `keyColumns`, and its `selectedId` compares by key. Before, the arrow keys
    indexed a list that included For You while the grid didn't, so the
    highlight could land one column off.
  - The desktop For You row renders whenever For You is shown, empty or
    not (an empty one says "No stories yet"), and carries a column ref, so
    Jump to For You always scrolls to it (review fix 3).
  - Edit pill: a 44px button after the `role="tablist"`, pinned at the
    trailing edge (the strip is `flex-1 min-w-0` and scrolls; the pill is
    `shrink-0`). A tab selected by code scrolls into the strip's view (one
    effect on the shown tab and the strip's order). Before, only Jump to
    column did that.
  - `moveTab` and `showTab` build the next layout from `latestPrefs()`, so two
    taps in one tick both count. A move stores the whole order. A Show switch
    changes only `hidden` and stores no order, so hiding alone doesn't stop
    new categories from landing by label.
  - The `columns.length === 0` "No stories yet. Tap Refresh…" branch is gone;
    the skeleton renders while `!firstRefreshDone`.
  - `noOwnFeeds` (once a refresh has brought the feed list back, every feed
    `isBuiltIn`) goes to each Column with `onAddFeed = openSettings('feeds')`
    (review fix 1).
  - Closing the editor puts focus back on the pill, or on ≡ when the editor
    was opened from the Menu.
- `frontend/src/components/Column.tsx`: `columnKey` and `label` replace `name`
  and `tabKey`; the header renders `label`; the comparator compares both, and
  `noOwnFeeds`. `EmptyColumnMessage` takes `sourceName`, the column's backing
  feed (review fix 4), and its fallback message is "No stories yet", with
  `FirstFeedButton` on an empty Saved or For You while there is no feed of
  one's own.
- `frontend/src/components/FirstFeedButton.tsx` (new): the 44px "Add your
  first feed" button, built once so PR6c's row and PR11b's retarget reuse it.
- `frontend/src/components/Grouped.tsx` (new): `GroupedSection`, `GroupedRow`
  and `ChevronRight`, moved from `Drawer.tsx`. `GroupedRow` gains
  `trailing`: with it the row is a `div` (the controls are never inside a
  button), with tighter padding and gap so a word like "Security" fits beside
  the controls at 320.
- `frontend/src/components/TabsEditor.tsx` (new): a bottom sheet
  (`role="dialog"`, `aria-modal`, `aria-label="edit tabs"`), mounted only while
  open, with a focus trap, Esc (capture phase), backdrop tap and Done to close,
  motion-safe slide and fade, `overflow-y-auto overscroll-contain` body and
  safe-area bottom padding. Brief is pinned first. Each other row has
  "Move <tab> up", "Move <tab> down", "Move <tab> to top" (44px) and a `role="switch"`
  "Show <tab>". After a move, focus goes back to the same control on the moved
  row, or to the opposite arrow when that one is now disabled. A polite
  `aria-live` line (not `role="status"`, and empty until the first move)
  reads "<tab>, <place> of <count>", counting Brief, so Move to top reads
  "…, 2 of 9". The last row is the hook point for PR8a's New tab.
- `frontend/src/components/Drawer.tsx`: imports the primitives from
  `Grouped.tsx`. It gains an "Edit tabs" row under Settings, and Jump to column
  is built from `tabOrder.shown` (Saved and For You included, the hidden tabs
  left out), calling back with the tab key.
- `CategoryPicker` takes `order`; `FeedEditor` and FeedManager's Add form pass
  `usePreferences().state.categoryLayout?.order`.
- `frontend/src/lib/columns.ts`:
  - A legacy index counts an empty Saved or For You out, as the strip that
    stored it did (those tabs used to be absent while empty).
  - `freshTabIndex` with nothing to show anywhere picks For You (an empty
    install opens on its "No stories yet" and first-feed button), or the
    first tab after Brief when For You is hidden. Before this, it picked the
    first tab after Brief, which is now always Saved.

## Review of the WIP

Kept as written: the library and its tests, the preference key and setter,
the allowlist entry and its pytest, the `{key, label}` columns, `Grouped.tsx`,
`FirstFeedButton`, the picker order, and the slice19 and slice21 edits.

Changed:
- Desktop arrow keys: the WIP compared the grid's `selectedId` by key, which
  exposed the old mismatch (the arrow keys stepped onto For You, which the
  grid doesn't render, so `flow-bookmark-on-save-desktop` failed with "ArrowDown
  selected no card"). That was fixed with `keyColumns`, as above.
- The editor's live line counted places without Brief ("News, 1 of 8" after
  Move to top). It now counts Brief, matching the spec's "second place".
- An empty install opened on an empty Saved; now it opens on For You (see
  `freshTabIndex`).
- The strip wrapper's indentation, and the trailing-row padding (at 320,
  "Security" wrapped mid-word).
- The Jump to For You on the desktop now scrolls to the For You row.
- Nine existing flows encoded "Saved and For You come and go". They were
  rewritten for always-present tabs (below), and the new flows, the smoke state
  and the docs were added.

## Tests

- Unit (`scripts/unit/categories.test.ts`): `orderColumns` with no layout and
  with one (unknown keys at the end, by label; gone keys keep their place),
  hiding (Brief never hidden or moved), `moveKey` (up, down, top, ends, a copy,
  stepping over absent keys, last tab to second place in one Move to top),
  `normalizeLayout`, `categoryOptions` with and without `order` (the
  'yours sorts by label, ignoring case' case stays green).
- Unit (`scripts/unit/columns.test.ts`): "no migration before the first
  refresh while meta tabs are present", the legacy index counting an empty
  Saved or For You out, and the empty install's fresh pick (For You).
- pytest: `test_preferences_category_layout.py` (round trip, replace, delete;
  no PATCH; no `:<id>` or look-alike forms; the frontend writes the same key).
  `test_frontend_slice19_empty_column_source_status.py` pins "No stories yet";
  `test_slice21_column_memo.py` pins `columnKey` and `label` in the comparator.
- New flows: `flow-tabs-reorder`, `flow-tabs-reorder-desktop`, `flow-tab-hide`,
  `flow-tab-hide-desktop`, `flow-new-category-at-end`,
  `flow-new-category-alpha`, `flow-saved-always`,
  `flow-fresh-device-opens-foryou`, `flow-first-feed-empty-meta`.
- Rewritten flows (Saved and For You no longer leave the strip):
  `flow-tab-stays-on-star`, `flow-unsave-last-saved`,
  `flow-foryou-fails-keeps-tab`, `flow-foryou-down-settles`,
  `flow-foryou-down-holds-fresh`, `flow-foryou-down-holds-index`,
  `flow-prefs-fail-keeps-saved`, `flow-prefs-fail-save-keeps-list`, and
  `flow-bookmark-on-save-desktop` (one ArrowRight past the empty Saved).
- FM and FU pass unchanged.
- Smoke: new `tabs-editor` state (390, 320); the edit-tabs dialog joins
  `C12_ROOTS`; `dashboard` (390, 320, 312) checks that the Edit pill is outside
  the tablist, on screen, uncovered and at least 44px tall.

## Verification

- `npm run lint`: 0 errors (the same 5 warnings as the base). `npm run build`
  passes. Unit tests: 150 of 150 pass.
- Backend pytest on `popping_test_f2`: 1246 passed and 13 failed. The 13 are
  exactly the DNS-only baseline list (`/root/popping-env/baseline.txt`).
- Full strict smoke (`--csp --strict`, CI fonts, port 4174): 144 states and
  flows, 0 failures, 0 warnings (355 s). The first run on the WIP had 9
  failures (the eight flows that expected Saved and For You to come and go,
  and `flow-bookmark-on-save-desktop`). The nine new flows and `tabs-editor`
  then passed 3 more runs in a row.
- `git diff --exit-code package.json package-lock.json`: clean.
- I looked at screenshots of `tabs-editor` at 390 and 320, `dashboard` at 312,
  the reorder flows and `flow-first-feed-empty-meta`. No PNGs are committed.

### Mutation checks (test → broken line → failing rule)

Each was a scratch edit, followed by a rebuild, `--only` on the flows, the
restore, a byte compare against the backup and a rebuild.
- `flow-tabs-reorder`, `flow-new-category-at-end`,
  `flow-fresh-device-opens-foryou` → App passes `null` instead of
  `categoryLayout` to `orderColumns` → TABS-REORDER (the strip didn't change),
  NEWCAT-END and FRESH-FY (the stored order was not applied).
- `flow-tabs-reorder` → TabsEditor's focus restore (`target.focus()`) removed
  → TABS-REORDER "after Move to top focus is on ''".
- `flow-tab-hide`, `flow-tab-hide-desktop` → `orderColumns`'s `shown` returns
  `all` → TAB-HIDE (strip and Jump list still show News), TAB-HIDE-DESK (the
  grid still shows News).
- `flow-new-category-at-end` → `fullOrder` puts the unnamed tabs first →
  NEWCAT-END "the new category is not last".
- `flow-new-category-alpha` → `fullOrder` stops sorting the unnamed tabs →
  NEWCAT-ALPHA "out of label order".
- `flow-saved-always`, `flow-unsave-last-saved`, `flow-foryou-fails-keeps-tab`
  → `availableTabs` drops empty tabs (the old rule) → SAVED-ALWAYS, UNSAVE,
  FY-FAIL.
- `flow-first-feed-empty-meta` → `noOwnFeeds` forced false → FIRST-FEED "no
  'Add your first feed' button".
- `flow-fresh-device-opens-foryou` → `freshTabIndex`'s For You preference
  removed → FRESH-FY "a new device opened on News".
- `flow-tab-hide-desktop`, `flow-bookmark-on-save-desktop` → the ArrowDown
  handler reads `columns[selectedColumnIndex]` instead of `keyColumns` →
  TAB-HIDE-DESK (nothing selected along the walk), BOOKMARK "ArrowDown selected
  no card". The first attempt mutated `keyColumns` itself to `columns`. That
  passed, because the grid then renders For You too and stays consistent. The
  flow was tightened to assert the walk column by column, and the mutation was
  moved to the handler.
- `dashboard` → the pill's wrapper given `role="tablist"` → EDIT-PILL "inside
  the tablist" at 390, 320 and 312.
- `test_category_layout_round_trips` → `category_layout` removed from the
  allowlist → 400 on the PUT.

## Deviations from the spec

- Categories still come from `byCategory`'s keys (tabs with stories), not
  from active sources: PR4d has not merged. FU's "source 15's category last in
  the Jump list" holds only after PR4d (source 15 has no stories in the
  fixtures), so FU stays as it is.
- Arrow keys, not j/k: the app has no j/k bindings, and its column walk is
  ArrowLeft / ArrowRight (the code comment calls it "j/k/o"). That walk now
  uses the shown tabs, through `keyColumns`.
- `freshTabIndex` and the legacy-index count changed in `lib/columns.ts`
  (PR1b's lib), as above. Both keep PR1b's behaviour for the cases it covered
  and follow from Saved and For You always being present.
- Stored ids (`read_entries:<name>`, `column_prefs:<name>`, …) stay keyed by
  `name`. Moving them to tab keys would orphan every stored row.
- `#36`'s pending device line about Saved moving the phone is now wrong. It
  was left as written (another unit's line), and the PR7a lines say that
  theirs replaces it.

## Review fixes (second commit, "PR7a: review fixes")

Five findings, four distinct defects (findings 1 and 4 are the same one).
All were confirmed against the code and fixed; none was skipped.

1. **"Add your first feed" after a failed first load** (findings 1 and 4).
   `noOwnFeeds` was gated on `firstRefreshDone`, which the refresh's catch
   also sets while `sources` is still its initial `[]`. So offline, or
   with /api/sources or /api/entries failing, every empty Saved or For You
   offered the button to an owner with feeds. Now a `sourcesLoaded` state is
   set only on refresh's success path, next to `setSources`, and
   `noOwnFeeds = sourcesLoaded && !sources.some((s) => !isBuiltIn(s))`.
   New flow `flow-first-feed-load-fails`.
2. **Desktop arrow-key selection held by index.** `selectedColumnIndex`
   was a plain index into `keyColumns`, so moving or hiding a tab in Edit
   tabs pointed it at another column while `selectedCardId` stayed on the
   old one: no highlight, ArrowDown jumped to the wrong column, and `m`/`h`
   wrote the card's id into the wrong column's rows. Now App keeps
   `selectedColumnKey` (null is the first column) and derives the index
   with `findIndex`. ArrowLeft/ArrowRight store the next column's key,
   ArrowDown/ArrowUp store the column they select in, and Jump to column
   sets the key. One effect replaces the length-only clamp: when the key
   leaves `keyColumns` (hidden, or out of the view), the selection moves to
   the column now at its last place (or the last column) and the card is
   dropped. Both Column call sites compare `selectedId` by that column's
   key. New flow `flow-desktop-select-follows-tab`.
3. **Jump to For You on the desktop did nothing while For You was
   empty.** The row rendered only with stories, so its ref was never
   mounted. Following the plan, the row now renders whenever For You is
   shown; empty, it says "No stories yet", with `FirstFeedButton` while
   every feed is a built-in, and its count line is left out. This removes
   the earlier deviation note ("the desktop For You row still renders only
   when For You has stories"). New flow `flow-foryou-empty-desktop`.
4. **A one-feed filter lost its slice-19 status.** `EmptyColumnMessage`
   only looked up `cat:` keys, so the filter column (`meta:filter`) showed
   "No stories yet" for a broken or never-fetched feed. `TabColumn` gains
   an optional `sourceName`: the one active source's name when the filter
   holds exactly one feed, and the raw category for `cat:` tabs (the
   lookup the slice-19 path always made). Both Column call sites pass it,
   `_columnPropsEqual` compares it, and `EmptyColumnMessage` calls
   `sourcesByName?.get(sourceName)`, falling back to "No stories yet" (plus
   `FirstFeedButton` where set). New flow `flow-filter-one-feed-status`.
   New pytest `test_empty_state_looks_up_the_columns_source_name` in the
   slice19 file; `test_slice21_column_memo.py` also pins `sourceName` and
   `noOwnFeeds` in the comparator.

### Verification of the fixes

- `npm run lint`: 0 errors (the same 5 warnings). `npm run build` passes.
  Unit tests: 150 of 150.
- Backend pytest on `popping_test_f2`: 1249 passed, 13 failed, exactly the
  DNS-only baseline list.
- Full strict smoke (`--csp --strict`, port 4174): 148 states and flows
  (the 144 before plus the four new flows), 0 failures, 0 warnings. I looked
  at the screenshots of the empty desktop For You row and the one-feed
  filter's status; no PNGs are committed.
- `git diff --exit-code package.json package-lock.json`: clean.
- Mutation checks: with the first commit's `App.tsx` and `Column.tsx`
  rebuilt, all four new flows fail (FIRST-FEED-FAIL: For You and Saved offer
  the button after the failed load; SELECT-KEY: the selection is lost after
  Move News to top and ArrowDown lands in Deals; FY-EMPTY-DESK: no For You
  row; FILTER-STATUS: "No stories yet" instead of the feed's error). With the
  fix in place but the gone-key fallback pointed at the last column instead
  of the column's last place, SELECT-KEY fails ("selected … in Video,
  expected a card in Saved"). Files were restored and byte-compared after
  each run.

## Open items for later units

- PR3c: count `tabsEditorOpen` in App's `modalOpen`. PR5a: `onKey` returns
  early while the editor is open (today the arrow keys still move the card
  selection behind it).
- PR3b: build C8's next-tab row and the badge sum on `tabOrder.shown`.
- PR4d: add the active-sources union to `availableTabs`. FU then sees
  CATEGORY_40 last in Jump to column.
- PR6c and PR11b: reuse `FirstFeedButton`.
- PR7b: add a tab sheet row that calls `openTabsEditor`. The pill returns
  focus itself, so the sheet's row needs its own return target.
- PR8a: the New tab row goes at the marked end of `TabsEditor`'s list, and
  `view:<id>` tabs join `availableTabs` with their typed name as `label`.
- `ARCHITECTURE.md`: no change. PR7a adds no process-local state; the layout
  is a preference row.

## Data compatibility and rollback

- Nothing changes until the owner edits the order. `category_layout` is
  absent until then, and the default order equals today's.
- Old bundles ignore `category_layout`. A revert drops the order, and the
  key is then never read. The allowlist entry is harmless without the
  frontend.
- From now on PR2b no longer reverts alone (Rules: revert newest-first).
