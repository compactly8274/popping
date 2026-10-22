import { memo, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import type { Entry } from '../api'
import { Card } from './Card'
import { FORYOU_KEY, SAVED_KEY, type ColKey } from '../lib/columns'
import type { TabCount } from '../lib/feedOrder'
import type { SeenRegistry } from '../lib/seen'
import { FirstFeedButton } from './FirstFeedButton'
import { normalizeSort, type Sort, type TabPrefs } from '../lib/sorting'
import { WINDOWS, widerWindow, type Window } from '../lib/window'

// Per-column sort/filter preferences. Owned by App (so it persists
// across columns and across renders) and passed in here for the
// popover to read/write. The shape is ``lib/sorting``'s ``TabPrefs``
// (``sort`` may still hold an older build's ``top`` or ``oldest``;
// ``normalizeSort`` reads it; ``window`` is the tab's window, PR4d).
// ``minScore`` stays in the stored shape but nothing filters on it any
// more.
//
// App reads a tab's prefs through ``prefsFor`` (lib/sorting), which
// returns module constants for a tab with no stored row, because the
// memo below compares prefs by reference.
export type ColumnPrefs = TabPrefs

const SORTS: ReadonlyArray<{ value: Sort; label: string }> = [
  { value: 'newest', label: 'Newest' },
  { value: 'best', label: 'Best' },
]

// What an empty category tab says at each window (D5), as "<said> ·
// <offer>": the offer sets the tab's window to the next one up
// (``widerWindow``). Nothing at All: that is the end.
const EMPTY_WINDOW_LINES: Record<Window, string | null> = {
  '1d': 'Nothing in 1 day · Show 7 days',
  '2d': 'Nothing in 2 days · Show 7 days',
  '7d': 'Nothing in 7 days · Show all',
  all: null,
}

type Props = {
  // The tab's key (lib/columns): which sort it defaults to, whether it
  // has a sort at all (Saved doesn't), and whether its empty state may
  // offer "Add your first feed" (Saved and For You).
  columnKey: ColKey
  // The name the header shows (App's column label: the category's
  // label, or Saved / For You).
  label: string
  // The feed the empty state reports on (``sourcesByName``'s key): the
  // one feed a filter shows, or for a category the feed named like it.
  // Saved, For You and a filter of several feeds leave it unset.
  sourceName?: string
  // The tab as its snapshot draws it (lib/feedOrder.ts, PR3b): the
  // stories not seen yet, in the tab's order (Newest or Best), with
  // arrivals at the end; then the seen ones, drawn dimmed under the
  // marker and the folded "Earlier (N)". App partitions; the Column only
  // renders. Saved has everything in ``unseen`` and no marker.
  sections: {
    unseen: Entry[]
    earlier: Entry[]
  }
  // How many of the tab's stories aren't seen yet (the partition's
  // count: held arrivals and stories a poll dropped included). 0 shows
  // "You're all caught up"; above 0 the end row reads "N not seen ·
  // Mark as read" (D24), and the header shows "N new" on md+.
  unseenCount?: number
  // C8: the next shown tab with stories not seen yet, named under the
  // marker of a caught-up tab. App keeps one object per value, so the
  // memo compares it by reference.
  nextTab?: TabCount | null
  // The snapshot's generation: when it changes, the batches start over
  // and Earlier folds again.
  generation?: number
  sourcesById: Map<number, string>
  // Source objects keyed by name, so the empty-state JSX can show
  // the source's last_fetch_at / error_count / last_error and
  // tell the user why the column is empty ("last fetch errored",
  // "auto-disabled after 5 errors", etc.) instead of just
  // "no entries yet". Optional — a column with no ``sourceName``
  // (Saved, For You, a filter of several feeds), or one whose name
  // matches no feed, falls back to "No stories yet". Added in slice 19.
  sourcesByName?: Map<string, import('../api').Source>
  // Every feed is a built-in one: an empty Saved or For You adds the
  // "Add your first feed" button, which calls ``onAddFeed`` (stable;
  // the memo comparator ignores it).
  noOwnFeeds?: boolean
  onAddFeed?: () => void
  // The id of the currently keyboard-selected entry in this column.
  // Only the matching Card renders the blue focus ring. ``-1``
  // (or undefined) means no selection in this column.
  selectedId?: number
  // Ref callback so App can focus + scroll-into-view on keyboard nav.
  cardRefs?: Map<number, HTMLElement | null> | React.MutableRefObject<Map<number, HTMLElement | null>>
  // A card's source name was tapped: open that feed's editor. Must be
  // stable (the memo comparator ignores callbacks).
  onSourceTap?: (sourceId: number) => void
  // A story was opened (Card's ``onActivate``): App marks it seen.
  onOpenEntry?: (entry: Entry) => void
  // Mark all as read: the ⋯'s first row and the "N not seen · Mark as
  // read" end row. App marks the tab's unseen stories and shows Undo.
  onMarkAll?: () => void
  // C8's row: open that tab.
  onOpenTab?: (key: ColKey) => void
  // Whether the marker is above the scroller's visible top. While it
  // is, App holds new arrivals back (Safari has no scroll anchoring),
  // and the rows under the marker stay as they were drawn.
  onHoldChange?: (hold: boolean) => void
  // Earlier folded with the keyboard selection inside it (its button,
  // or a new snapshot): App drops the selection, so no shortcut acts on
  // a card that isn't drawn. Called with the selected id. Must be stable
  // (the memo comparator ignores callbacks).
  onSelectionFolded?: (entryId: number) => void
  // Prefs + setter for the ⋯ popover (Mark all, Newest | Best, and
  // Show stories from). Without a setter there is no ⋯. The empty
  // state's "Show 7 days" writes through the same setter.
  prefs?: ColumnPrefs
  onPrefsChange?: (next: ColumnPrefs) => void
  // Whether the tab's stories at its window are known (App's
  // ``windowAnswered``): false while a 7-day or All fetch hasn't
  // answered yet, or has only failed. Until then an empty tab says
  // nothing of the window. Unset: known.
  windowAnswered?: boolean
  // Optional map sourceId → category. Passed through to each Card so
  // it can render the colored left stripe. Without it the cards just
  // skip the stripe.
  categoriesBySourceId?: Map<number, string>
  // Optional map sourceId → favicon path. Passed through to each Card
  // as a thumbnail fallback for entries with no photo of their own —
  // see Card.tsx's ThumbnailFallback. Without it, image-less entries
  // just render with no thumbnail (today's behavior).
  faviconBySourceId?: Map<number, string | null>
  // Per-card summary expansion set + toggle callback. App owns both
  // so the keyboard ``s`` shortcut and the per-card chevron stay in
  // sync. Both optional — older callers (none today) just omit
  // them and the cards render with summaries permanently collapsed.
  expandedSummaries?: Set<number>
  onToggleSummary?: (entryId: number) => void
  // Hide (Card's ``onHide``: the ⋯ sheet's row and a swipe left). App's
  // ``hideStory``: the story leaves every tab at once, with Undo.
  onHideEntry?: (entryId: number) => void
  // Per-card "star" / "unstar" action. When present, each card
  // gets a bookmark button. App wires this to the starred set; the
  // entry surfaces in the dedicated "Saved" column.
  onStarEntry?: (entryId: number) => void
  // Per-card "is this entry currently starred" lookup. App
  // passes a Set so each card render is an O(1) check rather
  // than a linear search.
  starredSet?: Set<number>
  // Per-card vote action + lookup — same shape as star/hide but a
  // three-state Map (no entry / 'up' / 'down') instead of a Set,
  // since a vote isn't a boolean membership test.
  onVoteEntry?: (entryId: number, direction: 'up' | 'down' | null) => void
  votedMap?: Map<number, 'up' | 'down'>
  // The phone only: a callback ref for the story list, which is the
  // phone's real scroller (this section is h-full inside App's
  // wrapper). The list it lands on is marked ``data-scroller``. Pass a
  // stable function (App passes its state setter, or undefined while
  // the phone layout is hidden on a desktop). The memo comparator
  // compares it, so crossing 768px re-renders the list and moves the
  // ref and ``data-scroller`` with it.
  scrollerRef?: (el: HTMLElement | null) => void
  // The scroller the "load more" sentinel (and the marker, for
  // ``onHoldChange``) is observed against: this Column's own list on
  // the phone (the element ``scrollerRef`` hands App), App's one
  // desktop wrapper on the desktop. Not the viewport: a margin on the
  // viewport doesn't reach past the phone list's clipped edge. Not this
  // list on the desktop either: there it grows to fit its cards, so it
  // would always hold the sentinel. Null until App has the element;
  // nothing more loads until then.
  sentinelRoot?: HTMLElement | null
  // Seen on scroll (PR3c): the phone tab's unseen cards and its marker
  // register with ``seenRegistry`` (App's ``useSeenObserver``), which
  // marks a card seen once it has been read on screen and scrolled off
  // the top. Only the phone Column, and never Saved; App keys that
  // Column by tab, so this never changes while it is mounted. Brief,
  // search, the desktop and Saved are open only.
  trackSeen?: boolean
  // Stable for the App's life; the memo comparator ignores it, like the
  // callbacks, and Column reads it through a ref.
  seenRegistry?: SeenRegistry<Element>
}

// Cards render 30 at a time. A ``[data-more]`` sentinel after the last
// one adds the next 30 once it is within 600px of the bottom of
// ``sentinelRoot``.
const BATCH = 30
const MORE_MARGIN = '0px 0px 600px 0px'
// The limit that draws the first ``n`` cards, in whole batches.
const batchesFor = (n: number) => Math.ceil(n / BATCH) * BATCH

// What the rows between the marker and Earlier show, for one snapshot:
// the count the marker reads (0: "You're all caught up") and C8's tab
// (null: no row).
type EndRows = { generation: number | undefined; count: number; next: TabCount | null }

// Slice 21: renamed ``Column`` to ``ColumnInner`` and added a
// ``React.memo`` wrap with a custom comparator (``_columnPropsEqual``,
// below) so App's per-render closures don't defeat memoization. The
// comparator ignores the callback props (``onOpenEntry``,
// ``onMarkAll``, ``onOpenTab``, ``onHoldChange``,
// ``onSelectionFolded``, ``onHideEntry``,
// ``onStarEntry``, ``onVoteEntry``,
// ``onToggleSummary``, ``onPrefsChange``), the per-card ``cardRefs``
// and the stable ``seenRegistry`` — they're inherently per-render at the
// App layer; the data-driven props (``sections``, ``unseenCount``,
// ``nextTab``, ``generation``, ``trackSeen``, ``sourcesById``,
// ``sourcesByName``, ``starredSet``, ``votedMap``,
// ``prefs``, ``sentinelRoot``) and the
// phone's ``scrollerRef`` (a stable setter or undefined, so comparing
// it costs no render) are what we actually re-render against.
//
// Trade-off: if a callback's logic changes without the data
// changing, this Column won't re-render. App's callbacks here read
// refs or the latest partition (``useFeedOrder``'s ``latest``), never
// a render-old closure, so a kept callback still does the right thing.
//
// Pair with the ``useCallback`` wraps in ``App.tsx`` (slice 21, same
// commit) so the App-level handlers keep stable identity between
// renders — otherwise the JSX-passed inline arrows would still defeat
// the memo even with this comparator.
export function ColumnInner({
  columnKey,
  label,
  sourceName,
  sections,
  unseenCount,
  nextTab,
  generation,
  sourcesById,
  // Slice 19: optional sourcesByName map for context-aware empty-state.
  // Meta columns (Saved / For You / Multisub) pass undefined; the
  // empty-state falls back to the original generic message in that case.
  sourcesByName,
  noOwnFeeds,
  onAddFeed,
  selectedId,
  cardRefs,
  onSourceTap,
  onOpenEntry,
  onMarkAll,
  onOpenTab,
  onHoldChange,
  onSelectionFolded,
  prefs,
  onPrefsChange,
  windowAnswered,
  categoriesBySourceId,
  faviconBySourceId,
  expandedSummaries,
  onToggleSummary,
  onHideEntry,
  onStarEntry,
  starredSet,
  onVoteEntry,
  votedMap,
  scrollerRef,
  sentinelRoot,
  trackSeen,
  seenRegistry,
}: Props) {
  const [popoverOpen, setPopoverOpen] = useState(false)
  // How many unseen cards render, and how many of Earlier's once it is
  // open. Both start over (and Earlier folds again) with every new
  // snapshot, so a tab that resets opens at its first 30.
  const [limit, setLimit] = useState(BATCH)
  const [earlierOpen, setEarlierOpen] = useState(false)
  const [earlierLimit, setEarlierLimit] = useState(BATCH)
  // Every unseen card has been drawn in this snapshot. From then on a
  // story that arrives is drawn at once (the limit grows with it): the
  // marker, C8's row and an open Earlier never go away for a frame while
  // a batch waits for the sentinel. Only a snapshot's first load goes
  // through the sentinel. Not in Saved: nothing is drawn under its cards,
  // and its stories can arrive by the hundred (PR4c: saves no tab holds
  // come by id, 200 to a request, often after Saved is drawn), so they
  // go through the batches too.
  const [drewAllUnseen, setDrewAllUnseen] = useState(false)
  // The keyboard selection that opened Earlier. Earlier opens when the
  // selection moves into it, once per card, so the Earlier button can
  // still fold it with the selection inside. A new snapshot keeps it: a
  // selection already inside Earlier hasn't moved, so Earlier stays
  // folded (and App drops the selection, below). A selection the reset
  // itself moves into Earlier (Mark all) still opens it.
  const [openedFor, setOpenedFor] = useState<number | undefined>(undefined)
  const [shownGeneration, setShownGeneration] = useState(generation)
  // A new snapshot (React's "adjust state when a prop changes"). React
  // renders again with these before it draws anything, so the
  // adjustments below wait for that render instead of acting on the old
  // snapshot's limits.
  const newSnapshot = shownGeneration !== generation
  if (newSnapshot) {
    setShownGeneration(generation)
    setLimit(BATCH)
    setEarlierOpen(false)
    setEarlierLimit(BATCH)
    setDrewAllUnseen(false)
  }
  const [sentinel, setSentinel] = useState<HTMLElement | null>(null)
  const [marker, setMarkerEl] = useState<HTMLElement | null>(null)
  // The marker is above the scroller's visible top (its observer, below).
  // App holds arrivals back meanwhile; the rows between the marker and
  // Earlier stay as they were drawn (``end``, below).
  const [held, setHeld] = useState(false)
  // Set when the end row's "Mark as read" was pressed with the keyboard
  // or a screen reader: the snapshot it was pressed in. The row turns
  // into "You're all caught up" and the focus goes there, not to <body>.
  const refocusRef = useRef<{ generation: number | undefined } | null>(null)
  // The pointer type ("mouse", "pen", "touch") of a pointerdown on the
  // end row's button, until the click it starts reads and clears it.
  const endPointerRef = useRef<string | null>(null)
  const sortLabelId = useId()
  const windowLabelId = useId()
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const prefsButtonRef = useRef<HTMLButtonElement | null>(null)
  // The latest hold callback, for the marker's observer (the memo keeps
  // old callbacks; this one must reach App's current tab).
  const holdRef = useRef(onHoldChange)
  holdRef.current = onHoldChange
  // The seen-on-scroll registry, for the stable card ref callback below.
  const seenRef = useRef(seenRegistry)
  seenRef.current = seenRegistry
  // The snapshot this render draws, and the one the last commit drew
  // (moved on by the layout effect below, which runs after the commit's
  // refs are set): the marker's ref callback tells a change inside one
  // snapshot from a new one by them.
  const generationRef = useRef(generation)
  generationRef.current = generation
  const drawnGenerationRef = useRef(generation)

  // The marker's element, for its ref callback. Its two forms are two
  // elements, so the row turning from one into the other inside a
  // snapshot (the end rule using up the last cards on screen with it, an
  // arrival making the count 1) takes the old one out of the page, and
  // the focus on it would fall to <body>. It goes on to the new row
  // instead (no scroll), so a keyboard or screen-reader user keeps their
  // place. React calls the old element's ref with null while it is still
  // in the page, and the new one's in the same commit, before any layout
  // effect. A new snapshot is left to the refocus effect below: the end
  // row's own press resets the tab, and only a keyboard or reader press
  // hands the focus on.
  const markerElRef = useRef<HTMLElement | null>(null)
  const markerFocusRef = useRef(false)
  const setMarker = useCallback((el: HTMLElement | null) => {
    if (el) {
      const hand = markerFocusRef.current && generationRef.current === drawnGenerationRef.current
      markerFocusRef.current = false
      markerElRef.current = el
      const active = document.activeElement
      if (hand && (!active || active === document.body)) el.focus({ preventScroll: true })
    } else {
      const was = markerElRef.current
      markerElRef.current = null
      markerFocusRef.current = was != null && was === document.activeElement
    }
    setMarkerEl(el)
  }, [])

  // This Column's list (the phone's scroller; ``scrollerRef`` hands it
  // to App too), for the scroll to its top below.
  const listRef = useRef<HTMLDivElement | null>(null)
  const setList = useCallback(
    (el: HTMLDivElement | null) => {
      listRef.current = el
      scrollerRef?.(el)
    },
    [scrollerRef],
  )
  // A tab that tracks seen on scroll starts its list at its top with
  // each new snapshot: a Refresh landing, a new Sort or age window, Mark
  // all and its Undo (C1's return scrolls there itself). The tab is
  // starting over. Left where it was, the reset would take every story
  // seen above the screen out from over the cards being read, and those
  // would jump up past the top: the tracker can't tell that from a
  // scroll past them, so it would mark them, and the reader would lose
  // their place. At the top, the unseen stories start with the ones
  // read last (for the same sort). Done in the commit, before the
  // browser draws or reports any intersection, so no frame shows the
  // jump. A marker that went without a new one in this commit hands no
  // focus on later.
  useLayoutEffect(() => {
    markerFocusRef.current = false
    if (drawnGenerationRef.current === generation) return
    drawnGenerationRef.current = generation
    if (trackSeen) listRef.current?.scrollTo({ top: 0 })
  })

  // Keep the latest ``cardRefs`` value in a ref so we can ship a
  // stable ref callback to each ``Card``. Without this, the inline
  // lambda in the ``entries.map`` below would defeat the
  // ``Card.memo`` — every Column re-render allocates new closures
  // and React would re-mount every card's DOM ref. The cardRefs
  // shape (Map vs ref-of-Map) can change between renders too, so we
  // also stash the active setter in the ref to keep the dispatcher
  // single-sourced.
  const cardRefsLatest = useRef(cardRefs)
  cardRefsLatest.current = cardRefs

  // Stable ref callback. Same function reference across renders;
  // reads the latest ``cardRefs`` and ``entry.id`` from refs so
  // each card lands in the right slot regardless of when it
  // mounted.
  // A detaching card removes its entry only while the entry is still
  // that card: the same story can be registered by the phone layout
  // and the desktop grid, and the grid unmounting (the window crossing
  // 768px) must not drop the phone card's entry.
  // An unseen card of a tab that tracks seen-on-scroll (``track``: the
  // phone Column, above the marker) also registers with the seen
  // registry here, so Card's memo stays untouched. A detach always
  // unregisters, with the element going away (a card that moved to
  // Earlier with a new snapshot is a new element).
  const setCardRef = useCallback((entryId: number, track: boolean) => (el: HTMLElement | null, prev?: HTMLElement | null) => {
    if (el) {
      if (track) seenRef.current?.card(entryId, el)
    } else {
      seenRef.current?.card(entryId, null, prev)
    }
    const refs = cardRefsLatest.current
    if (!refs) return
    const map = 'current' in refs ? refs.current : refs
    if (el) map.set(entryId, el)
    else if (!prev || map.get(entryId) === prev) map.delete(entryId)
  }, [])

  // Click-outside dismissal. The popover is small and short-lived;
  // a backdrop div would be overkill. ``pointerdown``, not
  // ``mousedown``: iOS sends no mousedown for a tap on empty space,
  // so the popover would stay open. The ⋯ button is left to its own
  // onClick toggle; closing here too would reopen it on the click.
  useEffect(() => {
    if (!popoverOpen) return
    const onDocPointerDown = (e: PointerEvent) => {
      const target = e.target as Node
      if (prefsButtonRef.current?.contains(target)) return
      if (popoverRef.current && !popoverRef.current.contains(target)) {
        setPopoverOpen(false)
      }
    }
    document.addEventListener('pointerdown', onDocPointerDown)
    return () => document.removeEventListener('pointerdown', onDocPointerDown)
  }, [popoverOpen])

  // Esc closes the popover. Same effect slot as click-outside.
  useEffect(() => {
    if (!popoverOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPopoverOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [popoverOpen])

  const isSaved = columnKey === SAVED_KEY
  const count = isSaved ? 0 : (unseenCount ?? 0)
  const unseenTotal = sections.unseen.length
  const earlierTotal = isSaved ? 0 : sections.earlier.length

  // The keyboard-selected card always renders, so App can scroll to it
  // (ArrowDown walks past a batch, and from the unseen cards into
  // Earlier): the limits grow, by whole batches, to reach it. Earlier
  // opens when the selection moves into it; folded again with the
  // selection inside (its button, or a new snapshot), it takes the
  // selection with it (``onSelectionFolded``, below).
  let selectedUnseen = -1
  let selectedEarlier = -1
  if (selectedId != null) {
    selectedUnseen = sections.unseen.findIndex((e) => e.id === selectedId)
    if (selectedUnseen < 0 && !isSaved) selectedEarlier = sections.earlier.findIndex((e) => e.id === selectedId)
  }
  if (!newSnapshot) {
    if (selectedEarlier >= 0 && selectedId !== openedFor) {
      setOpenedFor(selectedId)
      setEarlierOpen(true)
    } else if (selectedEarlier < 0 && openedFor !== undefined) {
      setOpenedFor(undefined)
    }
  }
  const reachEarlier = earlierOpen && selectedEarlier >= 0
  const needUnseen = Math.max(reachEarlier ? unseenTotal : selectedUnseen + 1, drewAllUnseen && !isSaved ? unseenTotal : 0)
  if (!newSnapshot && needUnseen > limit) setLimit(batchesFor(needUnseen))
  if (!newSnapshot && reachEarlier && selectedEarlier >= earlierLimit) setEarlierLimit(batchesFor(selectedEarlier + 1))
  const shown = Math.max(limit, needUnseen)
  const unseenShown = sections.unseen.slice(0, shown)
  // The marker, C8's row and Earlier come only once every unseen card
  // is drawn, so nothing under them moves while the batches load.
  const unseenDone = shown >= unseenTotal
  if (!newSnapshot && unseenDone && !drewAllUnseen) setDrewAllUnseen(true)
  const earlierShownCount = Math.max(earlierLimit, reachEarlier ? selectedEarlier + 1 : 0)
  const earlierShown = unseenDone && earlierOpen ? sections.earlier.slice(0, earlierShownCount) : []
  // Which list the "load more" sentinel grows.
  const more: 'unseen' | 'earlier' | null = !unseenDone
    ? 'unseen'
    : earlierOpen && earlierShownCount < earlierTotal
      ? 'earlier'
      : null

  // The rows between the marker and Earlier: the marker's form ("You're
  // all caught up" or "N not seen · Mark as read") and C8's row. While
  // the marker is above the screen they stay as last drawn, so a row
  // that comes or goes up there (a held arrival turning the count from 0
  // to 1, another tab's first story bringing C8's row) can't move what
  // is being read below them: Safari has no scroll anchoring. They catch
  // up when the marker is back on screen, the moment App lets the held
  // arrivals in, and with every new snapshot.
  const liveEnd: EndRows = { generation, count, next: count === 0 ? (nextTab ?? null) : null }
  const [drawnEnd, setDrawnEnd] = useState<EndRows>(liveEnd)
  const keepEnd = held && drawnEnd.generation === generation
  const end = keepEnd ? drawnEnd : liveEnd
  if (!keepEnd && (drawnEnd.generation !== generation || drawnEnd.count !== count || drawnEnd.next !== liveEnd.next)) {
    setDrawnEnd(liveEnd)
  }

  // Load more: the sentinel near the bottom of ``sentinelRoot`` adds 30
  // cards to the list it ends. A fresh observer per limit (hence the
  // dependencies): its first callback reports where the sentinel is
  // now, so a batch that still leaves the sentinel inside the margin
  // loads the next one too, and one that pushes it out waits for the
  // next scroll. The observer stops after one batch, so a second
  // callback can't add 60.
  useEffect(() => {
    if (!sentinel || !sentinelRoot || !more) return
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return
        io.disconnect()
        if (more === 'unseen') setLimit((n) => n + BATCH)
        else setEarlierLimit((n) => n + BATCH)
      },
      { root: sentinelRoot, rootMargin: MORE_MARGIN },
    )
    io.observe(sentinel)
    return () => io.disconnect()
  }, [sentinel, sentinelRoot, more, limit, earlierLimit])

  // Is the marker above the scroller's visible top? Then a story that
  // arrives now would be drawn above what is being read and push it
  // down, so App holds arrivals back until it isn't. The observer's
  // first callback reports where the marker is now, so a marker that
  // changes (the row turning into "You're all caught up") keeps the
  // hold true to its place; a marker that goes (unseen cards still
  // loading) or a Column that goes (another tab) lets arrivals in. A new
  // snapshot measures it again too: an observer calls back only when
  // the marker crosses the screen's edge, and C1's jump to the top takes
  // a marker above the screen to below it with no frame in between, so
  // the hold (and the end rows it keeps) would stay on until the next
  // crossing.
  useEffect(() => {
    if (!marker || !sentinelRoot) return
    const io = new IntersectionObserver((entries) => {
      const e = entries[entries.length - 1]
      const top = e.rootBounds ? e.rootBounds.top : sentinelRoot.getBoundingClientRect().top
      const hold = !e.isIntersecting && e.boundingClientRect.bottom <= top
      setHeld(hold)
      holdRef.current?.(hold)
    }, { root: sentinelRoot })
    io.observe(marker)
    return () => io.disconnect()
  }, [marker, sentinelRoot, generation])
  useEffect(() => {
    if (marker) return
    setHeld(false)
    holdRef.current?.(false)
  }, [marker])
  useEffect(() => () => holdRef.current?.(false), [])

  // A folded Earlier never keeps the keyboard selection. Its button, or
  // a new snapshot, folded it with the selected card inside, and the
  // selection didn't move, so it stays folded: App drops the selection,
  // and ``s``, ``b``, ``h`` and Enter can't act on a story no one can
  // see. The next arrow starts at the column's first card. A layout
  // effect: App's update then renders before the browser takes the next
  // key (from a passive effect it waits behind it, and the key acts on
  // the card that went).
  const foldedSelection = selectedEarlier >= 0 && !earlierOpen ? selectedId : undefined
  useLayoutEffect(() => {
    if (foldedSelection != null) onSelectionFolded?.(foldedSelection)
  }, [foldedSelection, onSelectionFolded])

  // Seen on scroll's end rule (PR3c): the marker, in either form, is the
  // end of the tab. Once it has been on screen for the dwell, the cards
  // still on screen with it count as seen: nothing scrolls past the last
  // ones.
  useEffect(() => {
    const reg = seenRef.current
    if (!trackSeen || !marker || !reg) return
    reg.marker(marker)
    return () => reg.marker(null, marker)
  }, [trackSeen, marker])

  // The end row's "Mark as read" was pressed with the keyboard or a
  // screen reader (never a mouse, pen or finger: its ``onClick``): once
  // the tab has started over, caught up, the focus that fell to <body>
  // with the button goes to "You're all caught up" in its place, so the
  // reader keeps their place in the page. The first run
  // after the press can still see the button that went (the new row's
  // element reaches ``marker`` a render later), so it waits for a marker
  // in the page. Focused without a scroll, then brought into view only
  // if it isn't all inside ``sentinelRoot``'s box: on the phone the list
  // has shrunk to it and nothing moves, but in the desktop grid the
  // other columns of its row keep the page tall, so a shorter column's
  // marker can be thousands of pixels above the screen.
  useEffect(() => {
    const from = refocusRef.current
    if (!from || from.generation === generation || !marker?.isConnected) return
    refocusRef.current = null
    const active = document.activeElement
    if (count !== 0 || (active && active !== document.body)) return
    marker.focus({ preventScroll: true })
    const box = sentinelRoot?.getBoundingClientRect()
    const at = marker.getBoundingClientRect()
    if (box && (at.top < box.top || at.bottom > box.bottom)) marker.scrollIntoView({ block: 'nearest' })
  }, [generation, count, marker, sentinelRoot])

  const empty = unseenTotal === 0 && earlierTotal === 0
  const cardProps = {
    trackSeen: !!trackSeen,
    sourcesById,
    selectedId,
    cardRefs,
    setCardRef,
    categoriesBySourceId,
    faviconBySourceId,
    onOpenEntry,
    expandedSummaries,
    onToggleSummary,
    onHideEntry,
    onStarEntry,
    starredSet,
    onVoteEntry,
    votedMap,
    onSourceTap,
  }

  return (
    <section className="flex flex-col h-full overflow-visible">
      <header className="relative flex items-center justify-between px-1 pb-2 min-h-[44px]">
        {/* Thin hairline underline. iOS-style list-header separator —
          a single 1px line right at the bottom of the title row,
          rendered slightly inset so the line reads as belonging to
          the column rather than the page. */}
        <div
          aria-hidden="true"
          className="absolute left-1 right-1 bottom-0 h-px bg-hairline"
        />
        <div className="flex items-center gap-2 min-w-0">
          <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary truncate">
            {label}
          </h2>
          {/* How many stories aren't seen yet, on md+ (the phone's tab
              strip carries the same count as a badge). */}
          {count > 0 && (
            <span data-new-count className="hidden md:inline shrink-0 text-ios-caption font-semibold text-accent">
              {count} new
            </span>
          )}
        </div>

        {/* Sort/filter popover trigger. Every tab but Saved has one:
            For You sorts too (its Best is the server's order). Saved
            has no row to show (no read state, no sort, no window), so
            a ⋯ there would open an empty popover. */}
        {onPrefsChange && !isSaved && (
          <button
            ref={prefsButtonRef}
            data-column-action="prefs"
            onClick={() => setPopoverOpen((p) => !p)}
            className="shrink-0 w-11 h-11 -mr-1 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated"
            aria-label="column preferences"
            aria-expanded={popoverOpen}
          >
            <MoreIcon className="w-4 h-4" />
          </button>
        )}

        {popoverOpen && prefs && onPrefsChange && !isSaved && (
          <div
            ref={popoverRef}
            data-column-popover
            // Position relative to the header so it lands just below
            // the more-button on the right edge. min-w-[240px] keeps
            // the controls readable. z-20 puts it above cards but
            // below the Drawer (z-30+). The popover is the only
            // place we still render a popover-shaped surface — the
            // rest of the dashboard uses sheets / grouped lists.
            className="absolute right-0 top-full mt-2 z-20 min-w-[240px] rounded-ios-lg bg-bg-elevated shadow-2xl p-3 text-ios-body space-y-3"
          >
            {/* Mark all as read: the tab's stories not seen yet, with
                Undo (App). Disabled at 0. (Saved, with no read state,
                has no ⋯.) */}
            {onMarkAll && (
              <button
                type="button"
                data-mark-all
                disabled={count === 0}
                onClick={(e) => {
                  // The row goes with the popover. Pressed with the
                  // keyboard or VoiceOver (focus on it, or a click with
                  // ``detail`` 0), focus goes back to ⋯, not to <body>, so
                  // it keeps its place and can reach the toast's Undo.
                  const focused = e.detail === 0 || e.currentTarget === document.activeElement
                  setPopoverOpen(false)
                  if (focused) prefsButtonRef.current?.focus({ preventScroll: true })
                  onMarkAll()
                }}
                className="w-full min-h-[44px] -my-1 rounded-ios px-1 text-left text-ios-body text-accent active:bg-bg-surface disabled:text-label-tertiary"
              >
                Mark all as read
              </button>
            )}
            {/* Newest | Best. (Saved, which keeps its own order,
                lib/sorting, has no ⋯.) */}
            <div>
              <div
                id={sortLabelId}
                className="text-ios-caption uppercase tracking-wide text-label-tertiary mb-1"
              >
                Sort
              </div>
              <div
                role="group"
                aria-labelledby={sortLabelId}
                className="grid grid-cols-2 rounded-ios overflow-hidden border border-hairline"
              >
                {SORTS.map((o) => {
                  const active = normalizeSort(prefs.sort, columnKey) === o.value
                  return (
                    <button
                      key={o.value}
                      type="button"
                      data-sort={o.value}
                      aria-pressed={active}
                      onClick={() => {
                        if (!active) onPrefsChange({ ...prefs, sort: o.value })
                      }}
                      className={`min-h-[44px] text-ios-body transition ${
                        active
                          ? 'bg-bg-elevated text-accent'
                          : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
                      }`}
                    >
                      {o.label}
                    </button>
                  )
                })}
              </div>
            </div>
            {/* Show stories from: the tab's window (D5). (Saved,
                which lists every save, has no ⋯.) */}
            <div>
              <div
                id={windowLabelId}
                className="text-ios-caption uppercase tracking-wide text-label-tertiary mb-1"
              >
                Show stories from
              </div>
              <div
                role="group"
                aria-labelledby={windowLabelId}
                className="grid grid-cols-4 rounded-ios overflow-hidden border border-hairline"
              >
                {WINDOWS.map((o) => {
                  const active = prefs.window === o.value
                  return (
                    <button
                      key={o.value}
                      type="button"
                      data-window={o.value}
                      aria-pressed={active}
                      onClick={() => {
                        if (!active) onPrefsChange({ ...prefs, window: o.value })
                      }}
                      className={`min-h-[44px] px-1 text-ios-body whitespace-nowrap transition ${
                        active
                          ? 'bg-bg-elevated text-accent'
                          : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
                      }`}
                    >
                      {o.label}
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        )}
      </header>
      {/* ``overflow-x-clip``: a mid-swipe card translateX (up to
          88px) must not widen this scroller — ``overflow-y-auto``
          alone would leave overflow-x at ``auto`` and give the
          browser a horizontally-pannable region mid-swipe, so the
          gesture pans the whole list instead of dragging the card.
          ``clip`` (not ``hidden``) keeps the element scrollable and
          avoids creating a scroll container on the X axis. */}
      <div
        ref={setList}
        data-scroller={scrollerRef ? '' : undefined}
        className="flex-1 overflow-x-clip overflow-y-auto overscroll-contain space-y-2 pr-1"
      >
        {empty ? (
          // Slice 19: context-aware empty state. The source's
          // ``last_fetch_at`` / ``last_error`` / ``error_count``
          // tell the user WHY the column is empty — has the
          // scheduler tried yet? did the last fetch error? was
          // the source auto-disabled? Meta columns (Saved /
          // For You / Multisub) don't have a backing source so
          // we fall back to the original generic message.
          <EmptyColumnMessage
            sourceName={sourceName}
            sourcesByName={sourcesByName}
            onAddFeed={noOwnFeeds && (columnKey === SAVED_KEY || columnKey === FORYOU_KEY) ? onAddFeed : undefined}
            window={columnKey.startsWith('cat:') ? prefs?.window : undefined}
            windowAnswered={windowAnswered !== false}
            onShowWider={prefs && onPrefsChange ? (next) => onPrefsChange({ ...prefs, window: next }) : undefined}
          />
        ) : (
          <>
            {/* The stories not seen yet, in the tab's order. They never
                dim, even once opened: nothing moves or fades under the
                reader until the next snapshot. */}
            {unseenShown.length > 0 && <CardList entries={unseenShown} dimmed={false} {...cardProps} />}
            {/* Load more. A div, never a <section>: smoke asserts one
                section per <main>. */}
            {more === 'unseen' && <div ref={setSentinel} data-more aria-hidden="true" className="h-px" />}
            {unseenDone && !isSaved && (
              <>
                {/* The marker (D4), in the form ``end`` holds. Not
                    role="status": smoke waits for no status region before
                    every state, and it isn't news. Focusable from code
                    only, for the end row's focus hand-over. */}
                {end.count === 0 ? (
                  <div
                    ref={setMarker}
                    data-caught-up
                    tabIndex={-1}
                    className="flex min-h-[44px] items-center justify-center gap-2 px-1 text-ios-body text-label-secondary"
                  >
                    <span aria-hidden="true" className="text-accent">✓</span>
                    <span>You&apos;re all caught up</span>
                  </div>
                ) : (
                  // D24: some stories are still unseen (none opened, or
                  // flung past with PR3c). One tap marks them, with the
                  // same path and Undo as ⋯ → Mark all as read.
                  <button
                    ref={setMarker}
                    type="button"
                    data-caught-up
                    onPointerDown={(e) => {
                      endPointerRef.current = e.pointerType
                    }}
                    onClick={(e) => {
                      // Pressed with the keyboard or a screen reader, the
                      // focus goes on to the row that replaces it (the
                      // effect above). That is a click with ``detail`` 0
                      // (a key, or a reader's simulated click: VoiceOver,
                      // TalkBack, NVDA or JAWS in Chrome), or one on the
                      // focused button that no pointerdown started (NVDA
                      // and JAWS in Firefox send mouse events only). A
                      // mouse, pen or finger press is neither, though
                      // Chrome and Edge (and Firefox on Windows and
                      // Linux) focus the button on mousedown or a tap: in
                      // the desktop grid the new marker can be far above
                      // the screen, and bringing it into view would move
                      // the page under the pointer. (``:focus-visible``
                      // can't tell them apart: a button focused with Tab
                      // keeps it through a click.)
                      const pointer = endPointerRef.current
                      endPointerRef.current = null
                      if (e.detail === 0 || (!pointer && e.currentTarget === document.activeElement)) refocusRef.current = { generation }
                      onMarkAll?.()
                    }}
                    className="w-full min-h-[44px] rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated"
                  >
                    {end.count} not seen · Mark as read
                  </button>
                )}
                {/* C8: the next shown tab with stories not seen yet, under
                    "You're all caught up" only. Its name reads like the
                    tab's own in the strip ("News, 12 new"), not "News dot
                    12". */}
                {end.next && (
                  <button
                    type="button"
                    data-next-tab
                    aria-label={`${end.next.label}, ${end.next.count} new`}
                    onClick={() => end.next && onOpenTab?.(end.next.key as ColKey)}
                    className="w-full min-h-[44px] flex items-center justify-between gap-2 rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated"
                  >
                    <span className="min-w-0 truncate">
                      {end.next.label} · {end.next.count}
                    </span>
                    <ChevronIcon className="w-4 h-4 shrink-0" collapsed />
                  </button>
                )}
                {earlierTotal > 0 && (
                  <button
                    type="button"
                    data-earlier
                    aria-expanded={earlierOpen}
                    onClick={() => setEarlierOpen((o) => !o)}
                    className="w-full flex items-center gap-1.5 px-1 min-h-[44px]
                               text-ios-caption uppercase tracking-wide text-label-tertiary
                               hover:text-label-secondary active:text-label-primary
                               transition-colors select-none"
                  >
                    <ChevronIcon className="w-3 h-3" collapsed={!earlierOpen} />
                    <span>Earlier ({earlierTotal})</span>
                  </button>
                )}
                {/* The seen stories, dimmed, 30 at a time. */}
                {earlierShown.length > 0 && <CardList entries={earlierShown} dimmed {...cardProps} />}
                {more === 'earlier' && <div ref={setSentinel} data-more aria-hidden="true" className="h-px" />}
              </>
            )}
          </>
        )}
      </div>
    </section>
  )
}

// A run of cards: the unseen ones, or Earlier's (``dimmed``).
type CardListProps = {
  entries: Entry[]
  dimmed: boolean
  // The Column tracks seen-on-scroll: this run's cards register when
  // they are the unseen ones (Earlier's are seen already).
  trackSeen: boolean
  sourcesById: Map<number, string>
  selectedId: number | undefined
  cardRefs:
    | Map<number, HTMLElement | null>
    | React.MutableRefObject<Map<number, HTMLElement | null>>
    | undefined
  setCardRef: (entryId: number, track: boolean) => (el: HTMLElement | null, prev?: HTMLElement | null) => void
  categoriesBySourceId: Map<number, string> | undefined
  faviconBySourceId: Map<number, string | null> | undefined
  onOpenEntry: ((entry: Entry) => void) | undefined
  expandedSummaries: Set<number> | undefined
  onToggleSummary: ((entryId: number) => void) | undefined
  onHideEntry: ((entryId: number) => void) | undefined
  onStarEntry: ((entryId: number) => void) | undefined
  starredSet: Set<number> | undefined
  onVoteEntry: ((entryId: number, direction: 'up' | 'down' | null) => void) | undefined
  votedMap: Map<number, 'up' | 'down'> | undefined
  onSourceTap: ((sourceId: number) => void) | undefined
}

function CardList({
  entries,
  dimmed,
  trackSeen,
  sourcesById,
  selectedId,
  cardRefs,
  setCardRef,
  categoriesBySourceId,
  faviconBySourceId,
  onOpenEntry,
  expandedSummaries,
  onToggleSummary,
  onHideEntry,
  onStarEntry,
  starredSet,
  onVoteEntry,
  votedMap,
  onSourceTap,
}: CardListProps) {
  const track = trackSeen && !dimmed
  return (
    <div className="space-y-2" data-group={dimmed ? 'earlier' : 'unseen'}>
      {entries.map((e) => {
        // Stable ref callback. The previous version allocated a
        // new closure per card per Column render, defeating the
        // ``memo`` on ``Card``. Instead, ``Card`` writes its own
        // DOM node into ``cardRefs`` keyed by entry id — the
        // Column doesn't need to bind anything, the Card knows
        // its own id and looks itself up.
        //
        // The custom-equal in ``Card.memo`` ignores the
        // ``cardRef`` prop entirely (it's a sentinel ``null`` /
        // ``undefined`` from Column), so any callback churn
        // here is free.
        return (
          <Card
            key={e.id}
            entry={e}
            sourceName={sourcesById.get(e.source_id)}
            sourceFaviconPath={faviconBySourceId?.get(e.source_id)}
            dimmed={dimmed}
            selected={selectedId === e.id}
            cardRef={cardRefs || track ? setCardRef(e.id, track) : undefined}
            onActivate={onOpenEntry ? () => onOpenEntry(e) : undefined}
            category={categoriesBySourceId?.get(e.source_id)}
            expanded={expandedSummaries?.has(e.id) ?? false}
            onToggleSummary={onToggleSummary ? () => onToggleSummary(e.id) : undefined}
            onHide={onHideEntry ? () => onHideEntry(e.id) : undefined}
            onStar={onStarEntry ? () => onStarEntry(e.id) : undefined}
            starred={starredSet?.has(e.id) ?? false}
            onVote={onVoteEntry ? (dir) => onVoteEntry(e.id, dir) : undefined}
            vote={votedMap?.get(e.id) ?? null}
            onSourceTap={onSourceTap}
          />
        )
      })}
    </div>
  )
}

// iOS-style "more" glyph — three horizontal dots. Used for the
// column-preferences affordance; matches the SF Symbols
// "ellipsis" pictogram at small sizes.
function MoreIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <circle cx="6"  cy="12" r="1.75" />
      <circle cx="12" cy="12" r="1.75" />
      <circle cx="18" cy="12" r="1.75" />
    </svg>
  )
}

// Right-pointing chevron used for the "Earlier (N)" disclosure (and,
// unrotated, C8's next-tab row). Rotates 90° (so it points down) when
// Earlier is open. Single CSS transform, GPU-driven — no JS animation
// loop, no reflow. The ``aria-hidden`` chevron is paired with the
// surrounding button's ``aria-expanded`` so the disclosure state
// is announced correctly.
function ChevronIcon({ className, collapsed }: { className?: string; collapsed?: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${className ?? ''} transition-transform duration-200 ${collapsed ? '' : 'rotate-90'}`}
      aria-hidden="true"
    >
      <polyline points="9 6 15 12 9 18" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Slice 19: context-aware empty-state
// ---------------------------------------------------------------------------

// Auto-disable threshold for the source.error_count check below.
// Mirrors ``app.scheduler._AUTO_DISABLE_THRESHOLD`` — the backend
// flips active=False when error_count hits this. Surfacing it in
// the empty-state lets the user distinguish "transient error"
// (error_count < 5) from "auto-disabled" (>= 5).
const AUTO_DISABLE_THRESHOLD = 5

function EmptyColumnMessage({
  sourceName,
  sourcesByName,
  onAddFeed,
  window: tabWindow,
  windowAnswered,
  onShowWider,
}: {
  sourceName?: string
  sourcesByName?: Map<string, import('../api').Source>
  // Set on an empty Saved or For You while every feed is a built-in.
  onAddFeed?: () => void
  // A category tab's window (PR4d, D5): at 1, 2 or 7 days an empty
  // tab says "Nothing in 2 days · Show 7 days", and the offer widens
  // it (``onShowWider``). Unset on Saved, For You and a filter.
  window?: Window
  // Whether the tab's stories at that window are known (Column's
  // ``windowAnswered``). While a 7-day fetch is on its way, or after it
  // failed, an empty tab claims nothing and offers nothing: the line
  // would say "Nothing in 7 days" of stories it hasn't seen, and its
  // "Show all" would widen a tab that has some. The error banner says
  // why a fetch failed.
  windowAnswered?: boolean
  onShowWider?: (next: Window) => void
}) {
  // The column's feed: the one feed a filter shows, or the feed named
  // like a category.
  const source = sourceName != null ? sourcesByName?.get(sourceName) : undefined
  // A quiet category (D5): its feeds brought nothing in the window.
  // After a feed's own trouble (below), which says why, but before
  // "no entries match": the window is the filter.
  const quiet = tabWindow != null && EMPTY_WINDOW_LINES[tabWindow] != null && widerWindow(tabWindow)
  const quietLine = () => {
    // Not known yet (or the fetch failed): nothing, not a claim.
    if (!windowAnswered) return <div data-window-pending className="min-h-[44px]" />
    const [said, offer] = EMPTY_WINDOW_LINES[tabWindow!]!.split(' · ')
    const next = widerWindow(tabWindow!)!
    return (
      <div data-window-empty className="flex min-h-[44px] items-center gap-1 px-1 text-ios-body text-label-secondary">
        <span>{said}</span>
        <span aria-hidden="true">·</span>
        {onShowWider ? (
          // Keyed by the window: a new offer is a new button, so a press
          // (VoiceOver's focus) can't carry over from the last one.
          <button key={tabWindow} type="button" data-show-wider onClick={() => onShowWider(next)} className="min-h-[44px] px-1 text-accent active:opacity-70">
            {offer}
          </button>
        ) : (
          <span>{offer}</span>
        )}
      </div>
    )
  }
  // No such feed (Saved, For You, a filter of several feeds, a category
  // with no feed of its name) or sourcesByName not provided: the window
  // line on a quiet category, else "No stories yet", with the
  // first-feed button when set.
  if (!source) {
    if (quiet) return quietLine()
    return (
      <div className="px-1 space-y-3">
        <p className="text-ios-body text-label-secondary">No stories yet</p>
        {onAddFeed && <FirstFeedButton onClick={onAddFeed} />}
      </div>
    )
  }
  // Source-backed column. Pick the most specific message:
  //   1. auto-disabled (error_count >= threshold and not active)
  //   2. transient error (last_error set, error_count < threshold)
  //   3. fetched but found nothing (last_fetch_at set, no error)
  //   4. never fetched yet (last_fetch_at null)
  if (source.auto_disabled || (source.error_count >= AUTO_DISABLE_THRESHOLD && !source.active)) {
    return (
      <p className="text-ios-body text-label-secondary italic px-1">
        Popping stopped checking this feed after {source.error_count} failed tries — resume it in Menu → Settings → Feeds
      </p>
    )
  }
  if (source.last_error) {
    // Truncate to keep the message terse — the full error lives in
    // Menu → Settings → Feeds. 80 chars is enough to identify the
    // kind of error without scrolling.
    const snippet = source.last_error.slice(0, 80)
    return (
      <p className="text-ios-body text-label-secondary italic px-1">
        last fetch failed: {snippet}{source.last_error.length > 80 ? '\u2026' : ''}
      </p>
    )
  }
  if (quiet) return quietLine()
  if (source.last_fetch_at) {
    // Format the timestamp as "Xh ago" without pulling in a date
    // library. Sufficient granularity for "is the scheduler still
    // running?" — minutes/seconds aren't actionable.
    const fetchedMs = new Date(source.last_fetch_at).getTime()
    const ageHours = Math.max(0, Math.round((Date.now() - fetchedMs) / (1000 * 60 * 60)))
    return (
      <p className="text-ios-body text-label-secondary italic px-1">
        last fetched {ageHours}h ago — no entries match the current filters
      </p>
    )
  }
  return (
    <p className="text-ios-body text-label-secondary italic px-1">
      source added, waiting for first fetch
    </p>
  )
}

// Slice 21: custom comparator for ``React.memo`` wrap. Compares the
// data-driven props by identity (refs/maps/sets are reference-compared)
// or by value (scalars). Callback props are intentionally ignored —
// the App layer re-allocates them every render, and the per-render
// closure captures fresh state, so comparing them by identity would
// always return false and defeat the memo.
//
// Same pattern as ``Card.memo`` in ``Card.tsx`` (line 1477). Card's
// comparator is finer-grained (per-entry); Column's is coarser
// (sections ref + sets ref + scalar props) because Column renders
// the section containers and delegates to memoized Card children.
function _columnPropsEqual(prev: Props, next: Props): boolean {
  return (
    prev.columnKey === next.columnKey &&
    prev.label === next.label &&
    prev.sourceName === next.sourceName &&
    prev.sections === next.sections &&
    prev.sourcesById === next.sourcesById &&
    prev.sourcesByName === next.sourcesByName &&
    prev.noOwnFeeds === next.noOwnFeeds &&
    prev.unseenCount === next.unseenCount &&
    // C8's tab: one object per value (App caches them).
    prev.nextTab === next.nextTab &&
    // A new snapshot: the batches start over and Earlier folds.
    prev.generation === next.generation &&
    // Seen on scroll: fixed for a mounted Column (App keys the phone one
    // by tab), compared all the same.
    prev.trackSeen === next.trackSeen &&
    prev.selectedId === next.selectedId &&
    // ``prefs`` is a fresh object per columnPrefs write, but its
    // contents only change when the user opens the popover and picks
    // a new value. The user's own ``onPrefsChange`` callback reads
    // the latest value, so a stale comparator returning true here is
    // safe (the next user-driven write will produce a new prefs
    // object and the comparator will return false). Comparing by
    // reference is what makes this work — shallow-equal would
    // always differ because every render allocates a new object.
    prev.prefs === next.prefs &&
    prev.windowAnswered === next.windowAnswered &&
    prev.categoriesBySourceId === next.categoriesBySourceId &&
    prev.faviconBySourceId === next.faviconBySourceId &&
    prev.expandedSummaries === next.expandedSummaries &&
    prev.starredSet === next.starredSet &&
    prev.votedMap === next.votedMap &&
    // The element changes on every phone tab remount and once App has
    // the desktop wrapper; the sentinel's observer rebinds to it.
    prev.sentinelRoot === next.sentinelRoot &&
    // App's setter on the phone, undefined while the phone layout is
    // hidden on a desktop. Both are stable, so this only differs when
    // the width crosses 768px; without it the list would keep (or
    // never get) the ref and ``data-scroller`` after the flip.
    prev.scrollerRef === next.scrollerRef
  )
}

export const Column = memo(ColumnInner, _columnPropsEqual) as typeof ColumnInner
