# PR3a: Desktop single scroller and batching (frontend track)

On the desktop, everything under the header (Brief, the saved-view chips,
For You, FramingWatch and the category grid) now scrolls as one page. Before,
a For You row taller than the window squeezed the grid down to its padding
(32px) and the document scrolled instead. Long lists render 30 cards at a
time, and the next 30 load as you scroll near the end.

Stacked on PR7a (#42, itself on PR2b #40). Built as if both were merged.

## What changed

- `frontend/src/App.tsx`:
  - One wrapper `div` around BriefCard (desktop), PresetChips and the
    existing branch (search results, the first-load skeleton, or the For You
    row, FramingWatch, the grid and the phone `<main>`). The wrapper goes
    around the branch, not the old line range, so the ternary and the
    fragment the phone `<main>` shares stay whole.
    - Under `isDesktop` it is `flex-1 min-h-0 overflow-y-auto
      overscroll-contain` and carries `data-scroller`. App holds it in
      state (`desktopScroller`) through a callback ref.
    - On the phone it is `display: contents`: no box, so PresetChips, the
      search `<main>` and the phone `<main>` stay flex items of the app
      column, laid out exactly as before. It is always the same element,
      so crossing 768px remounts nothing.
  - On the desktop the search `<main>` drops `flex-1 overflow-y-auto
    overscroll-contain` and the grid drops `flex-1 overflow-y-auto` (it keeps
    `overflow-x-clip`). The phone keeps those classes on the search
    `<main>`. The skeleton's wrapper drops them on the desktop too (see
    Deviations).
  - Desktop Columns get `sentinelRoot={desktopScroller}`. The phone Column
    gets `sentinelRoot={phoneScroller}`, and `scrollerRef` only when
    `!isDesktop`.
- `frontend/src/components/Column.tsx`:
  - New prop `sentinelRoot?: HTMLElement | null`, in `_columnPropsEqual`.
  - `_columnPropsEqual` also compares `scrollerRef` now (review fixes,
    final round). App passes the setter only on the phone, so the value
    changes when the width crosses 768px, and the phone list has to
    re-render to move the ref and `data-scroller`. Both values are
    stable (the setter or undefined), so this costs no other render.
  - Batching: a `limit` state (30) is counted over the open sections in
    order (Fresh, then History; a collapsed section counts nothing). Only the
    first `limit` cards render. While more remain, a
    `div[data-more][aria-hidden]` sentinel (1px, never a `<section>`) ends
    the list.
  - The sentinel is observed with `root: sentinelRoot` and
    `rootMargin: '0px 0px 600px 0px'`. A hit adds 30 and disconnects that
    observer. The effect depends on `limit`, so each batch gets a fresh
    observer whose first callback reports where the sentinel is now: a
    batch that still leaves it inside the margin loads the next, and one
    that pushes it out waits for the next scroll. With no root yet (the
    first render, before App has the element) nothing more loads.
  - The keyboard-selected card always renders: when `selectedId` sits past
    the limit, the limit grows by whole batches to reach it (state set in
    render, the React "adjust state on a prop change" pattern), so App's
    scroll-into-view finds the card.
  - The History section waits until every open Fresh card has rendered, so
    its header never sits above Fresh cards still to load.
- `backend/tests/test_slice21_column_memo.py`: `sentinelRoot` and
  `scrollerRef` join the comparator parametrize list.
- `frontend/scripts/iphone-audit/flows.cjs`: helpers `moreNhl(n)` (extra
  nhl_news stories), `withStories(page, extra)` (the page's fetch appends
  them to the tab story lists, as `flow-saved-order` does, since the mock is
  a context route), `cardCount` and `sentinelBelow(column, px)`. Four new
  flows (below).
- `frontend/scripts/iphone-audit/README.md`: the new flows.

## Deviations from the spec

- **Only one `data-scroller` on the desktop needed one more change.** The
  phone `<main>` is rendered, hidden (`md:hidden`), on the desktop too, and
  PR1b gave its Column `scrollerRef`, so the hidden phone list also carried
  `data-scroller`. The phone Column now gets `scrollerRef` only when
  `!isDesktop`, so `phoneScroller` is null on the desktop, the hidden list
  has no `data-scroller`, and its sentinel is not observed (it stays at 30
  cards while hidden). Because the prop now changes with the width,
  the memo comparator compares it (see the final round below): without
  that, a phone Column whose memo holds across the flip keeps or never
  gets the ref. PR3c's `useSeenObserver(isDesktop ? null :
  phoneScroller)` and PR6b's `usePullToRefresh(phoneScroller)` see null on
  the desktop either way.
- **The skeleton's wrapper** also drops `flex-1 overflow-y-auto
  overscroll-contain` on the desktop. The spec names only the search `main`
  and the grid, but the skeleton sits inside the same wrapper, and the
  intent is one scroller.
- **The phone keeps its branch unchanged** through `display: contents`
  rather than a second copy of the branch. Under `isDesktop` the wrapper is
  exactly the spec's `div[data-scroller].flex-1.min-h-0.overflow-y-auto
  .overscroll-contain`.
- **The keyboard rule** ("the render limit includes `selectedId`'s position")
  is in PR3b's Column section, but batching breaks ArrowDown past card 30
  without it, so it lands here. PR3b keeps it for `{unseen, earlier}`.
- **A third flow**, `flow-desktop-keys-past-batch`, guards that rule.
- Nothing resets the limit yet. The phone remounts its Column per tab
  (PR1b's key), which starts it at 30. PR3b resets it on the snapshot
  `generation`.

## Tests

- New flows:
  - `flow-desktop-layout` (1280): with 25 For You stories (a row of about
    2500px) and 70 NHL stories, the only `data-scroller` is the shown
    wrapper around `main.grid`. The grid is taller than 300px, the document
    doesn't scroll, and the wrapper does. NHL renders 30 cards before any
    scroll, 60 once the wrapper is scrolled so the sentinel is 300px below
    its bottom edge, and no more without another scroll. Only the wrapper
    moved (`scrollY` stays 0).
  - `flow-auto-append` (390): the NHL tab with 70 stories renders 30, then
    60 with the list scrolled to 300px above the sentinel, no more without
    another scroll, then 70 at the end, with no sentinel left. It then
    scrolls to the end again and checks the last card is in view, so the
    `end` shot shows the end of the list.
  - `flow-scroller-rotate` (loaded at 1280, resized to 390 and back): the
    NHL tab is stored as the phone tab and `column_sections:nhl` is
    seeded, so the phone Column's memo holds across the flip (as on any
    tab whose Fresh or History was ever toggled). At 1280 only the
    wrapper carries `data-scroller`; at 390 only the phone list does, its
    sentinel loads 60, and a click on the shown tab scrolls it to the
    top; back at 1280, only the wrapper again.
  - `flow-desktop-keys-past-batch` (1280): 31 ArrowDowns in the NHL column
    select its 31st card. The column renders 60 (the batch grown to reach
    it), and the card is scrolled into view.
  - The 300px-short scroll is what makes the flows check the root and the
    margin: with the viewport as the root, or no margin, nothing loads
    until the sentinel is on screen.
- pytest: `test_slice21_column_memo.py[sentinelRoot]` and
  `[scrollerRef]`.
- No new smoke state. The existing ones (dashboard's one `main section`,
  C4, C7) pass with the sentinel and the wrapper in place.

## Verification

- `npm run lint`: 0 errors (the same 5 warnings as the base). `npm run
  build` passes. Unit tests: 150 of 150.
- `git diff --exit-code package.json package-lock.json`: clean.
- Backend pytest on `popping_test_f2`: 1250 passed and 13 failed. The 13
  are exactly the DNS-only baseline list (`/root/popping-env/baseline.txt`).
  PR7a had 1249; the one new case is the `sentinelRoot` parametrize.
- Full strict smoke (`--csp --strict`, CI fonts, port 4174): 151 states and
  flows (PR7a's 148 plus the three new flows), 0 failures, 0 warnings
  (424 s, with the other unit's smoke running on the same container). The
  three new flows, `flow-tap-tab-scrolls-top` and `dashboard` then passed
  5 runs in a row on the final build.
- Screenshots looked at (not committed): `1280-flow-desktop-layout` before
  and after the scroll (the header stays put, the For You row is followed
  by full-height columns), and `390-flow-auto-append`. The first round's
  `390-flow-auto-append-end` showed cards from the middle of the list, not
  its end (the last batch went in below the scroll position); see the
  final round.

### Mutation checks (test → broken line → failing rule)

Each was a scratch edit, then a rebuild, `--only` on the flows, a restore
from a backup copy and a byte compare (`cmp`).

| Test | Broken line | Fails on |
|---|---|---|
| `flow-desktop-layout` | the wrapper's class is `contents` on the desktop too | DESK-LAYOUT: no shown `data-scroller`; the document scrolls by 9278px; the wrapper has nothing to scroll; nothing loads at 300px |
| `flow-desktop-layout` | the old layout back (wrapper `contents`, grid `flex-1 overflow-y-auto`) | DESK-LAYOUT: "the grid is 32px tall under a 2501px For You row" (the bug), plus the scroller and document rules |
| both flows | Column's observer root `sentinelRoot` → `null` (the viewport) | AUTO-APPEND and DESK-LAYOUT: 30 cards at 300px above the end, expected 60 |
| both flows | `shown = Infinity` (no batching) | 70 cards before any scroll, expected 30 |
| both flows | the sentinel never rendered | "has no load more sentinel" |
| `flow-desktop-layout` | desktop `sentinelRoot={null}` | DESK-LAYOUT: 30 cards, expected 60 (`flow-auto-append` passes: the phone is untouched) |
| both flows | `rootMargin` `'0px'` | 30 cards at 300px above the end, expected 60 |
| `flow-auto-append` | `limit` dropped from the observer effect's dependencies | AUTO-APPEND: 60 after the second scroll, expected 70 (the desktop flow loads only one batch, so it passes) |
| `flow-desktop-keys-past-batch` | the selected-card growth removed (`shown = limit`) | KEYS-BATCH: "the selected card was not scrolled into view". The sentinel still loads the next batch while the arrow keys walk down, but only after App's scroll effect has run for the missing card |
| `test_slice21_column_memo.py` | `prev.sentinelRoot === next.sentinelRoot` → `true` | `test_column_comparator_checks_prop[sentinelRoot]` |
| `flow-scroller-rotate` | `scrollerRef` left out of `_columnPropsEqual` (this unit's first commit) | ROTATE: "after 1280 → 390 data-scroller is on [], expected only the phone list" |
| `flow-scroller-rotate` | the comparator re-renders only when `scrollerRef` is set (`=== \|\| !next.scrollerRef`) | ROTATE: "after 390 → 1280 data-scroller is on ["wrapper","phone"], expected only the wrapper" |
| `flow-auto-append` | the second `toEnd()` before the `end` shot removed | AUTO-APPEND: "the end shot does not show the last card" |
| `test_slice21_column_memo.py` | `prev.scrollerRef === next.scrollerRef` → `true` | `test_column_comparator_checks_prop[scrollerRef]` |

## Review fixes (final round)

Two findings, both confirmed by the verifier.

1. **Major: the phone Column missed its `scrollerRef` after crossing
   768px.** App passes `scrollerRef` only when `!isDesktop`, but
   `_columnPropsEqual` ignored it. On a tab with a stored
   `column_sections` row the memo held across the flip, so the list was
   not re-rendered. From 1280 to 390 (an iPhone in landscape is over
   768px, then turned upright) the list never got the ref or
   `data-scroller`: `phoneScroller` stayed null, the sentinel had no root
   (stuck at 30), and tap-to-top did nothing. From 390 to 1280 the hidden
   phone list kept a second `data-scroller`. Fix: the comparator compares
   `scrollerRef` (the setter or undefined, both stable, so it differs only
   on the flip). Column's comments now say so. Tests: `scrollerRef` in
   the slice-21 parametrize list, and the new flow `flow-scroller-rotate`
   (loads at 1280 with the NHL tab stored and `column_sections:nhl`
   seeded, resizes to 390 and back). It failed on this unit's first
   commit ("after 1280 → 390 data-scroller is on []"), and the reverse
   step failed on a comparator that re-renders only toward the phone.
   The flow clicks the tab: a resized desktop context has no touch.
2. **Minor: `flow-auto-append`'s `end` shot was not at the end.** It now
   scrolls to the end again after the last batch and checks that the
   last card is inside the list's box before the shot. Without the
   second scroll it fails "the end shot does not show the last card".
   Looked at: `390-flow-auto-append-end` shows NHL story 67, the last
   card.

Also: `docs/DEVICE_CHECKS.md` gains a rotation check (open the
home-screen app sideways on a long tab, turn it upright, scroll to the
end and tap the tab). The `scrollerRef` deviation above now names the
comparator.

Verification for this round:

- `npm run lint`: 0 errors (the same 5 warnings). `npm run build`
  passes. Unit tests: 150 of 150.
- `git diff --exit-code package.json package-lock.json`: clean.
- `test_slice21_column_memo.py` on `popping_test_f2`: 43 passed (one new
  case, `[scrollerRef]`).
- Full strict smoke (`--csp --strict`, CI fonts, port 4174): 152 states
  and flows, 0 failures, 0 warnings (401 s).
- The four PR3a flows, `flow-tap-tab-scrolls-top` and `dashboard`: 3 runs
  in a row, 0 failures.

## Open items for later units

- PR3b: `sections` becomes `{unseen, earlier}`. Batch unseen with this
  limit, put the `[data-caught-up]` row after the last unseen card once the
  sentinel is gone (`hasMore` false is the hook), batch Earlier 30 at a
  time, reset the limits on `generation`, and keep the selected-card
  growth. `flow-desktop-layout` gains the Mark all step and
  `flow-auto-append` ends with the marker.
- PR3c: the seen observer should use the same roots: `phoneScroller` on the
  phone (null on the desktop, already).
- PR8b: PresetChips leave the wrapper with the chips.
- `ARCHITECTURE.md`: no change. The batch limit is component state, not
  process-local state.

## Data compatibility and rollback

- No stored data changes. Revert is safe: the old build renders every card
  and the old desktop layout.
