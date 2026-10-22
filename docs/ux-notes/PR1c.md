# PR1c: Newest | Best, Reset scope, bookmark on save, Brief AI row (frontend track)

A tab's sort now does what it says: Newest puts the newest stories first,
Best the highest-ranked. A saved view's chip no longer overwrites it.
Reset keeps saves, hides, votes and saved views. Saving records the ranking
signal once, and unsaving doesn't record it at all. With no AI provider set
up, the Brief buttons read "Brief needs an AI provider · Set up".

## What changed

- `frontend/src/lib/sorting.ts` (new, pure; a type import and a value import
  of `columns.ts`, both with the `.ts` extension):
  - `entryTime` (`published_at ?? fetched_at`, null when neither parses),
    `byNewest` (newest first, dated before undated, then the higher id),
    `byBest` (`composite_score` descending, then `byNewest`).
  - `sortEntries(entries, sort, key)`: Saved comes back as it came (App
    already lists it by `byNewest`); Newest
    sorts by `byNewest`; Best on For You keeps the server's order (the
    convergence boost in `routes/foryou.py` isn't sent); Best elsewhere
    (categories, the filter's column) sorts by `byBest`.
  - `defaultSort(key)`: `best` for `meta:foryou`, `newest` otherwise.
  - `normalizeSort(sort, key)`: `newest` and `best` stay; `top`, `oldest`,
    missing and unknown become the tab's default.
  - `prefsFor(columnPrefs, {key, name})`: the stored row over the tab's
    defaults, with the sort normalized. A tab with no row gets the module
    constant `BEST_PREFS` or `NEWEST_PREFS`; a complete, already-normal row
    is returned as it is; a row that needed filling in gets one cached
    object per row and default (a `WeakMap`), so Column's memo, which
    compares prefs by reference, still holds.
  - `TabPrefs` is the stored shape (`sort` gains `best`; `minScore` stays in
    the shape but nothing filters on it).
- `frontend/src/components/Column.tsx`:
  - `ColumnPrefs` is `TabPrefs`; `DEFAULT_PREFS` is gone (nothing used it
    once App reads prefs through `prefsFor`).
  - New `tabKey` prop (compared in `_columnPropsEqual`).
  - The ⋯ shows on every tab, For You included. Its Sort is a 44px
    Newest | Best segmented control (`button[data-sort]`, `aria-pressed`)
    showing `normalizeSort(prefs.sort, tabKey)`, hidden on `meta:saved`.
    The Minimum score slider is gone. The age select is unchanged.
- `frontend/src/App.tsx`:
  - `prefsFor(col)` (useCallback over `columnPrefs`) replaces
    `columnPrefs[name] ?? DEFAULT_PREFS` in the columns memo and at both
    Column sites, which also pass `tabKey`.
  - The columns memo applies the age filter, then sorts once with
    `sortEntries(…, normalizeSort(prefs.sort, key), key)`. `minScore` is
    ignored. `sectionsByColumn` no longer re-sorts its two sections.
  - `visibleStarred` (Saved's stories) sorts with `byNewest`, which is the
    order main gave Saved through `sectionsByColumn`'s re-sort: published,
    else fetched, dated before undated, then the higher id. Its old
    published-only comparator put an undated page saved today last and
    left undated saves in no fixed order.
  - `applyPreset` sets the source filter only; `savePreset` and
    `updatePreset` still capture `columnPrefs`, so stored presets keep
    their shape.
  - Keyboard `b` records `bookmark` only when it saves.
  - `resetLocalState` keeps the localStorage wipe and deletes the
    per-column `read_entries`, `last_viewed`, `column_prefs` and
    `column_sections` rows plus `history_group_by`. It no longer deletes
    `starred_entries`, `hidden_entries` or `filter_presets` (it never
    deleted `voted_entries`).
  - `llmConfigured: boolean | null`: `api.llmStatus()` once signed in, again
    when Settings goes from open to closed and when the page becomes
    visible; only the latest answer counts, and a failure is `null` (the
    normal buttons). It goes to both BriefCards with `onOpenSettings`
    (`openSettings`), and the desktop header's Brief button becomes
    "Brief needs an AI provider · Set up" (opens Settings › AI) when it is
    `false`.
- `frontend/src/components/BriefCard.tsx`: `llmConfigured` and
  `onOpenSettings` props. With `llmConfigured === false` the four buttons
  (`generate-compact`, `generate`, `regenerate-compact`, `regenerate`) are
  a `SetUpAiButton` (`data-brief-action="set-up-ai"`, 44px, calls
  `onOpenSettings('llm')`). On a phone it sits on its own row and is
  caption-sized; from `sm` up it is body-sized at the row's end, like the
  button it replaces. The collapsed no-brief strip drops "No brief
  generated yet" in that state (the row says why). The compact strips and
  the with-brief trailing row gained `flex-wrap` (row gap 0) so the row can
  drop below.
- `frontend/src/components/Card.tsx`: ☆ and the menu's Save record
  `bookmark` only when the story wasn't saved.
- `frontend/src/components/Settings.tsx`: History drops its Read group (the
  server leaves views out by default since PR1a) and its empty text says
  "Stories you hide or save show up here." Reset's copy says what it does
  ("Reset reading state": read marks, each tab's sort and filter,
  last-visit times and folded sections on all devices, and this device's
  own settings; saved and hidden stories, votes, saved views, feeds and
  briefs stay); the confirm buttons are Reset / Keep. The Settings AI tab's
  Generate brief now is unchanged.
- `frontend/src/lib/preferences.tsx`: `ColumnPrefsValue.sort` gains `best`;
  comments point at `lib/sorting`.

## Tests

- Unit `frontend/scripts/unit/sorting.test.ts` (9 tests): `entryTime`;
  `byNewest` and `byBest` with undated stories and ties; For You's Best
  keeps a boosted story with a lower raw score first (and Newest sorts it
  by time); categories and the filter re-sort on Best, Saved never;
  `defaultSort` / `normalizeSort` per key, including `top`, `oldest` and
  junk; `prefsFor`: no row → the module constants (For You Best), a window
  or sort write on For You with no row stores `best`, older `top` / `oldest`
  rows read as the default, the same row gives the same object, malformed
  fields fall back.
- Flows (`flows.cjs`, 390): `flow-sort-best`, `flow-brief-needs-ai` (its
  step 4 at 1280 with `page.setViewportSize`), `flow-bookmark-on-save`
  (☆), `flow-reset-keeps-saves`, `flow-saved-order` (an undated page
  fetched a minute ago leads Saved; saves with neither date come last,
  higher id first; the dates are dropped by an init-script fetch shim,
  because the mock is a context route a page route can't edit). At 1280:
  `flow-bookmark-on-save-desktop` (the card menu's Save for later and
  Unsave, then keyboard `b` twice). `flow-fz-reset-inline` follows the new button
  names. New helpers: `putPrefs` (writes rows into the mock's store
  through the page, then reloads), `prefWrites` (records preference PUTs
  and DELETEs), `BRIEF`.
- Smoke: `column-popover` (now with Newest | Best) and two new states,
  `brief-needs-ai` (no brief, expanded) and `brief-needs-ai-compact` (one
  brief, collapsed), at 390, 320 and 312. The two other needs-AI layouts
  are covered by the flow's height and label checks and were looked at in
  screenshots.

## Verification

- `npm run lint`: 0 errors, 5 warnings (main has 6; the
  `react-refresh/only-export-components` warning for Column's
  `DEFAULT_PREFS` export went with it). `npm run build` (tsc + vite) clean.
  Unit tests: 97 of 97 (9 new). `package.json` and the lockfile untouched.
- Full strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173):
  0 failures, 0 warnings, 337 s. The four new flows plus
  `flow-fz-reset-inline` looped 5 times: 0 failures each time.
- Backend pytest (`popping_test_f`): 1238 passed, 13 failed, the 13 being
  exactly the DNS-only baseline list. The tests that pin frontend source
  (slice19's `<Column` regex, slice21's comparator checks, the preference
  caps) pass; `tabKey={…}` contains no `>`, so slice19's regex still
  matches both Column sites.
- Screenshots looked at (not committed, not sent): `brief-needs-ai` and
  `brief-needs-ai-compact` at 390, 320 and 312 (the Set up row on its own
  line under the title, on one line at 312), `column-popover` at 390 and
  320 (Sort: Newest | Best, then Hide if older than),
  `flow-sort-best-best` (News on Best, the Morning chip above), the
  flow's no-brief state.

### Mutation checks (test → broken line → failing rule)

Each mutation was applied by a script, built, run with `--only`, and
restored with a checksum check.

| Test | Broken line | Fails on |
|---|---|---|
| `sorting.test.ts` | `sortEntries`: For You's Best early return removed | "For You keeps the server order on Best" |
| `flow-sort-best` | the same | SORT: For You shows 301,101,303,… , the server sent 101,301,102,… |
| `flow-sort-best` | Best returns the entries unsorted | SORT: after Best, News shows 101..112, expected 101,102,107,… |
| `flow-sort-best` | `applyPreset` applies `preset.columnPrefs` again | SORT: the chip wrote `column_prefs:news` `newest`; News not on Best after the chip |
| `flow-sort-best` | Sort control not hidden on Saved | SORT: Saved offers Newest \| Best |
| `sorting.test.ts` | `defaultSort` always `newest` | normalizeSort per key; three `prefsFor` tests |
| `sorting.test.ts` | `normalizeSort` keeps `top` | normalizeSort per key; two `prefsFor` tests |
| `sorting.test.ts` | `prefsFor` cache not read | "the same row gives the same object" |
| `flow-reset-keeps-saves` | `starred_entries` back in Reset's keys | RESET: Reset deleted starred_entries; no Saved tab after Reset |
| `flow-reset-keeps-saves` | `history_group_by` dropped from Reset's keys | RESET: Reset kept history_group_by |
| `flow-brief-needs-ai` | no status read when Settings closes | BRIEF-AI: Regenerate did not come back after closing Settings |
| `flow-brief-needs-ai` | no status read on coming back | BRIEF-AI: coming back did not read the AI status again |
| `flow-brief-needs-ai` | header Brief ignores `llmConfigured` | BRIEF-AI: the desktop header has no Set up |
| `flow-brief-needs-ai` | `regenerate-compact` ignores `needsAi` | BRIEF-AI: a brief, collapsed: no Set up button |
| `flow-brief-needs-ai` | Set up opens Feeds | BRIEF-AI: Set up opened Settings on "feeds" |
| `flow-bookmark-on-save` | ☆ records `bookmark` on unsave too | BOOKMARK: an unsave sent a bookmark (2 in all) |
| `flow-bookmark-on-save-desktop` | the menu's Save records `bookmark` on unsave too (Card.tsx `if (!starred)` removed) | BOOKMARK: the menu's Unsave sent a bookmark (2 in all) |
| `flow-bookmark-on-save-desktop` | the menu's Save records nothing | BOOKMARK: the menu's Save sent 0 bookmark events, expected 1 |
| `flow-bookmark-on-save-desktop` | keyboard `b` records `bookmark` on unsave too (App.tsx `if (!wasStarred)` removed) | BOOKMARK: b (unsave) sent a bookmark (2 in all) |
| `flow-saved-order` | `visibleStarred` back on the published-only comparator | SAVED-ORDER: Saved shows 103,105,110,111,112, expected 110,103,105,112,111 |

The `regenerate-compact` mutation also hit a 30 s tap timeout after its
failure; the flow now stops at the first missing Set up instead.

## Deviations from the spec

- `prefsFor(columnPrefs, tab)` takes the tab (`{key, name}`), not the key
  alone: stored rows are keyed by the column's name
  (`column_prefs:<name>`), and the filter's column name changes with the
  filter, so a key can't name its row. Keeping the row ids keeps stored
  data and older builds compatible.
- The For You ⋯ now shows (Column hid it by name). The spec's For You
  default of Best and its Newest | Best control need it.
- The desktop For You row (the card grid above the categories) has no
  header and keeps the server's order; only the phone's For You tab and
  the desktop category columns have the control.
- The AI status is fetched once signed in (`authProbed` and either a user
  or OIDC off), not on `authProbed` alone, so the sign-in page doesn't
  send a request that would 401.
- `flow-bookmark-on-save`, `flow-bookmark-on-save-desktop`,
  `flow-saved-order` and the `brief-needs-ai-compact` state are extra: the
  spec doesn't list a test for the bookmark rule or Saved's order, and the
  compact strip is where the label is tightest. The menu and keyboard
  paths are tested at 1280 because on a phone the press is the system's
  link menu, not the card menu.
- The Set up label is caption-sized on phones. At 17px in CI's DejaVu Sans
  it measures 278px, wider than the 256px the Brief row has at 312px, so it
  would wrap (C11); at 13px it is 221px.
- `DEFAULT_PREFS` was removed from Column.tsx (dead once `prefsFor` is the
  only reader).

## Open items for later units

- PR4d: the window control joins the same popover; `prefsFor` already
  merges `maxAgeHours`, and the age filter in the columns memo is where the
  window takes over.
- PR7b: the tab ⋯ sheet should use `prefsFor` and `normalizeSort` the same
  way (a spread of `prefsFor`'s result can't store `top`).
- PR8a: drop the presets' captured `columnPrefs` at conversion; nothing
  reads them now.
- PR12a: Activity covers views (the History Read group is gone).

## Revert

Revert is safe. An older bundle reads a stored `best` as oldest-first
(`sort !== 'top'` then `newest ? … : oldest`) until the device next reloads
(PR1r bounds it), and reads rows written here with `minScore` 0. A device
on a pre-PR1 bundle still deletes saves, hides and presets if Reset is
pressed there. As a git revert, PR1c stands alone until the next frontend
unit that depends on it merges.

## Review fixes (second commit)

- Saved's order: `visibleStarred` sorted by `published_at` alone, so once
  `sectionsByColumn` stopped re-sorting, an undated page saved today
  dropped to the bottom and undated saves had no fixed order (the
  comparator returned 1 both ways). It now sorts with `byNewest`, main's
  order. `sortEntries` still hands Saved back untouched and Saved still
  has no Sort control. The `lib/sorting.ts` header comment says so.
  New flow `flow-saved-order`.
- Bookmark coverage: `flow-bookmark-on-save`'s comment claimed the menu's
  Unsave, which it never tapped. The new `flow-bookmark-on-save-desktop`
  covers the menu's Save / Unsave and keyboard `b`; the shared interaction
  recorder is the `recordInteractions` / `bookmarksFor` helpers. The stray
  bookmark sentence in `flow-brief-needs-ai`'s comment is gone.
- Checks after the fixes: lint 0 errors, 5 warnings; build clean; unit
  97 of 97; the five PR1c flows touched (`flow-saved-order`,
  `flow-bookmark-on-save`, `flow-bookmark-on-save-desktop`,
  `flow-sort-best`, `flow-brief-needs-ai`) looped 5 times with 0
  failures; the four new mutations above each fail their flow; full
  strict smoke (`--csp --strict`, fonts-ci.conf, preview on 4173): 0
  failures, 0 warnings, 323 s. Backend tests that pin frontend source
  (slice19, slice21, preferences, reserved names) pass apart from the two
  DNS-only baseline failures; `package.json` and the lockfile untouched.
