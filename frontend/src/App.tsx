// App: 3-4 column desktop grid; on phones one column at a time,
// picked from a tab strip. The top bar has the title, search, refresh
// (phones) and one Menu button (≡), which holds Settings, saved views,
// Jump to column and the source filter. When OIDC is enabled and the
// user isn't logged in, the dashboard is replaced with a LoginPage.
//
// Layered UX features (each documented at the call site):
//   - Active source filter chips (multi-select via the Menu)
//   - Per-column sort/filter popover (the ⋯ button)
//   - The read model (PR3b): opening a story uses it up; each tab
//     shows its unseen stories, then "You're all caught up", then
//     "Earlier (N)"; the tab badges count what isn't seen yet
//     (lib/feedOrder.ts, lib/useFeedOrder.ts)
//   - Seen on scroll (PR3c): on the phone, a story read on screen and
//     scrolled off the top is used up too (lib/seen.ts,
//     lib/useSeenObserver.ts)
//   - Search (debounced, replaces columns when query is set)
//   - Keyboard navigation on desktop (←/→/↑/↓/Enter)
//   - Tab-visibility-aware polling (pauses when tab is hidden,
//     refreshes when it returns; after 10 minutes away the tabs start
//     over at their tops, C1)
//   - Brief tone picker (terse / narrative / alert)
//   - Source filter chips in the header

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { api, type Brief, type CurrentUser, type Entry, type Health, type Source } from './api'
import { BriefCard } from './components/BriefCard'
import { Card } from './components/Card'
import { Column, type ColumnPrefs } from './components/Column'
import { normalizeSort, prefsFor as tabPrefsFor, sortEntries, type Sort } from './lib/sorting'
import { DEFAULT_WINDOW, inWindow, isWider, windowHours, type Window } from './lib/window'
import { Drawer } from './components/Drawer'
import { FramingWatch } from './components/FramingWatch'
import { FirstFeedButton } from './components/FirstFeedButton'
import { Hamburger } from './components/Hamburger'
import { LoginPage } from './components/LoginPage'
import { SearchResults } from './components/SearchResults'
import { ActionSheetHost } from './components/ActionSheet'
import { voteInteractionEvents } from './components/Card'
import { ShortcutsSheet } from './components/ShortcutsSheet'
import { ToastHost, toast } from './components/Toast'
import { UserBadge } from './components/UserBadge'
import { Wallpaper } from './components/Wallpaper'
import { SkeletonGrid } from './components/Skeleton'
import { Settings } from './components/Settings'
import { isSettingsTab, type SettingsTab } from './lib/settingsTabs'
import { holdInteraction, lastStoryOpenAt, noteStoryOpened, recordBatched, recordImmediate } from './lib/interactions'
import { categoryLabel, rememberDisplayNames, sourceLabel } from './lib/labels'
import { fullOrder, moveKey, orderColumns, type Move } from './lib/categories'
import { isBuiltIn } from './lib/feedRules'
import { TabsEditor } from './components/TabsEditor'
import { configureSourceSaves, dropSource, setPageHiding } from './lib/sourceSaves'
import { useUpdateCheck, type Return } from './lib/useUpdateCheck'
import { countUnseen, keepIdsFor, nextTab as findNextTab, seenKey, seenTest, type TabCount } from './lib/feedOrder'
import { MAX_STARRED } from './lib/prefSync'
import { placeOf, putBack, savedStories, savedToFetch, type Place } from './lib/saved'
import { useFeedOrder, type FeedTab } from './lib/useFeedOrder'
import { createSeenBatch } from './lib/seen'
import { useSeenObserver } from './lib/useSeenObserver'
import {
  BRIEF_KEY,
  FILTER_KEY,
  FORYOU_KEY,
  SAVED_KEY,
  colKey,
  readStoredTab,
  resolveTab,
  type ColKey,
  type StoredTab,
  type Tab,
} from './lib/columns'
import { briefFailureMessage, errorMessage, fieldSaveMessage, isRetryable } from './lib/apiErrors'
import { sameHeaders, type HeaderMap } from './lib/headers'
// Per-user server-backed preferences. Replaces the 8 localStorage
// keys the dashboard used to write (readEntries, lastViewed,
// columnPrefs, columnSections, hiddenEntries, starredEntries,
// filterPresets, historyGroupBy). The localStorage versions
// were per-device, so a phone and a laptop saw different read
// state, different "+N new" counts, and different sort
// orders. The server-backed store keys by user_id, so all
// devices on the same LAN (sharing the bypass user_id) see
// the same state. See ``lib/preferences.tsx`` for the
// provider and ``lib/prefSync.ts`` for the sync: saves, hides,
// votes and read marks go to the server as changes, so two
// devices never undo each other's.
import {
  PREFERENCE_KEYS,
  usePreferences,
  type ColumnPrefsValue,
  type FilterPresetValue,
  MAX_PRESETS,
} from './lib/preferences'
import { STORAGE_KEYS, safeGetItem, safeRemoveItem, safeSetItem } from './lib/storage'
import { useMediaQuery } from './lib/visualViewport'

const REFRESH_INTERVAL_MS = 60_000
// Health pings don't need 60s cadence — DB status is slow-
// moving and the user only needs to see "everything is green" most
// of the time. 5 min cuts the request count by 5x for the cost of
// up-to-5-minute staleness on the status chip.
const HEALTH_INTERVAL_MS = 5 * 60_000
// C1: back after this long away, every tab starts over (a new snapshot,
// so what was read goes under the marker and what arrived is sorted in)
// and the lists go back to their tops. Shorter, nothing moves, so you
// come back to the same place after reading an article. Time away is
// measured with Date.now (useUpdateCheck), because timers stop while
// iOS suspends the page.
const RETURN_RESET_MS = 10 * 60 * 1000
// What calls off C1's second reset (``armReturnReset``): the user is
// already using the page. ``click`` too, for an activation that comes
// with none of the others (a desktop screen reader in Firefox sends
// only mouse events, and no pointer events with them).
const RETURN_INPUTS = ['pointerdown', 'touchstart', 'click', 'keydown', 'wheel'] as const

// Structural equality for the plain JSON objects the API returns
// (Entry, Source — flat scalars plus at most one nested plain
// object like `meta` / `custom_headers`, no functions/Dates/Maps).
// Used by `reconcileById` below to decide whether a freshly-fetched
// row can be swapped for its previous-poll object, or is genuinely
// unchanged and should keep the OLD reference.
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((v, i) => deepEqual(v, b[i]))
  }
  const aRec = a as Record<string, unknown>
  const bRec = b as Record<string, unknown>
  const aKeys = Object.keys(aRec)
  if (aKeys.length !== Object.keys(bRec).length) return false
  return aKeys.every((key) => deepEqual(aRec[key], bRec[key]))
}

// Every 60s poll (see REFRESH_INTERVAL_MS) hands `refresh()` a
// brand-new array of brand-new objects, freshly parsed from JSON —
// even when nothing actually changed since the last poll. Card and
// Column both memoize on reference equality (see Card.tsx's
// _cardPropsEqual), so passing the fetch response straight to
// setEntries/setSources/setForYou defeats that memoization every
// single tick: a full re-render (including Card's native touchmove
// listener teardown/re-attach) of every visible card and column
// once a minute, regardless of whether the feed changed. Reconciling
// against the previous state — keeping the OLD object reference for
// any row that's structurally unchanged — lets the memo comparators
// actually bail out on the common "nothing changed for this row"
// case.
type BriefTone = 'terse' | 'narrative' | 'alert'

// How long a feed saved, added or deleted in this tab overrides what a
// poll says about it (see ``sourceChangesRef``).
const SOURCE_CHANGE_TTL_MS = 120_000

// Puts a feed where the server's ``ORDER BY category, name`` would,
// without re-sorting the rest (JavaScript and Postgres order some
// strings differently, and the list shouldn't shuffle).
function insertSorted(list: Source[], row: Source): Source[] {
  const at = list.findIndex(
    (s) => s.category > row.category || (s.category === row.category && s.name > row.name),
  )
  return at < 0 ? [...list, row] : [...list.slice(0, at), row, ...list.slice(at)]
}

function reconcileById<T extends { id: number }>(prev: T[], next: T[]): T[] {
  if (prev.length === 0) return next
  const prevById = new Map(prev.map((item) => [item.id, item]))
  return next.map((item) => {
    const old = prevById.get(item.id)
    return old !== undefined && deepEqual(old, item) ? old : item
  })
}

// localStorage keys live in ``lib/storage.ts`` so feature
// modules share one namespaced key registry. Only device-local
// screen state remains there (the phone tab, Brief's collapse and
// tone) -- the rest moved to the server-backed ``user_preferences``
// store (``lib/preferences.tsx``) so all of a user's devices see
// the same state.

// The phone's stored tab: its key, or (read once, to migrate) the
// index stored before keys. ``resolveTab`` turns it into the tab shown.
function loadMobileTab(): StoredTab {
  if (typeof window === 'undefined') return null
  return readStoredTab(safeGetItem(STORAGE_KEYS.mobileTab), safeGetItem(STORAGE_KEYS.mobileColLast))
}

// A source filter as one value, so the same filter in a new Set
// compares equal.
function filterScope(sources: Set<string>): string {
  return Array.from(sources).sort().join('\n')
}

// Seen marks (``seen:<category>``) and the hide and save sets have
// their caps in ``lib/prefSync.ts``: the provider drops the oldest ids
// past a cap in the same PATCH that adds the new ones (ids App fetched
// last, ``keep``, go last), so App never trims a list itself.

// The first-deploy baseline (D33): when this device first loaded the
// read model. Stories fetched more than 48 h before it count as seen,
// locally (nothing is written to the server), so no tab opens at 99+.
// When storage refuses it, this load's time stands in.
function loadFirstRunAt(): number {
  const stored = Number(safeGetItem(STORAGE_KEYS.firstRunAt))
  if (Number.isFinite(stored) && stored > 0) return stored
  const now = Date.now()
  safeSetItem(STORAGE_KEYS.firstRunAt, String(now))
  return now
}

// No stories: Saved's (empty) Earlier, and a tab with no partition.
const NO_ENTRIES: Entry[] = []
// The desktop's For You row shows this many of For You's stories (PR4d);
// the rest are in their category columns.
const FORYOU_ROW_TOP = 6
// What every tab loads (D5): the last 2 days, at most the 25
// best-scoring stories per feed in them (D28).
const TAB_HOURS = windowHours(DEFAULT_WINDOW)
const PER_SOURCE_CAP = 25
// How many stories a wider tab fetches for itself: a category's (its cut
// is by arrival, PR4b), and For You's (which the 2-day load uses too).
const WIDER_LIMIT = 200
const WIDER_FORYOU_LIMIT = 50
const NO_SECTIONS = { unseen: NO_ENTRIES, earlier: NO_ENTRIES }

// A hidden story, kept for its Undo (``keepHiddenStories``): the story
// and its place in ``entries`` and in ``forYou`` (``lib/saved``'s
// ``Place``: its neighbours and index; null: not there). The latest
// ``KEPT_HIDDEN`` hidden stories are kept (more for a bulk hide).
type KeptStory = { entry: Entry; inEntries: Place | null; inForYou: Place | null }
const KEPT_HIDDEN = 100
type KeptSide = 'inEntries' | 'inForYou'
// What ``putBack`` takes for one list: the stories kept there with their
// places, and the places of the hidden stories still kept, which lend
// their neighbours when the list no longer holds a story's own.
function putBackItems(stories: Iterable<KeptStory>, side: KeptSide) {
  const out: { item: Entry; place: Place }[] = []
  for (const k of stories) {
    const place = k[side]
    if (place) out.push({ item: k.entry, place })
  }
  return out
}
function keptPlaces(kept: ReadonlyMap<number, KeptStory>, side: KeptSide): (id: number) => Place | undefined {
  const places = new Map<number, Place>()
  for (const [id, k] of kept) {
    const place = k[side]
    if (place) places.set(id, place)
  }
  return (id) => places.get(id)
}

// One tab's column: ``key`` names it, ``label`` is what it shows,
// ``name`` is the id its stored rows are kept under (see
// ``availableTabs``). ``sourceName`` is the feed an empty column reports
// on (Column's empty state): the one feed a filter shows, or for a
// category the feed named like it. Saved and For You have none.
type TabColumn = { key: ColKey; name: string; label: string; entries: Entry[]; sourceName?: string }

// User-saved filter preset. Captures a complete dashboard
// view: which sources are filtered, and the per-column
// sort/min-score/max-age. The shape is stable across
// versions -- existing presets on disk can be loaded into
// a newer build without migration.
export type FilterPreset = {
  id: string
  name: string
  activeSources: string[]
  columnPrefs: Record<string, ColumnPrefs>
}


// Saved-preset chip strip. Renders the user’s saved filter
// presets as horizontal chips above the column grid. Click a
// chip to apply the preset; tap the "⋯" on a chip (or right-click
// it) for rename / update / delete. Hidden when the user has no
// presets — a first-time user doesn’t see this affordance until
// they save one.
function PresetChips({
  presets,
  onApply,
  onDelete,
  onRename,
  onUpdate,
}: {
  presets: FilterPreset[]
  onApply: (preset: FilterPreset) => void
  onDelete: (id: string) => void
  onRename: (id: string, name: string) => void
  onUpdate: (id: string) => void
}) {
  if (presets.length === 0) return null
  return (
    <div
      data-hscroll
      className="flex gap-1.5 overflow-x-auto pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] sm:pl-[max(1.5rem,env(safe-area-inset-left))] sm:pr-[max(1.5rem,env(safe-area-inset-right))] border-b border-hairline bg-bg-app"
      role="toolbar"
      aria-label="saved filter presets"
    >
      <span
        // Slightly bigger on mobile (the user's primary
        // device) so the label is legible at arm's length.
        // The desktop variant is the original terse caption
        // style -- mobile users need to spot the strip on
        // first paint, desktop users treat it as decoration.
        className="shrink-0 self-center mr-1 text-ios-body sm:text-ios-caption uppercase tracking-wide text-label-secondary sm:text-label-tertiary font-medium"
        aria-hidden="true"
      >
        Saved views
      </span>
      {presets.map((p) => (
        <PresetChip
          key={p.id}
          preset={p}
          onApply={() => onApply(p)}
          onDelete={() => onDelete(p.id)}
          onRename={(name) => onRename(p.id, name)}
          onUpdate={() => onUpdate(p.id)}
        />
      ))}
    </div>
  )
}

// A 44px tap target around a smaller pill: the strip keeps its compact
// look while every chip and action is easy to hit on a phone.
const CHIP_BUTTON = 'group shrink-0 min-h-[44px] flex items-center'
const chipPill = (tone: string) => `rounded-full border px-3 py-1 text-ios-caption whitespace-nowrap group-active:opacity-60 ${tone}`

function PresetChip({
  preset,
  onApply,
  onDelete,
  onRename,
  onUpdate,
}: {
  preset: FilterPreset
  onApply: () => void
  onDelete: () => void
  onRename: (name: string) => void
  onUpdate: () => void
}) {
  // Three states: the normal chip, the "⋯" action row (rename /
  // update / delete), and delete's own confirm step. Right-click is
  // still a shortcut straight into the action row (desktop muscle
  // memory from the old delete-only version), but the explicit "⋯"
  // button is the one guaranteed to work everywhere — long-press
  // isn't wired up here, so right-click alone would leave touch
  // users with no way to rename or delete a chip at all.
  const [mode, setMode] = useState<'idle' | 'actions' | 'confirmDelete' | 'rename'>('idle')
  const [draft, setDraft] = useState(preset.name)
  const renameRef = useRef<HTMLInputElement>(null)
  // An unchanged or empty name does nothing, as before.
  const finishRename = () => {
    const trimmed = draft.trim()
    setMode('idle')
    if (!trimmed || trimmed === preset.name) return
    onRename(trimmed)
    toast(`Renamed to “${trimmed}”.`, 'info')
  }

  if (mode === 'rename') {
    return (
      <div className="shrink-0 flex items-center gap-1.5">
        <input
          ref={renameRef}
          type="text"
          aria-label={`new name for ${preset.name}`}
          enterKeyHint="done"
          autoCorrect="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              finishRename()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              setMode('idle')
            }
          }}
          className="w-44 min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary"
        />
        <button
          type="button"
          onPointerDown={(e) => e.preventDefault()}
          onMouseDown={(e) => e.preventDefault()}
          onClick={finishRename}
          className={CHIP_BUTTON}
          aria-label={`save the new name for ${preset.name}`}
        >
          <span className={chipPill('bg-accent border-accent text-white font-semibold')}>Done</span>
        </button>
        <button
          type="button"
          onClick={() => setMode('idle')}
          className="shrink-0 w-11 h-11 flex items-center justify-center text-label-tertiary active:opacity-60"
          aria-label="cancel rename"
        >
          ✕
        </button>
      </div>
    )
  }

  if (mode === 'confirmDelete') {
    return (
      <div className="shrink-0 flex items-center gap-1.5">
        <span className="text-ios-caption text-red-400">Delete?</span>
        <button
          onClick={onDelete}
          className={CHIP_BUTTON}
          aria-label={`confirm delete preset ${preset.name}`}
        >
          <span className={chipPill('bg-red-500/10 border-red-500/30 text-red-400 font-semibold')}>Yes</span>
        </button>
        <button
          onClick={() => setMode('idle')}
          className={CHIP_BUTTON}
          aria-label={`cancel delete preset ${preset.name}`}
        >
          <span className={chipPill('border-hairline text-label-secondary')}>No</span>
        </button>
      </div>
    )
  }

  if (mode === 'actions') {
    return (
      <div className="shrink-0 flex items-center gap-1.5">
        <button
          onClick={() => {
            // An inline field prefilled with the name, focused in the
            // same tap so the keyboard comes up.
            setDraft(preset.name)
            flushSync(() => setMode('rename'))
            renameRef.current?.focus()
            renameRef.current?.select()
          }}
          className={CHIP_BUTTON}
          aria-label={`rename preset ${preset.name}`}
        >
          <span className={chipPill('bg-bg-elevated border-hairline text-label-primary')}>Rename</span>
        </button>
        <button
          onClick={() => {
            onUpdate()
            setMode('idle')
            toast(`“${preset.name}” updated to the current view.`, 'info')
          }}
          className={CHIP_BUTTON}
          aria-label={`update preset ${preset.name} to the current filter and column settings`}
          title="Overwrite this view with the current filter + per-column settings"
        >
          <span className={chipPill('bg-bg-elevated border-hairline text-label-primary')}>Update</span>
        </button>
        <button
          onClick={() => setMode('confirmDelete')}
          className={CHIP_BUTTON}
          aria-label={`delete preset ${preset.name}`}
        >
          <span className={chipPill('bg-bg-elevated border-hairline text-red-400')}>Delete</span>
        </button>
        <button
          onClick={() => setMode('idle')}
          className="shrink-0 w-11 h-11 flex items-center justify-center text-label-tertiary active:opacity-60"
          aria-label="cancel"
        >
          ✕
        </button>
      </div>
    )
  }

  // Two independent pills (not one fused pill via a nested flex +
  // overflow-hidden wrapper) — that construction collapsed to a
  // sliver in practice, so this sticks to the same single-``<button>``-
  // per-pill shape the original (working) chip used, just twice.
  return (
    <div className="shrink-0 flex items-center gap-0.5">
      <button
        onClick={onApply}
        onContextMenu={(e) => {
          e.preventDefault()
          setMode('actions')
        }}
        title={`Apply preset "${preset.name}". Tap ⋯ to rename, update, or delete.`}
        className={CHIP_BUTTON}
      >
        <span className="rounded-full bg-accent-soft text-accent border border-accent-soft group-hover:bg-accent-soft/80 group-active:opacity-60 pl-3 pr-2 py-1 text-ios-caption font-medium whitespace-nowrap">
          {preset.name}
        </span>
      </button>
      <button
        onClick={() => setMode('actions')}
        aria-label={`options for preset ${preset.name}`}
        title="Rename, update, or delete this view"
        className={`${CHIP_BUTTON} min-w-[44px] justify-center`}
      >
        <span className="rounded-full bg-accent-soft text-accent/70 border border-accent-soft group-hover:bg-accent-soft/80 group-active:opacity-60 px-2 py-1 text-ios-caption">
          ⋯
        </span>
      </button>
    </div>
  )
}

export function App() {
  const [entries, setEntries] = useState<Entry[]>([])
  const [forYou, setForYou] = useState<Entry[]>([])
  // The rows a wider tab (7 days or All, PR4d) fetched for itself, by
  // tab key. Kept across tab switches, so a tab opened again draws at
  // once; only a tab on screen fetches (below, ``widerTabs``).
  const [extraByTab, setExtraByTab] = useState<ReadonlyMap<ColKey, Entry[]>>(() => new Map())
  const extraRef = useRef(extraByTab)
  extraRef.current = extraByTab
  // Bumped by every applied refresh: the wider tabs on screen fetch again.
  const [refreshTick, setRefreshTick] = useState(0)
  const [sources, setSources] = useState<Source[]>([])
  // The latest sources for the feed editor's save store, which reads
  // them outside React.
  const sourcesRef = useRef(sources)
  sourcesRef.current = sources
  // Feeds this tab saved, added or deleted, and when (ms). A poll that
  // started before the change may carry the old state; ``refresh``
  // lays these over its result for two minutes so the screen doesn't
  // step back. Ids are never reused, so a deleted id stays dropped.
  const sourceChangesRef = useRef({
    written: new Map<number, number>(),
    inserted: new Map<number, number>(),
    deleted: new Map<number, number>(),
  })
  const appliedRefreshRef = useRef(0)
  const [health, setHealth] = useState<Health | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  // Settings overlay state. Driven by the URL so the back button
  // and a reload both behave: ``?view=settings`` opens it,
  // ``&tab=<SETTINGS_TABS>`` picks the tab and ``&source=<id>`` opens
  // that feed's row in Feeds.
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('feeds')
  const [settingsSourceId, setSettingsSourceId] = useState<number | null>(null)
  // Bumped on every focus request, so asking for the same feed again
  // while Settings is open still scrolls to it.
  const [settingsFocusNonce, setSettingsFocusNonce] = useState(0)

  // Read the URL on every render. We listen for ``popstate`` and
  // our own custom events (the Settings tab buttons fire a popstate
  // after ``replaceState``) so the active tab stays in sync.
  useEffect(() => {
    const sync = () => {
      const u = new URL(window.location.href)
      const view = u.searchParams.get('view')
      if (view === 'settings') {
        setSettingsOpen(true)
        const t = u.searchParams.get('tab')
        // Only a known tab is taken, so a typo'd ``?tab=feed`` can't
        // render an overlay with no tab active.
        if (isSettingsTab(t)) setSettingsTab(t)
        const source = u.searchParams.get('source')
        setSettingsSourceId(source && /^\d+$/.test(source) ? Number(source) : null)
      } else {
        setSettingsOpen(false)
        setSettingsSourceId(null)
      }
    }
    sync()
    window.addEventListener('popstate', sync)
    return () => window.removeEventListener('popstate', sync)
  }, [])

  // Opening pushes one history entry, marked ``popping: 'settings'``,
  // so Back closes Settings. Opening again while it is open (a feed's
  // › from the Menu, a toast's Open) replaces that entry, keeping its
  // state: a Settings opened from a link has none, and Done must not
  // step back out of the app from there.
  const openSettings = useCallback((tab: SettingsTab = 'feeds', sourceId?: number) => {
    const u = new URL(window.location.href)
    const reentry = u.searchParams.get('view') === 'settings'
    u.searchParams.set('view', 'settings')
    u.searchParams.set('tab', tab)
    if (sourceId != null) u.searchParams.set('source', String(sourceId))
    else u.searchParams.delete('source')
    if (reentry) window.history.replaceState(window.history.state, '', u.toString())
    else window.history.pushState({ popping: 'settings' }, '', u.toString())
    setSettingsTab(tab)
    setSettingsSourceId(sourceId ?? null)
    setSettingsFocusNonce((n) => n + 1)
    setSettingsOpen(true)
  }, [])
  // Closes at once, then tidies the URL: back over our own entry
  // (popstate's sync agrees it is closed), or, for a Settings opened
  // from a link, a replace. Done, Esc and the backdrop never add a
  // history entry, so one Back after Done doesn't reopen Settings.
  const closeSettings = useCallback(() => {
    setSettingsOpen(false)
    setSettingsSourceId(null)
    if (window.history.state?.popping === 'settings') {
      window.history.back()
      return
    }
    const u = new URL(window.location.href)
    u.searchParams.delete('view')
    u.searchParams.delete('tab')
    u.searchParams.delete('source')
    window.history.replaceState(null, '', u.toString())
  }, [])
  // The phone's stored tab (see ``resolveTab`` for the tab shown).
  // Only ``selectTab`` writes it, from a tap, the Menu's Jump to, or
  // the resolve effect settling a tab that went.
  const [mobileTab, setMobileTab] = useState<StoredTab>(loadMobileTab)
  const selectTab = useCallback((key: ColKey) => {
    setMobileTab((prev) => (prev && 'key' in prev && prev.key === key ? prev : { key }))
    safeSetItem(STORAGE_KEYS.mobileTab, key)
    safeRemoveItem(STORAGE_KEYS.mobileColLast)
  }, [])
  // The phone tab's story list, its real scroller (Column's list, not
  // the wrapper around it, which only scrolls Brief). Set through
  // Column's ``scrollerRef``, so it changes on every tab remount and
  // hooks that need it take it as an effect dependency.
  const [phoneScroller, setPhoneScroller] = useState<HTMLElement | null>(null)
  // The desktop's one scroller: the wrapper around Brief, the chips, For
  // You, FramingWatch and the grid. Each desktop Column observes its
  // "load more" sentinel against it.
  const [desktopScroller, setDesktopScroller] = useState<HTMLElement | null>(null)
  // The wrapper that scrolls the Brief tab.
  const phoneBodyRef = useRef<HTMLDivElement | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [authProbed, setAuthProbed] = useState(false)
  const [oidcDisabled, setOidcDisabled] = useState(false)
  // Multi-source filter (empty set = no filter).
  const [activeSources, setActiveSources] = useState<Set<string>>(() => new Set())
  const [brief, setBrief] = useState<Brief | null>(null)
  const [generatingBrief, setGeneratingBrief] = useState(false)
  // Whether an AI provider is set up (``/api/llm/status``). Null while
  // loading or when the check failed: the Brief buttons then stay as
  // they are. False turns them into "Brief needs an AI provider · Set
  // up". Held here, not in BriefCard: the header Brief button needs
  // it too, and it is read again when Settings closes, so Set up →
  // pick a provider → Done brings Generate back without a reload.
  const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null)
  // Brief tone — lifted from BriefCard so the header Brief button,
  // the Drawer "Generate brief now" button, and the BriefCard pills
  // all stay in sync.
  // The Brief tone survives a reload on this device; anything stored
  // that isn't one of the three tones reads as 'terse'.
  // The desktop grid, its For You row, framing watch and inline Brief
  // render only at desktop widths; a phone renders only its own layout.
  const isDesktop = useMediaQuery('(min-width: 768px)')
  // Each tab's snapshot (lib/useFeedOrder.ts, PR3b): what it shows above
  // and below "You're all caught up", kept still while you read.
  const feed = useFeedOrder<Entry>()
  const { resetSnapshot, setHold, latest: latestPart } = feed
  // Each refresh's number; a result older than one already applied is
  // dropped. (Declared here: C1's return handler below reads it.)
  const refreshSeqRef = useRef(0)
  // C1: the refresh a long return starts resets the tabs again when it
  // lands (the stories that came in while away are sorted in, not
  // appended): every refresh numbered above this one does, unless the
  // user has done anything since the return (below).
  const resetAfterSeqRef = useRef<number | null>(null)
  // When that return was (``Date.now``), and what takes its listeners off.
  const returnAtRef = useRef(0)
  const disarmRef = useRef<(() => void) | null>(null)
  // The second reset is called off: the refresh's stories then append or
  // wait like a poll's, and nothing moves under someone already reading.
  const disarmReturnReset = useCallback(() => {
    resetAfterSeqRef.current = null
    disarmRef.current?.()
    disarmRef.current = null
  }, [])
  // Armed by a long return, until its refresh lands. A touch, a click
  // (even one that comes alone), a key or the wheel calls it off
  // (``RETURN_INPUTS``; a story opened from a card starts with one of
  // these), and so does a list moved off its top (VoiceOver scrolls
  // without them). The return's own scroll to the top leaves the list at
  // 0, so it doesn't count.
  const armReturnReset = useCallback(() => {
    disarmRef.current?.()
    resetAfterSeqRef.current = refreshSeqRef.current
    returnAtRef.current = Date.now()
    const onInput = () => disarmReturnReset()
    const onScroll = (e: Event) => {
      const t = e.target
      if (t instanceof Element && t.hasAttribute('data-scroller') && t.scrollTop > 0) disarmReturnReset()
    }
    for (const t of RETURN_INPUTS) window.addEventListener(t, onInput, { capture: true, passive: true })
    document.addEventListener('scroll', onScroll, { capture: true, passive: true })
    disarmRef.current = () => {
      for (const t of RETURN_INPUTS) window.removeEventListener(t, onInput, { capture: true })
      document.removeEventListener('scroll', onScroll, { capture: true })
    }
  }, [disarmReturnReset])
  useEffect(() => () => disarmRef.current?.(), [])
  const scrollersRef = useRef({ phone: phoneScroller, desktop: desktopScroller })
  scrollersRef.current = { phone: phoneScroller, desktop: desktopScroller }
  // C1, coming back. PR1r's update check has already started (it may
  // reload onto a new build). After RETURN_RESET_MS or more away, unless
  // the page hid within 2 s of opening a story (that story stays put for
  // ▲ or Save), every tab starts over and the lists go back to their
  // tops; App's own visibility listener then starts the silent refresh,
  // which resets the tabs once more when it lands if the user hasn't
  // done anything meanwhile.
  const onReturn = useCallback(
    ({ away, openedBefore }: Return) => {
      if (away < RETURN_RESET_MS || openedBefore) return
      resetSnapshot()
      armReturnReset()
      const { phone, desktop } = scrollersRef.current
      phone?.scrollTo({ top: 0 })
      desktop?.scrollTo({ top: 0 })
    },
    [resetSnapshot, armReturnReset],
  )
  // A newer build on the server: the phone reloads onto it when the user
  // comes back after 10 min away; the desktop shows a Reload button.
  const update = useUpdateCheck(isDesktop, onReturn)
  const [briefTone, setBriefToneState] = useState<BriefTone>(() => {
    const stored = safeGetItem(STORAGE_KEYS.briefTone)
    return stored === 'narrative' || stored === 'alert' ? stored : 'terse'
  })
  const setBriefTone = useCallback((tone: BriefTone) => {
    setBriefToneState(tone)
    safeSetItem(STORAGE_KEYS.briefTone, tone)
  }, [])
  // Kick off brief generation + poll the job-status endpoint until
  // the LLM call lands. Shared between the header Brief button and
  // BriefCard's Regenerate/Generate buttons so both surfaces get
  // the same 202+ack→poll UX. ``onError`` lets BriefCard surface a
  // local error string and the header button bubble it into the
  // toast queue.
  const triggerBriefGenerate = useCallback(
    async (tone: 'terse' | 'narrative' | 'alert', onError?: (msg: string) => void) => {
      // Local AbortController for this generation. When the user
      // navigates away (BriefCard unmounts) or starts another
      // generate, the previous loop's next tick sees the abort
      // and returns cleanly. Without this, an unmounted
      // BriefCard would keep firing setState calls — React
      // tolerates them but the loop's settled ``setGeneratingBrief(false)``
      // in the finally branch can clobber a *new* generation's
      // loading state.
      //
      // Abort any previous loop first so a fast double-tap on
      // Generate doesn't fan out into two concurrent polls.
      generationAbortRef.current?.abort()
      const ac = new AbortController()
      generationAbortRef.current = ac
      setGeneratingBrief(true)
      try {
        const ack = await api.briefGenerate(tone)
        // The user could have started another generation while the
        // POST was in flight; if so, abandon this one.
        if (ac.signal.aborted) return
        const jobId = ack.job_id
        const POLL_MS = 800
        // The server tries each AI provider in turn and may retry a
        // cut-off reply, each call allowed 60 s, so a slow provider
        // can take a couple of minutes. Waiting less dropped briefs
        // that were still on their way.
        const TIMEOUT_MS = 150_000
        const startedAt = Date.now()
        while (!ac.signal.aborted) {
          if (Date.now() - startedAt > TIMEOUT_MS) {
            onError?.('The brief is taking much longer than usual. Try again in a few minutes.')
            return
          }
          await new Promise<void>((resolve) => setTimeout(resolve, POLL_MS))
          if (ac.signal.aborted) return
          let status: Awaited<ReturnType<typeof api.briefJobStatus>>
          try {
            status = await api.briefJobStatus(jobId)
          } catch {
            // 404 here means the in-memory job ledger either
            // forgot the job (process restart, ledger cap
            // exceeded) or the backend never saw the POST. The
            // brief itself is still in the DB on success — a
            // single-process 5xx or a multi-pod fan-in could
            // land it there even if this client lost the
            // ledger reference. Try ``briefLatest`` for our
            // tone before giving up; that's a single round-trip
            // and surfaces a successfully-generated brief
            // that the user would otherwise see as a
            // misleading "no longer tracked" error.
            try {
              const latest = await api.briefLatest({ tone, limit: 1 })
              if (latest.length > 0) {
                setBrief(latest[0])
                return
              }
            } catch {
              // fall through to the error path
            }
            onError?.('Popping lost track of that brief. Try again.')
            return
          }
          if (status.status === 'completed' && status.brief) {
            setBrief(status.brief)
            // Clear any prior error banner — a fresh, successful
            // brief means the previous error (if any) is no
            // longer relevant. Without this, a previous
            // failure stays red-flagged through the new
            // success and the user has to wait for the next
            // polling tick to dismiss it.
            setError(null)
            return
          }
          if (status.status === 'failed') {
            onError?.(briefFailureMessage(status.error))
            return
          }
        }
      } catch (err) {
        onError?.(errorMessage(err))
      } finally {
        if (generationAbortRef.current === ac) {
          setGeneratingBrief(false)
        }
      }
    },
    [],
  )
  // Refresh in-flight state — drives the Refresh button's disabled
  // state so a second tap doesn't fire a parallel fetch.
  const [, setTimeTick] = useState(0)
  // Re-render the dashboard every 30s so relative timestamps
  // ("5m ago") stay fresh without manual refresh. Lightweight:
  // just flips a state value, the rest of the tree re-renders via
  // existing memoization.
  useEffect(() => {
    const id = window.setInterval(() => setTimeTick((n) => n + 1), 30_000)
    return () => window.clearInterval(id)
  }, [])
  const [refreshing, setRefreshing] = useState(false)
  // The first refresh has answered (with stories or an error). Until
  // then the tab list is incomplete: the first-load skeleton shows, and
  // the phone neither migrates its stored tab nor settles a missing one.
  const [firstRefreshDone, setFirstRefreshDone] = useState(false)
  // A refresh has brought the feed list back. Unlike ``firstRefreshDone``
  // it stays false after a failed first load, while ``sources`` is still
  // its initial empty list.
  const [sourcesLoaded, setSourcesLoaded] = useState(false)
  // The source filter (``filterScope``) the last applied refresh loaded
  // the tabs for. Until it is the filter in force, the strip still holds
  // another scope's stories, so a missing phone tab may only be late,
  // and nothing settles.
  const [tabsLoadedFor, setTabsLoadedFor] = useState<string | null>(null)
  // The last applied refresh got For You. Its request fails on its own
  // (the refresh then carries on without it), which takes the tab away
  // until the next refresh that gets it.
  const [forYouLoaded, setForYouLoaded] = useState(false)
  // Per-user server-backed preferences (``lib/preferences.tsx``).
  // Saves, hides, votes and read marks change through ``addIds`` /
  // ``removeIds`` / ``setVotes``: each sends only the change, so two
  // calls in one tick, or two devices, both land. Handlers that
  // decide on the current set read ``prefs.latest()``, which holds
  // the writes made earlier in the same tick, not a ref mirrored by
  // an effect (Column's memo keeps old callbacks, so a closure's
  // ``state`` can be renders old).
  const prefs = usePreferences()
  // Seen marks: ``seen:<category>`` rows (PR3b on) and the legacy
  // ``read_entries:<column>`` rows older builds wrote (read, never
  // written; see ``seenIds``).
  const seenEntries = prefs.state.seenEntries
  const readEntries = prefs.state.readEntries
  const columnPrefs = prefs.state.columnPrefs as Record<string, ColumnPrefs>
  const hiddenEntries = prefs.state.hiddenEntries
  const starredEntries = prefs.state.starredEntries
  const votedEntries = prefs.state.votedEntries
  const filterPresets = prefs.state.filterPresets as FilterPreset[]
  const historyGroupBy = prefs.state.historyGroupBy
  // The tab order and the hidden tabs (null: never edited, the default
  // order). Written whole, one PUT per change, by the tab editor.
  const categoryLayout = prefs.state.categoryLayout
  const setCategoryLayout = prefs.setCategoryLayout
  // Stable references to the provider's calls, aliased so the
  // handlers below read like they did. All stable across renders.
  const { addIds, removeIds, setVotes, latest: latestPrefs, registerBeforeFlush } = prefs
  const setColumnPrefs: (
    columnId: string,
    prefs: ColumnPrefsValue,
  ) => void = prefs.setColumnPrefs
  const setFilterPresets: (presets: FilterPresetValue[]) => void =
    prefs.setFilterPresets
  // ▲ / ▼ on a card (``null`` clears its vote).
  const voteEntry = useCallback(
    (entryId: number, direction: 'up' | 'down' | null) => setVotes({ [entryId]: direction }),
    [setVotes],
  )
  // Note: no local ``setHistoryGroupBy`` alias — Settings.tsx's
  // History tab calls ``usePreferences()`` directly for that setter
  // rather than receiving it as a prop from here.
  // Mirror the whole-value preferences into refs so handlers can
  // read the latest value without stale-closure bugs. (The set keys
  // are read through ``latestPrefs()`` instead.)
  // The pattern: a tiny useEffect keeps the ref in sync with
  // the state on every render; the handlers read the ref.
  const columnPrefsRef = useRef(columnPrefs)
  const filterPresetsRef = useRef(filterPresets)
  const historyGroupByRef = useRef(historyGroupBy)
  useEffect(() => {
    columnPrefsRef.current = columnPrefs
  }, [columnPrefs])
  useEffect(() => {
    filterPresetsRef.current = filterPresets
  }, [filterPresets])
  useEffect(() => {
    historyGroupByRef.current = historyGroupBy
  }, [historyGroupBy])
  // Search state. ``searchInput`` is the controlled input value;
  // ``searchQuery`` is the debounced value used for the fetch.
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Entry[]>([])
  const [searching, setSearching] = useState(false)
  // Distinct from "no matches": a network/5xx error leaves
  // ``searchResults`` empty AND sets ``searchError`` so the user
  // can tell "your query didn't match anything" from "the search
  // request blew up". Reset on each new query so a stale error
  // doesn't bleed across successive searches.
  const [searchError, setSearchError] = useState<string | null>(null)
  // Search-bar expansion. The header collapses search into a single
  // magnifying-glass icon by default; tapping it expands the input
  // to full width (matching Apple Mail's header behaviour). ``false``
  // on first mount so the large title gets the full row.
  const [searchOpen, setSearchOpen] = useState(false)
  // Keyboard selection. The column is held by its key, so a column that
  // moves (Edit tabs) keeps the selection; null is the first column.
  const [selectedColumnKey, setSelectedColumnKey] = useState<ColKey | null>(null)
  const [selectedCardId, setSelectedCardId] = useState<number | null>(null)
  // A Column folded Earlier with the selected card inside (its button, or
  // a new snapshot): the selection goes, so the shortcuts never act on a
  // card that isn't drawn. Only while it is still that card: a guard
  // only, since Column calls it from a layout effect of the very commit
  // in which that card is selected, so no path today reaches it with
  // another (no flow can pin it). Stable: Column's memo keeps callbacks.
  const dropSelection = useCallback((entryId: number) => {
    setSelectedCardId((cur) => (cur === entryId ? null : cur))
  }, [])
  // Per-card summary expansion. Session-scoped on purpose — the user
  // typically expands once to scan, and re-expanding after a reload
  // is cheap (the column has been refetched and the backend serves
  // from its own cache column on second ask). Persisting every
  // expanded card to localStorage would just be surprise.
  const [expandedSummaries, setExpandedSummaries] = useState<Set<number>>(() => new Set())
  // Keyboard shortcuts overlay. Bare ``?`` opens; Esc closes. The
  // overlay is modal so it steals focus from the dashboard's normal
  // navigation while open — opening it during a search query keeps
  // the search results mounted (the overlay sits above them, not in
  // place of them).
  const [shortcutsOpen, setShortcutsOpen] = useState(false)

  // Latest in-flight brief generation's AbortController. Each
  // generate call replaces the ref with a fresh controller so the
  // previous loop sees the abort and exits; the unmount cleanup
  // effect below aborts the still-pending one when the App goes
  // away. See triggerBriefGenerate for the consumer.
  const generationAbortRef = useRef<AbortController | null>(null)
  // Shared ref Maps for column and card DOM nodes. Keyboard nav and
  // jump-to-category both read from these — using a single shared
  // Map per axis (rather than per-component refs) means we can find
  // any card/column regardless of which is currently mounted.
  const columnRefs = useRef<Map<string, HTMLElement | null>>(new Map())
  const cardRefs = useRef<Map<number, HTMLElement | null>>(new Map())

  // sourcesByName: Map<source_name, Source>. Lets us resolve an
  // entry's category in O(1) instead of the previous O(N) ``sources.find``.
  const sourcesByName = useMemo(
    () => new Map(sources.map((s) => [s.name, s])),
    [sources],
  )
  const sourcesById = useMemo(
    () => new Map(sources.map((s) => [s.id, s.name])),
    [sources],
  )
  // Feed names the user typed, for every sourceLabel() in the app.
  useEffect(() => rememberDisplayNames(sources), [sources])
  // Same key as ``sourcesById`` but the value is the source's
  // category. Passed down to Column → Card so each card can render
  // its category-colored left stripe. Optional because older source
  // rows (pre-Phase-5) have no category in the DB until they're
  // touched — we just skip the stripe for those.
  const categoriesBySourceId = useMemo(
    () => new Map(sources.map((s) => [s.id, s.category])),
    [sources],
  )
  // Same shape, favicon path this time — lets Card fall back to the
  // source's icon as a thumbnail placeholder when the entry itself
  // has no photo (see Card.tsx's ThumbnailFallback). NULL is a valid
  // value (favicon hasn't been fetched yet), distinct from "key
  // absent" (source not found at all) — Card's ``.get()`` treats
  // both the same way (render the letter-only fallback).
  const faviconBySourceId = useMemo(
    () => new Map(sources.map((s) => [s.id, s.favicon_path])),
    [sources],
  )

  // Build a Set for O(1) hidden-entry lookup. We recompute on
  // every render where ``hiddenEntries`` changes (the underlying
  // Set is cheap to construct; the entries render path is the
  // hot loop, not this memo).
  const hiddenSet = useMemo(() => new Set(hiddenEntries), [hiddenEntries])

  // Filtered entry views. The original ``entries`` and ``forYou``
  // arrays are still kept (the ranker still sees the full set so
  // hiding an entry doesn't penalize its source), but the
  // dashboard renders the filtered versions.
  const visibleEntries = useMemo(
    () => entries.filter((e) => !hiddenSet.has(e.id)),
    [entries, hiddenSet],
  )

  const byCategory = useMemo(() => {
    const grouped = new Map<string, Entry[]>()
    for (const e of visibleEntries) {
      const cat = categoriesBySourceId.get(e.source_id) ?? 'other'
      const arr = grouped.get(cat) ?? []
      arr.push(e)
      grouped.set(cat, arr)
    }
    return grouped
  }, [visibleEntries, categoriesBySourceId])

  const visibleForYou = useMemo(
    () => forYou.filter((e) => !hiddenSet.has(e.id)),
    [forYou, hiddenSet],
  )
  // Build a Set for O(1) starred-entry lookup. Mirrors the
  // ``hiddenSet`` pattern: cheap to construct on every render,
  // saves a linear pass on each downstream filter.
  const starredSet = useMemo(() => new Set(starredEntries), [starredEntries])
  // Build a Map for O(1) vote lookup. ``votedEntries`` is keyed by
  // entry id as a string (JSON object keys are always strings); the
  // Map here re-keys by number so Card's ``vote={votedMap?.get(e.id)}``
  // lookup matches the number ids used everywhere else in this file.
  const votedMap = useMemo(
    () => new Map(Object.entries(votedEntries).map(([id, dir]) => [Number(id), dir])),
    [votedEntries],
  )
  // Entries downvoted but not yet hidden — backs the Settings ->
  // Voted tab's list + "Hide all downvoted" bulk action. Excludes
  // already-hidden entries since hiding one is exactly what removes
  // it from this backlog; without the exclusion, hidden-and-
  // downvoted entries would linger in the list forever with no way
  // to "complete" them.
  const downvotedEntries = useMemo(
    () =>
      Object.entries(votedEntries)
        .filter(([id, dir]) => dir === 'down' && !hiddenSet.has(Number(id)))
        .map(([id]) => Number(id)),
    [votedEntries, hiddenSet],
  )
  // Saved (PR4c, D5): every save, newest save first (``lib/saved``),
  // whatever the tabs loaded. A save the tabs don't hold (an old story,
  // or one saved on another device, which arrives with the preferences'
  // resync) is fetched by id into ``savedById``, and only those: a story
  // the tabs or the cache hold counts as held. Ids the server no longer
  // returns (a deleted feed's stories go with it) are noted in
  // ``savedGoneRef``, so the next poll doesn't ask for them again; an
  // unsave drops its id from there. An unsave keeps the story in the
  // cache: Saved draws only saved ids, and the unsave's Undo ("Removed
  // from Saved · Undo") then draws it again at once, with no request
  // and offline too. The cache is bounded lazily: once it holds more
  // than ``MAX_STARRED`` stories (the saves' own cap), the unsaved ones
  // leave it. Nothing is fetched until the preferences have answered
  // and the first refresh is done, so a save the tabs bring isn't
  // fetched twice.
  const [savedById, setSavedById] = useState<ReadonlyMap<number, Entry>>(() => new Map())
  const savedGoneRef = useRef(new Set<number>())
  const savedFetchingRef = useRef(new Set<number>())
  const savedReady = !prefs.loading && firstRefreshDone
  useEffect(() => {
    if (!savedReady) return
    const starred = new Set(starredEntries)
    for (const id of savedGoneRef.current) if (!starred.has(id)) savedGoneRef.current.delete(id)
    if (savedById.size > MAX_STARRED && [...savedById.keys()].some((id) => !starred.has(id))) {
      // Over the cap: the unsaved stories leave the cache (this effect
      // runs again).
      setSavedById((prev) => new Map([...prev].filter(([id]) => starred.has(id))))
      return
    }
    const held = new Set<number>()
    for (const e of entries) held.add(e.id)
    for (const e of forYou) held.add(e.id)
    const want = savedToFetch(
      starredEntries,
      (id) =>
        held.has(id) ||
        savedById.has(id) ||
        hiddenSet.has(id) ||
        savedGoneRef.current.has(id) ||
        savedFetchingRef.current.has(id),
    )
    if (want.length === 0) return
    for (const id of want) savedFetchingRef.current.add(id)
    api
      .entriesByIds(want)
      .then((rows) => {
        const got = new Set(rows.map((r) => r.id))
        for (const id of want) if (!got.has(id)) savedGoneRef.current.add(id)
        // A story unsaved while the request was out is kept too, as an
        // unsave keeps it (its Undo draws it); Saved doesn't draw it.
        if (rows.length === 0) return
        setSavedById((prev) => {
          const next = new Map(prev)
          for (const r of rows) next.set(r.id, r)
          return next
        })
      })
      .catch(() => {
        // Saved shows what it holds; the next poll asks again.
      })
      .finally(() => {
        for (const id of want) savedFetchingRef.current.delete(id)
      })
  }, [savedReady, starredEntries, entries, forYou, savedById, hiddenSet])
  // Saved's stories. Each save is drawn from the tabs' copy (the newest),
  // else the by-id cache, else the copy drawn last time: a save a poll
  // just dropped stays while its fetch is out. Hidden ones are left out
  // (a star + a hide is "I saved this but don't want to see it now":
  // hide wins), and so are stories of a deleted feed. This is Saved's
  // only order: ``sortEntries`` hands Saved back as it came. While the
  // same stories are drawn, the same array comes back, so a poll that
  // changed nothing in Saved doesn't redraw it (Column's memo compares
  // by reference).
  const savedLastRef = useRef<Entry[]>(NO_ENTRIES)
  const visibleStarred = useMemo(() => {
    const held = new Map<number, Entry>()
    for (const e of forYou) held.set(e.id, e)
    for (const e of entries) held.set(e.id, e)
    const prev = savedLastRef.current
    const last = new Map(prev.map((e) => [e.id, e]))
    const gone = savedGoneRef.current
    const out = savedStories(
      starredEntries,
      (id) => held.get(id) ?? savedById.get(id) ?? (gone.has(id) ? undefined : last.get(id)),
      (e) => hiddenSet.has(e.id) || (sourcesLoaded && !sourcesById.has(e.source_id)),
    )
    return out.length === prev.length && out.every((e, i) => e === prev[i]) ? prev : out
  }, [entries, forYou, starredEntries, savedById, hiddenSet, sourcesLoaded, sourcesById])
  savedLastRef.current = visibleStarred

  // Apollo-style 3-surface model:
  //   1. For You     — top row on the dashboard, the personal front page.
  //   2. All Subs    — by-category grid (desktop) / single-column swipe (mobile).
  //   3. Multi-Sub   — when the user picks 1+ sources, the dashboard
  //                    replaces For You + categories with a single
  //                    filtered column. Empty filter = back to #1 + #2.
  //
  // ``viewKind`` is the derived switch. ``activeSources.size > 0`` flips
  // us into Multi-Sub; otherwise we're in All (which still owns For You
  // as its first column on mobile).
  const viewKind: 'all' | 'multisub' = activeSources.size > 0 ? 'multisub' : 'all'

  // All-subs tabs: Saved and For You, always (empty, they say "No
  // stories yet"), then a tab per category with stories. The desktop
  // grid renders For You as a separate row above the category columns
  // (see the render block); on mobile the For You column *is* the For
  // You row, one tab of the strip.
  //
  // ``key`` names the tab everywhere in memory (React keys, the
  // per-column maps, the phone's stored tab); ``label`` is what it
  // shows; ``name`` is only the id its stored rows are kept under
  // (``read_entries:<name>``, ``column_prefs:<name>``, …), unchanged so
  // what is stored stays readable by every build.
  //
  // A category has a tab while an active feed is in it, stories or not
  // (PR4d, D5: a quiet category keeps its tab, and with it the one
  // control that widens its window), or while stories of it are held (a
  // feed paused just now keeps its stories until the next poll). A
  // category whose feeds are all paused leaves. (PR5b leaves muted feeds
  // out of the union.)
  const categories = useMemo(() => {
    const out = new Set<string>()
    for (const s of sources) if (s.active && s.category) out.add(s.category)
    for (const cat of byCategory.keys()) out.add(cat)
    return out
  }, [sources, byCategory])
  const availableTabs = useMemo<TabColumn[]>(() => {
    const out: TabColumn[] = [
      { key: SAVED_KEY, name: 'Saved', label: 'Saved', entries: visibleStarred },
      { key: FORYOU_KEY, name: 'For You', label: 'For You', entries: visibleForYou },
    ]
    for (const cat of categories) {
      out.push({ key: colKey(cat), name: cat, label: categoryLabel(cat), entries: byCategory.get(cat) ?? NO_ENTRIES, sourceName: cat })
    }
    return out
  }, [visibleForYou, byCategory, categories, visibleStarred])
  // The strip's order (``lib/categories``): Brief, Saved, For You, then
  // the categories by label until the owner edits it; then as stored,
  // with a new category at the end. ``shown`` leaves out the hidden
  // tabs and is what everything that walks the tabs uses: the phone's
  // strip and its fresh-device pick, the desktop grid, Jump to column
  // and the arrow keys. Hiding a tab doesn't take its stories out of
  // For You.
  const tabOrder = useMemo(() => orderColumns(availableTabs, categoryLayout), [availableTabs, categoryLayout])
  const allSubsColumns = tabOrder.shown

  // Multi-sub column. One column, name reads "Filtering" so it stays
  // distinct from any category. The chip-bar header above the column
  // carries the actual source list (see render).
  //
  // Client-side filter: the backend returns up to 200 entries
  // (all sources) on every refresh, and we filter by
  // ``activeSources`` here. The previous comment claimed the
  // backend filtered at fetch time, but the only place that
  // happens is the ``api.entries({ source: ... })`` call inside
  // ``refresh()`` — and ``refresh()`` is NOT triggered by
  // filter toggles (it's on a 60s auto-refresh + manual button).
  // So if we relied on the backend filter, the multisub column
  // would show stale data until the next refresh fired. The
  // client-side filter is always up-to-date with the current
  // ``activeSources`` state.
  //
  // The filter uses the entry's source_id mapped to its source
  // name (via ``sourcesById``) because ``activeSources`` is a
  // ``Set<string>`` of names, not ids. Entries whose source is
  // not in the map (e.g. a source that was deleted) are
  // excluded — safer than including them and showing the user
  // "ghost" entries.
  // Build a human-readable column name from the active
  // sources. ``[...activeSources]`` is sorted
  // alphabetically so the same set of sources always
  // produces the same name (stable display).
  const multisubColumnName = useMemo(() => {
    const names = Array.from(activeSources).sort()
    if (names.length === 0) return 'Filtering'
    if (names.length === 1) return names[0]
    if (names.length === 2) return `${names[0]} + 1`
    return `${names[0]} + ${names.length - 1} more`
  }, [activeSources])

  const multisubColumn = useMemo<TabColumn[]>(() => {
    if (activeSources.size === 0) {
      // Defensive: this branch shouldn't be reached because
      // ``viewKind`` switches to 'all' when ``activeSources``
      // is empty, but we keep the empty case explicit so
      // a stale memo doesn't accidentally render a
      // unfiltered column.
      return [{ key: FILTER_KEY, name: multisubColumnName, label: categoryLabel(multisubColumnName), entries: [] }]
    }
    const filtered = visibleEntries.filter((e) => {
      const name = sourcesById.get(e.source_id)
      return name != null && activeSources.has(name)
    })
    // One feed: an empty filter says how that feed is doing.
    const sourceName = activeSources.size === 1 ? Array.from(activeSources)[0] : undefined
    return [{ key: FILTER_KEY, name: multisubColumnName, label: categoryLabel(multisubColumnName), entries: filtered, sourceName }]
  }, [visibleEntries, activeSources, sourcesById, multisubColumnName])

  const baseColumns = viewKind === 'multisub' ? multisubColumn : allSubsColumns

  // A tab's prefs: its stored row over the tab's defaults (For You
  // defaults to Best, every other tab to Newest), with an older
  // build's ``top`` or ``oldest`` read as that default. Module
  // constants or one cached object per row, so Column's memo holds.
  const prefsFor = useCallback(
    (col: { key: ColKey; name: string }) => tabPrefsFor(columnPrefs, col),
    [columnPrefs],
  )

  // Apply per-column prefs: the tab's window (PR4d, D5: the stories
  // that arrived inside it; the tabs load 2 days, so 1 day is a cut of
  // that and 7 days or All add the rows ``extraByTab`` fetched, below),
  // then the tab's sort, once (each tab's snapshot keeps this order).
  // Saved is never cut or sorted: it lists every save. ``order`` names
  // both for the tab's snapshot: a change of either starts a new one
  // (lib/feedOrder.ts).
  const columns = useMemo<Array<TabColumn & { order: string; sort: Sort; window: Window }>>(() => {
    const now = Date.now()
    return baseColumns.map((col) => {
      const prefs = prefsFor(col)
      const tabWindow: Window = col.key === SAVED_KEY ? 'all' : prefs.window
      let filtered = col.entries
      if (tabWindow !== 'all') filtered = filtered.filter((e) => inWindow(e.fetched_at, tabWindow, now))
      const sort = normalizeSort(prefs.sort, col.key)
      filtered = sortEntries(filtered, sort, col.key)
      return {
        key: col.key,
        name: col.name,
        label: col.label,
        entries: filtered,
        sourceName: col.sourceName,
        sort,
        window: tabWindow,
        order: `${sort}|${tabWindow}`,
      }
    })
  }, [baseColumns, prefsFor])
  // The columns the arrow keys walk (``selectedColumnKey``): on the
  // desktop the grid's, which leaves out For You in the 'all' view (it
  // is the row above the grid, with no selection); on the phone the
  // strip's.
  const keyColumns = useMemo(
    () => (isDesktop && viewKind === 'all' ? columns.filter((c) => c.key !== FORYOU_KEY) : columns),
    [columns, isDesktop, viewKind],
  )
  // The selected column's place in ``keyColumns``, and the column itself.
  // A key that is gone selects nothing until the effect below moves it.
  const keyIndex = keyColumns.findIndex((c) => c.key === selectedColumnKey)
  const selectedColumnIndex = keyIndex >= 0 ? keyIndex : 0
  const selectedKeyColumn: TabColumn | undefined =
    keyIndex >= 0 ? keyColumns[keyIndex] : selectedColumnKey == null ? keyColumns[0] : undefined

  // Seen (PR3b, D3): a story is used up once it is opened (or by Mark
  // all; from PR3c by scrolling past it on the phone). A mark is stored
  // under the story's category, ``seen:<category>``, wherever it was
  // seen, so it holds in every tab that shows the story (its category,
  // For You, a filter, search). Older builds marked stories read per
  // column (``read_entries:<column>``): those rows still count, and are
  // never written again.
  const seenIds = useMemo(() => {
    const out = new Set<number>()
    for (const map of [seenEntries, readEntries]) {
      for (const ids of Object.values(map)) for (const id of ids) out.add(id)
    }
    return out
  }, [seenEntries, readEntries])
  // D33: stories fetched more than 48 h before this device's first load
  // of the read model count as seen too (locally only).
  const [firstRunAt] = useState(loadFirstRunAt)
  const isSeen = useMemo(() => seenTest(seenIds, firstRunAt), [seenIds, firstRunAt])

  // The stories held now, for a manual refresh's "N new" (the ids held
  // before its request), and for a hide to keep the story its Undo puts
  // back (``keepHiddenStories``).
  const storiesRef = useRef({ entries, forYou, savedById })
  storiesRef.current = { entries, forYou, savedById }
  // A hide's Undo puts the story back (PR4c). From PR4b the server leaves
  // hidden stories out of every list, so a poll between the hide and the
  // Undo drops the story from ``entries`` and ``forYou``; only the tab's
  // snapshot would still draw it, and only until the next one. A hide
  // keeps the story and its places (``hiddenStoriesRef``, the latest
  // ``KEPT_HIDDEN``); the Undo puts it back there (``restoredRef``), and
  // for two minutes a poll that doesn't return it yet (the server
  // answered before the Undo's change reached it) lays it back over its
  // result, as ``sourceChangesRef`` does for feeds.
  const hiddenStoriesRef = useRef(new Map<number, KeptStory>())
  const restoredRef = useRef(new Map<number, KeptStory & { at: number }>())
  // The last applied refresh got For You (``forYouLoaded``, for the
  // handlers): while it didn't, For You is [] until it answers, and an
  // Undo leaves the story to be laid over that answer.
  const forYouOkRef = useRef(false)

  // ``silent`` skips the toasts (errors still set the banner): Settings
  // refreshes after adding a feed, and its own "Added …" toast should
  // stay up instead of being replaced by "Nothing new".
  // A refresh that isn't silent (Refresh, ``r``) also starts every tab
  // over once its stories land: what arrived is sorted into place and
  // what was read goes under the marker (the phone's list goes back to
  // its top with it: Column). Polls never do.
  // Each refresh also reads what changed in the preferences since the
  // last read (a save made on another device shows within a poll).
  const resyncPrefs = prefs.resync
  const refresh = useCallback(async (opts?: { silent?: boolean }) => {
    setRefreshing(true)
    void resyncPrefs()
    const seq = ++refreshSeqRef.current
    const startedAt = Date.now()
    const held = new Set([...storiesRef.current.entries, ...storiesRef.current.forYou].map((x) => x.id))
    let forYouOk = true
    try {
      const sourceArg = activeSources.size > 0 ? Array.from(activeSources) : undefined
      // A flat global `limit` on the unfiltered "give me everything"
      // fetch lets one high-volume or high-scoring category (Hacker
      // News' 5-minute refresh, say) crowd a slower/lower-scoring one
      // out of the response entirely once the table has enough rows
      // — the category's tab still shows up, but the column itself
      // renders almost nothing, because its actual rows never made
      // the cut. The
      // multi-source filter view doesn't have this problem (it's
      // already scoped to a handful of sources), so only the
      // unfiltered path uses ``perCategoryLimit`` instead of ``limit``.
      // Every tab loads the last 2 days (PR4d, D5), at most the 25
      // best-scoring stories per feed (D28), from the feeds that aren't
      // paused. A wider tab fetches its own rows (``extraByTab``).
      const entriesReq = sourceArg
        ? api.entries({ limit: 200, source: sourceArg, maxAgeHours: TAB_HOURS, perSourceCap: PER_SOURCE_CAP, activeOnly: true })
        : api.entries({ perCategoryLimit: 100, maxAgeHours: TAB_HOURS, perSourceCap: PER_SOURCE_CAP, activeOnly: true })
      // Hot path: entries + sources + for-you. These change with
      // every ingest and the user expects them to be near-real-time
      // when the dashboard is open.
      const [rawEntries, rawSources, rawForYou] = await Promise.all([
        entriesReq,
        api.sources(),
        api.forYou({ limit: WIDER_FORYOU_LIMIT, maxAgeHours: TAB_HOURS }).catch(() => {
          forYouOk = false
          return [] as Entry[]
        }),
      ])
      // A newer refresh already landed: this one's data is older.
      if (seq < appliedRefreshRef.current) return
      appliedRefreshRef.current = seq
      const changes = sourceChangesRef.current
      const now = Date.now()
      for (const m of [changes.written, changes.inserted, changes.deleted]) {
        for (const [id, at] of m) if (now - at > SOURCE_CHANGE_TTL_MS) m.delete(id)
      }
      const isDeleted = (id: number) => changes.deleted.has(id)
      const liveSources = rawSources.filter((row) => !isDeleted(row.id))
      let e = rawEntries.filter((x) => !isDeleted(x.source_id))
      let fy = rawForYou.filter((x) => !isDeleted(x.source_id))
      // Stories a hide's Undo put back, until the server returns them
      // (a failed For You is [], so only lists that answered count). Not
      // a story whose feed is gone, here or on another device: it would
      // fall into an "Other" tab. Not over a failed For You either: that
      // isn't an answer, and the story waits for the next one.
      // Each goes back at its place, as the Undo put it (``putBack``).
      const hiddenNow = new Set(latestPrefs().hiddenEntries)
      const feedIds = new Set(liveSources.map((row) => row.id))
      const gotIds = new Set([...e, ...fy].map((x) => x.id))
      const lay: KeptStory[] = []
      for (const [id, r] of restoredRef.current) {
        if (gotIds.has(id) || now - r.at > SOURCE_CHANGE_TTL_MS || hiddenNow.has(id) || !feedIds.has(r.entry.source_id)) {
          restoredRef.current.delete(id)
          continue
        }
        lay.push(r)
      }
      if (lay.length > 0) {
        const kept = hiddenStoriesRef.current
        e = putBack(e, putBackItems(lay, 'inEntries'), keptPlaces(kept, 'inEntries'))
        if (forYouOk) fy = putBack(fy, putBackItems(lay, 'inForYou'), keptPlaces(kept, 'inForYou'))
      }
      setEntries((prev) => reconcileById(prev, e))
      setSources((prev) => {
        const local = new Map(prev.map((row) => [row.id, row]))
        const since = (m: Map<number, number>, id: number) => (m.get(id) ?? 0) >= startedAt
        // Saved here after this poll started: the local row is newer.
        let next = liveSources.map((row) => (since(changes.written, row.id) && local.has(row.id) ? local.get(row.id)! : row))
        // Added here after this poll started and missing from it.
        const got = new Set(next.map((row) => row.id))
        for (const row of prev) {
          if (!got.has(row.id) && since(changes.inserted, row.id)) next = insertSorted(next, row)
        }
        return reconcileById(prev, next)
      })
      setSourcesLoaded(true)
      setForYou((prev) => reconcileById(prev, fy))
      setFirstRefreshDone(true)
      setTabsLoadedFor(filterScope(activeSources))
      setForYouLoaded(forYouOk)
      forYouOkRef.current = forYouOk
      // The wider tabs on screen fetch their rows again (below).
      setRefreshTick((n) => n + 1)
      setError(null)
      // C1: the refresh a long return started lands: start the tabs
      // over again, so what came in while away is sorted in. Not when the
      // user did anything first (``disarmReturnReset``), nor when a story
      // was opened since the return, by any path: that story stays in its
      // place for ▲ or Save, as the open guard keeps it.
      const armed = resetAfterSeqRef.current != null && seq > resetAfterSeqRef.current
      const afterReturn = armed && lastStoryOpenAt() < returnAtRef.current
      if (armed) disarmReturnReset()
      if (!opts?.silent || afterReturn) resetSnapshot()
      // Surface a brief success toast so the user knows the pull
      // actually happened: how many stories it brought that weren't
      // here before it was asked for, or "Nothing new". (PR6b replaces
      // it with its status row.)
      if (!opts?.silent) {
        const fresh = new Set([...e, ...fy].map((x) => x.id).filter((id) => !held.has(id)))
        toast(fresh.size > 0 ? `${fresh.size} new` : 'Nothing new', 'info')
      }
    } catch (err) {
      // Plain words, never the raw "<status> for <url>" text.
      const message = errorMessage(err)
      setError(message)
      setFirstRefreshDone(true)
      // A long return's refresh that failed: the reset on the return
      // stands; a later poll must not reset again.
      if (resetAfterSeqRef.current != null && seq > resetAfterSeqRef.current) disarmReturnReset()
      if (!opts?.silent) toast(message, 'error')
    } finally {
      setRefreshing(false)
    }
  }, [activeSources, resyncPrefs, resetSnapshot, disarmReturnReset, latestPrefs])
  // The latest ``refresh`` for callers that must stay stable: Column's
  // memo ignores callbacks, so it would keep calling an old one with
  // an old filter.
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh

  // Cold path: health pings (DB / sources count). Pulled on
  // its own slower interval so a single 5xx doesn't sit visible
  // for 60s on every entry-poll failure. ``refreshHealth`` is a
  // separate useCallback so the hot-path effect doesn't re-attach
  // when only the slow poller has fired.
  const refreshHealth = useCallback(async () => {
    try {
      const h = await api.health()
      setHealth(h)
    } catch {
      // Health is informational; an outage of the health endpoint
      // doesn't break the dashboard. Leave the previous value in
      // place and let the next tick try again.
    }
  }, [])

  // Wipe every namespaced localStorage key and the reading-state
  // preferences, then reload. Settings → Reset's button. A full
  // page reload (rather than local state reset + re-render)
  // because every component has its own state mirrors; rebuilding
  // them inline is more code than ``location.reload()`` and
  // prone to one-component-behind-another race conditions.
  const discardPrefs = prefs.discardPending
  const resetLocalState = useCallback(() => {
    try {
      // Wipe everything under the ``popping.`` namespace. Use
      // ``localStorage.key`` + prefix-match rather than enumerating
      // STORAGE_KEYS so any future key added to storage.ts is
      // cleared too. Falls through to a no-op in SSR / private-mode
      // where ``localStorage.key`` doesn't exist.
      if (typeof window !== 'undefined' && window.localStorage) {
        const toRemove: string[] = []
        for (let i = 0; i < window.localStorage.length; i++) {
          const k = window.localStorage.key(i)
          if (k && k.startsWith('popping.')) toRemove.push(k)
        }
        for (const k of toRemove) window.localStorage.removeItem(k)
      }
    } catch {
      // Quota / private-mode — best effort; the reload still
      // happens and re-fetches everything.
    }
    // Server-backed reading state needs an explicit reset too --
    // the localStorage wipe above only affects what's still
    // on this device. We DELETE the seen marks (``seen:*``, and
    // the ``read_entries:*`` rows older builds wrote), last-viewed
    // times, column prefs and section folds (every per-column
    // row), plus the History grouping, then reload; the next
    // mount's GET reads what is left. Saves, hides, votes and
    // saved views are kept: they are the user's choices, not
    // state, and a Reset that took them could not be undone.
    // ``Promise.allSettled`` so a single failure doesn't block
    // the rest of the reset (the reload happens regardless).
    const allKeys: string[] = [PREFERENCE_KEYS.historyGroupBy]
    // Per-column keys: the keys we know about from the
    // current state (writes still queued included) plus the five
    // per-column kinds.
    const perColumnKinds = [
      PREFERENCE_KEYS.seen,
      PREFERENCE_KEYS.readEntries,
      PREFERENCE_KEYS.lastViewed,
      PREFERENCE_KEYS.columnPrefs,
      PREFERENCE_KEYS.columnSections,
    ]
    const columnIds = new Set<string>()
    const m = latestPrefs()
    for (const k of perColumnKinds) {
      if (k === PREFERENCE_KEYS.seen) {
        for (const id of Object.keys(m.seenEntries)) columnIds.add(id)
      } else if (k === PREFERENCE_KEYS.readEntries) {
        for (const id of Object.keys(m.readEntries)) columnIds.add(id)
      } else if (k === PREFERENCE_KEYS.lastViewed) {
        for (const id of Object.keys(m.lastViewed)) columnIds.add(id)
      } else if (k === PREFERENCE_KEYS.columnPrefs) {
        for (const id of Object.keys(m.columnPrefs)) columnIds.add(id)
      } else if (k === PREFERENCE_KEYS.columnSections) {
        for (const id of Object.keys(m.columnSections)) columnIds.add(id)
      }
    }
    for (const id of columnIds) {
      for (const k of perColumnKinds) {
        allKeys.push(`${k}:${id}`)
      }
    }
    // The keys Reset deletes, by kind. Their queued writes are dropped
    // before the DELETEs: sent after them (the reload's leave flush sends
    // what is queued) they would write the rows back. Every other key's
    // queued writes (a save, a hide, a vote, a saved view) go first, and
    // the writes on the way are waited for (at most 3 s).
    const resetDeletes = (key: string) =>
      key === PREFERENCE_KEYS.historyGroupBy || perColumnKinds.some((k) => key.startsWith(`${k}:`))
    void discardPrefs(resetDeletes)
      .then(() => Promise.allSettled(allKeys.map((k) => api.deletePreference(k))))
      .finally(() => {
        window.location.reload()
      })
  }, [latestPrefs, discardPrefs])

  // Probe auth state once on mount.
  useEffect(() => {
    let cancelled = false
    api.me()
      .then((u) => {
        if (cancelled) return
        setUser(u)
        setOidcDisabled(false)
        setAuthProbed(true)
      })
      .catch(() => {
        if (cancelled) return
        setOidcDisabled(true)
        setAuthProbed(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // The AI status: once signed in, again whenever Settings closes (the
  // AI tab may have just set a provider up) and when the app comes back
  // to the front. Only the latest answer counts. A failed check is null,
  // which shows the normal buttons; Generate then reports the problem.
  const llmStatusSeqRef = useRef(0)
  const loadLlmStatus = useCallback(() => {
    const seq = ++llmStatusSeqRef.current
    api
      .llmStatus()
      .then((s) => {
        if (seq === llmStatusSeqRef.current) setLlmConfigured(s.configured)
      })
      .catch(() => {
        if (seq === llmStatusSeqRef.current) setLlmConfigured(null)
      })
  }, [])
  const signedIn = authProbed && (oidcDisabled || user !== null)
  useEffect(() => {
    if (!signedIn) return
    loadLlmStatus()
    const onVisibility = () => {
      if (document.visibilityState === 'visible') loadLlmStatus()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [signedIn, loadLlmStatus])
  const settingsWasOpenRef = useRef(settingsOpen)
  useEffect(() => {
    const was = settingsWasOpenRef.current
    settingsWasOpenRef.current = settingsOpen
    if (was && !settingsOpen && signedIn) loadLlmStatus()
  }, [settingsOpen, signedIn, loadLlmStatus])

  // Visibility-aware polling. The interval only runs while the tab
  // is visible; on return we force a refresh so the user sees fresh
  // data immediately. The interval cleanup runs on unmount.
  //
  // ``pollingMountedRef`` gates the initial ``refresh()`` call so the
  // first fetch belongs to this effect, but re-mounts triggered by
  // ``refresh`` recreating (e.g. when ``activeSources`` changes)
  // don't issue a duplicate fetch — the source-filter effect below
  // owns that case.
  const pollingMountedRef = useRef(false)
  useEffect(() => {
    if (!authProbed) return

    let intervalId: number | null = null

    const startPolling = () => {
      if (intervalId != null) return
      intervalId = window.setInterval(() => {
        void refresh({ silent: true })
      }, REFRESH_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (intervalId != null) {
        window.clearInterval(intervalId)
        intervalId = null
      }
    }

    // (C1's reset on a long return runs before this, from the update
    // check's listener, which is registered first: useUpdateCheck's
    // effect runs on App's first commit, this one only once auth is
    // probed.)
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        stopPolling()
      } else {
        // Back in front: every preference row, which is also how rows
        // deleted elsewhere (Reset) go. The refresh's own poll joins it.
        void resyncPrefs({ full: true })
        void refresh({ silent: true })
        startPolling()
      }
    }

    document.addEventListener('visibilitychange', onVisibility)
    if (!pollingMountedRef.current) {
      pollingMountedRef.current = true
      void refresh({ silent: true })
    }
    if (document.visibilityState === 'visible') startPolling()

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      stopPolling()
    }
  }, [refresh, authProbed, resyncPrefs])

  // One-shot initial health fetch so the Drawer status chip renders
  // without waiting up to 5 min for the cold poller to tick. Mount-
  // gated via ``authProbed`` so we don't fire before auth state is
  // resolved (avoids a 401-then-retry race on OIDC deploys).
  useEffect(() => {
    if (!authProbed) return
    void refreshHealth()
  }, [authProbed, refreshHealth])

  // Cold health poller. 5 min cadence is plenty for the Drawer
  // status chip; pulling faster just costs an extra round-trip per
  // page-open with no real change to the user-visible state. The
  // hot poller (above) doesn't re-run when only this fires because
  // ``refreshHealth`` is a stable callback. Errors are swallowed —
  // a broken health endpoint shouldn't break the dashboard.
  useEffect(() => {
    if (!authProbed) return
    let id: number | null = null
    const start = () => {
      if (id != null) return
      id = window.setInterval(() => {
        if (document.visibilityState === 'visible') void refreshHealth()
      }, HEALTH_INTERVAL_MS)
    }
    const stop = () => {
      if (id != null) {
        window.clearInterval(id)
        id = null
      }
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') stop()
      else start()
    }
    document.addEventListener('visibilitychange', onVisibility)
    if (document.visibilityState === 'visible') start()
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      stop()
    }
  }, [refreshHealth, authProbed])

  // Re-fetch whenever the active source filter changes. Without this,
  // tapping a chip in the Drawer or in the chip-bar would update
  // ``activeSources`` but leave ``entries`` showing whatever the
  // previous fetch returned — the user sees the same unfiltered list
  // until the 60s polling tick (or a visibility change) finally pulls
  // the now-filtered set. The polling effect above owns the initial
  // fetch, so we skip our own first run via a ref.
  //
  // Debounced 250ms so rapid chip taps (the Drawer renders all
  // source chips at once and tap-spamming is common) coalesce into
  // a single fetch. Without this, a "deselect bbc, tap cbc, tap reuters"
  // burst fires three back-to-back 200-row pulls.
  const initialRefreshDoneRef = useRef(false)
  useEffect(() => {
    if (!authProbed) return
    if (!initialRefreshDoneRef.current) {
      initialRefreshDoneRef.current = true
      return
    }
    const id = window.setTimeout(() => {
      void refresh({ silent: true })
    }, 250)
    return () => window.clearTimeout(id)
  }, [activeSources, refresh, authProbed])

  // The arrow keys never target a column that has gone (hidden, or out
  // of the view): the selection moves to the column now at its last
  // place, or the last one, and its card is dropped, since that card
  // belonged to the column that went.
  const lastKeyIndexRef = useRef(0)
  useEffect(() => {
    if (selectedColumnKey == null) return
    const i = keyColumns.findIndex((c) => c.key === selectedColumnKey)
    if (i >= 0) {
      lastKeyIndexRef.current = i
      return
    }
    const fallback = keyColumns[Math.min(lastKeyIndexRef.current, keyColumns.length - 1)]
    setSelectedColumnKey(fallback?.key ?? null)
    setSelectedCardId(null)
  }, [keyColumns, selectedColumnKey])

  // The phone's tabs in strip order: Brief, then the columns.
  const phoneTabs = useMemo<Tab[]>(
    () => [{ key: BRIEF_KEY, size: 0 }, ...columns.map((c) => ({ key: c.key, size: c.entries.length }))],
    [columns],
  )
  // Where in the strip the stored tab last stood, so a tab that goes is
  // replaced by its neighbour (the tab now there). Only a commit that
  // showed the stored tab itself sets it: a stand-in shown while
  // loading or under the filter is not the stored tab's place, and a
  // stored tab never seen (its category went while the phone was away)
  // gets the fresh-device tab, not Brief.
  const [lastTabIndex, setLastTabIndex] = useState<number | null>(null)
  // The tab shown in place of the stored one while nothing can be
  // stored (``tabsLoaded`` false, or under the filter): a neighbour, a
  // fresh device's first tab, or an old index's tab. In memory only.
  // ``resolveTab`` keeps showing it by key until the list settles, so a
  // tab appearing in front of it (a category's first stories) can't move
  // the phone, however long For You or the preferences stay down.
  const [standIn, setStandIn] = useState<ColKey | null>(null)
  // The tab the phone shows. It follows the stored key, so tabs
  // appearing, emptying, moving or hidden around it, or a feed moving
  // between categories, never move the phone. Nothing is migrated or settled
  // until preferences have loaded and the first refresh has answered,
  // and the last refresh applied loaded every tab for the filter in
  // force: before that a stored tab can be missing only because its
  // stories haven't arrived.
  //
  // While For You's request fails, only what hangs on For You waits: a
  // stored For You, a fresh device's first tab (For You first), and an
  // old index (counted with For You in its place). Any other tab still
  // settles, so a For You that stays down can't bring back the jump.
  // When it is back, they resolve as they would have (a fresh device
  // opens For You), as a stored For You does.
  //
  // The preferences must have loaded, not only answered: when their
  // GET fails, Saved (built from them) is missing only because it
  // wasn't read, and settling would replace a stored Saved for good or
  // migrate an old index one place off. The phone then shows a
  // stand-in and stores nothing until a tap.
  const storedKey = mobileTab && 'key' in mobileTab ? mobileTab.key : null
  const activeScope = useMemo(() => filterScope(activeSources), [activeSources])
  const filterActive = viewKind === 'multisub'
  const tabsLoaded =
    !prefs.loading &&
    prefs.loaded &&
    firstRefreshDone &&
    tabsLoadedFor === activeScope &&
    (forYouLoaded || (storedKey != null && storedKey !== FORYOU_KEY))
  const tabChoice = useMemo(
    () => resolveTab(phoneTabs, mobileTab, lastTabIndex, { loaded: tabsLoaded, filterActive, standIn }),
    [phoneTabs, mobileTab, lastTabIndex, tabsLoaded, filterActive, standIn],
  )
  const shownTab: ColKey = tabChoice.key ?? BRIEF_KEY
  // The shown column (-1 and undefined on Brief).
  const phoneColIndex = columns.findIndex((c) => c.key === shownTab)
  const phoneCol = phoneColIndex >= 0 ? columns[phoneColIndex] : undefined
  useEffect(() => {
    if (storedKey != null && tabChoice.key === storedKey) setLastTabIndex(tabChoice.index)
    // Hold the stand-in once the phone has shown it (after the first
    // refresh; the skeleton covers the strip before that). Brief is
    // never held, and nothing is held once the list settles.
    const unsettled = !tabsLoaded || filterActive
    const shownInstead = tabChoice.key != null && tabChoice.key !== storedKey && tabChoice.key !== BRIEF_KEY
    setStandIn(unsettled && firstRefreshDone && shownInstead ? tabChoice.key : null)
    // A tab that went, a fresh device's first tab, or an old index:
    // the tab shown becomes the stored one. Only on the phone: the
    // desktop never shows the strip, so it leaves the phone's tab alone.
    if (tabChoice.store && !isDesktop) selectTab(tabChoice.store)
  }, [tabChoice, storedKey, tabsLoaded, filterActive, firstRefreshDone, isDesktop, selectTab])
  // A tap on another tab selects it. A tap on the tab shown scrolls it
  // to the top (at once under reduced motion), and selects it when it
  // only stands in for the stored tab (while loading), except the
  // filter's column, which stands in only until the filter is cleared.
  const onPhoneTabTap = useCallback(
    (key: ColKey) => {
      if (key !== shownTab) {
        selectTab(key)
        return
      }
      if (key !== storedKey && key !== FILTER_KEY) selectTab(key)
      const el = key === BRIEF_KEY ? phoneBodyRef.current : phoneScroller
      const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
      el?.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' })
    },
    [shownTab, storedKey, phoneScroller, selectTab],
  )
  // The shown tab's chip stays in the strip's view when code picks it
  // (Jump to column, a tab that went, a move in Edit tabs), not only
  // when a finger does. ``nearest`` leaves a chip already in view alone.
  const stripRef = useRef<HTMLDivElement | null>(null)
  const stripOrder = columns.map((c) => c.key).join('\u0000')
  useEffect(() => {
    stripRef.current
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ inline: 'nearest', block: 'nearest' })
  }, [shownTab, stripOrder])

  // --- The read model (PR3b) -----------------------------------------------
  // The tabs drawn now, partitioned (lib/feedOrder.ts): the phone's tab
  // and, on the desktop, every grid column (the phone layout stays
  // mounted there, hidden, so its tab is drawn too). They are drawn
  // under a search too, so searching doesn't start a tab over. Saved has
  // no read model: it lists every save, with no marker. A tab not drawn
  // starts over the next time it is (a tab switch resets both tabs).
  // Nothing is kept until the preferences have answered and the first
  // refresh is done, so the first snapshot sees the stored marks.
  // The source filter's one column is another tab for each filter, and
  // again once that filter's own fetch has landed (until then it shows
  // the unfiltered stories of those feeds): its order names both, so
  // either starts it over. (PR8b removes the filter.)
  // Wider tabs (PR4d, D5). A tab at 7 days or All fetches its own rows
  // while it is on screen (the phone's tab; on the desktop every column,
  // one at a time): when it comes on screen or widens, and after every
  // poll. The rows (``extraByTab``) stay across tab switches. A tab not
  // on screen counts its badge from the 2-day data.
  const widerTabs = useMemo(() => {
    const shown = isDesktop ? keyColumns : phoneCol ? [phoneCol] : []
    return shown.filter((c) => c.key !== SAVED_KEY && isWider(c.window)).map((c) => ({ key: c.key, name: c.name, window: c.window }))
  }, [isDesktop, keyColumns, phoneCol])
  const widerScope = widerTabs.map((t) => `${t.key}|${t.window}`).join(',')
  const widerTabsRef = useRef(widerTabs)
  widerTabsRef.current = widerTabs
  // Per tab: the latest request (an older answer is dropped), and when
  // it came on screen at its window (``since``): its first answer
  // starts the tab's snapshot over, so the older rows go in at their
  // place and what was read goes under the marker, unless the list was
  // touched meanwhile (scrolled, or a story opened), when the rows are
  // sorted in instead (``inOrder``). Later answers are always sorted in.
  const widerSeqRef = useRef(new Map<ColKey, number>())
  const widerSinceRef = useRef(new Map<string, { at: number; answered: boolean }>())
  // Per tab and window (``key|window``): whether a wider fetch for it has
  // answered, or only failed so far. Until one answers, the tab's rows
  // for that window aren't known, so an empty tab claims nothing
  // ("Nothing in 7 days") and offers nothing (``windowAnswered``). Kept
  // across tab switches with ``extraByTab``'s rows; a failure after an
  // answer leaves it answered (the rows held are that answer's).
  const [widerStatus, setWiderStatus] = useState<ReadonlyMap<string, 'answered' | 'failed'>>(() => new Map())
  const fetchWider = useCallback(
    async (tab: { key: ColKey; name: string; window: Window }) => {
      const h = windowHours(tab.window)
      const seq = (widerSeqRef.current.get(tab.key) ?? 0) + 1
      widerSeqRef.current.set(tab.key, seq)
      const scope = `${tab.key}|${tab.window}`
      try {
        const rows =
          tab.key === FORYOU_KEY
            ? await api.forYou({ limit: WIDER_FORYOU_LIMIT, maxAgeHours: h })
            : tab.key === FILTER_KEY
              ? await api.entries({ source: Array.from(activeSources), limit: WIDER_LIMIT, maxAgeHours: h, perSourceCap: PER_SOURCE_CAP, activeOnly: true })
              : await api.entries({ category: tab.name, limit: WIDER_LIMIT, maxAgeHours: h, perSourceCap: PER_SOURCE_CAP, activeOnly: true })
        if (widerSeqRef.current.get(tab.key) !== seq) return
        const since = widerSinceRef.current.get(scope)
        const had = extraRef.current.get(tab.key) ?? NO_ENTRIES
        const changed = had.length !== rows.length || had.some((e, i) => e.id !== rows[i].id)
        if (changed) setExtraByTab((prev) => new Map(prev).set(tab.key, rows))
        setWiderStatus((prev) => (prev.get(scope) === 'answered' ? prev : new Map(prev).set(scope, 'answered')))
        if (since && !since.answered) {
          since.answered = true
          const scroller = isDesktop ? scrollersRef.current.desktop : scrollersRef.current.phone
          const touched = lastStoryOpenAt() >= since.at || (scroller?.scrollTop ?? 0) > 0
          if (changed && !touched) resetSnapshot(tab.key)
        }
      } catch (err) {
        // The tab keeps what it had (the 2-day data at least); the
        // banner says why, until a poll that works clears it.
        if (widerSeqRef.current.get(tab.key) === seq) {
          setError(errorMessage(err))
          setWiderStatus((prev) => (prev.has(scope) ? prev : new Map(prev).set(scope, 'failed')))
        }
      }
    },
    [activeSources, isDesktop, resetSnapshot],
  )
  useEffect(() => {
    const tabs = widerTabsRef.current
    const scopes = new Set(tabs.map((t) => `${t.key}|${t.window}`))
    for (const scope of widerSinceRef.current.keys()) if (!scopes.has(scope)) widerSinceRef.current.delete(scope)
    for (const scope of scopes) if (!widerSinceRef.current.has(scope)) widerSinceRef.current.set(scope, { at: Date.now(), answered: false })
    if (!tabs.length || !firstRefreshDone) return
    let stop = false
    void (async () => {
      // One at a time (the desktop can have several).
      for (const tab of tabs) {
        if (stop) return
        await fetchWider(tab)
      }
    })()
    return () => {
      stop = true
    }
  }, [widerScope, refreshTick, firstRefreshDone, fetchWider])
  // Each wider tab's stories as drawn: its own rows first (the server's
  // order for For You's Best; a story the 2-day load also holds is drawn
  // from that copy, the latest), then the 2-day stories they don't hold
  // (the newest arrivals), in the tab's order; hidden stories out.
  // ``ids`` are the rows only the fetch brought, placed in order by the
  // snapshot (``inOrder``) rather than appended like arrivals.
  const widened = useMemo(() => {
    const now = Date.now()
    const out = new Map<ColKey, { entries: Entry[]; ids: Set<number> }>()
    for (const c of columns) {
      if (c.key === SAVED_KEY || !isWider(c.window)) continue
      const extra = extraByTab.get(c.key)
      if (!extra?.length) continue
      const have = new Map(c.entries.map((e) => [e.id, e]))
      const theirs = new Set<number>()
      const ids = new Set<number>()
      const rows: Entry[] = []
      for (const e of extra) {
        if (theirs.has(e.id) || hiddenSet.has(e.id) || !inWindow(e.fetched_at, c.window, now)) continue
        theirs.add(e.id)
        const held = have.get(e.id)
        if (!held) ids.add(e.id)
        rows.push(held ?? e)
      }
      if (!ids.size) continue
      out.set(c.key, { entries: sortEntries([...rows, ...c.entries.filter((e) => !theirs.has(e.id))], c.sort, c.key), ids })
    }
    return out
  }, [columns, extraByTab, hiddenSet])
  const widenedIds = useMemo(() => {
    const out = new Set<number>()
    for (const w of widened.values()) for (const id of w.ids) out.add(id)
    return out
  }, [widened])
  // Whether a tab's stories at its window are known: 1 and 2 days come
  // with every refresh; 7 days and All once their own fetch has answered
  // (``widerStatus``). Column's empty state claims "Nothing in 7 days"
  // only then.
  const windowAnsweredFor = (c: { key: ColKey; window: Window }) => !isWider(c.window) || widerStatus.get(`${c.key}|${c.window}`) === 'answered'
  // A wider tab draws the rows it fetched too (``widened``).
  const asFeedTab = (c: TabColumn & { order: string }): FeedTab<Entry> => {
    const entries = widened.get(c.key)?.entries ?? c.entries
    return c.key === FILTER_KEY ? { key: c.key, entries, order: `${c.order}|${activeScope}|${tabsLoadedFor ?? ''}` } : { key: c.key, entries, order: c.order }
  }
  const drawnTabs: FeedTab<Entry>[] = []
  if (phoneCol && phoneCol.key !== SAVED_KEY) drawnTabs.push(asFeedTab(phoneCol))
  if (isDesktop) for (const c of keyColumns) if (c.key !== SAVED_KEY) drawnTabs.push(asFeedTab(c))
  // Out of a snapshot at once, cached copies too: a hidden story, and
  // one from a feed that was deleted. (PR5b adds muted feeds here.)
  const isRemoved = (e: Entry) => hiddenSet.has(e.id) || (sourcesLoaded && !sourcesById.has(e.source_id))
  // Seen on scroll's collector (PR3c; the rest of it is below): the
  // stories seen on scroll for SEEN_BATCH_MS, then one ``addIds`` per
  // ``seen:<category>`` key (``addSeenMarks``), so one render per batch,
  // not one per card. Opening a story doesn't wait for it
  // (``openEntry``). A snapshot started meanwhile places what it holds
  // as seen (``isPending``), whatever started it: a tab switch, a
  // Refresh, a new Sort or age window, the phone turned past 768px (the
  // grid's columns start theirs). Placed as unseen, such a story would
  // stay above the marker once its mark landed: ids keep their group.
  const addSeenMarksRef = useRef<(stories: readonly Entry[]) => unknown>(() => undefined)
  const [seenBatch] = useState(() =>
    createSeenBatch<Entry>({
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (timer) => window.clearTimeout(timer as number),
      drain: (stories) => addSeenMarksRef.current(stories),
      key: (e) => e.id,
    }),
  )
  const isPending = (e: Entry) => seenBatch.has(e.id)
  // A wider tab's own rows go in at their place in the tab's order, not
  // at the end like an arrival (``inOrder``).
  const inOrder = (e: Entry) => widenedIds.has(e.id)
  const parts = feed.partition(drawnTabs, { isSeen, isRemoved, isPending, inOrder, ready: !prefs.loading && firstRefreshDone })
  // One count per tab (D4): the partition's for a tab drawn now (held
  // arrivals and stories a poll dropped included), otherwise the tab's
  // stories not seen yet. It is the badge, the marker, the md+ "N new"
  // and C8's. Saved and Brief have none.
  const countFor = (col: { key: ColKey; entries: Entry[] }) =>
    col.key === SAVED_KEY ? 0 : (parts.get(col.key)?.unseenCount ?? countUnseen(col.entries, isSeen))
  // C8: under a caught-up tab's marker, the next tab with stories not
  // seen yet, walking the strip (``columns`` is ``orderColumns().shown``,
  // so a hidden tab is never named). One object per tab while its value
  // holds, so Column's memo can compare it by reference.
  const tabCounts: TabCount[] = columns
    .filter((c) => c.key !== SAVED_KEY)
    .map((c) => ({ key: c.key, label: c.label, count: countFor(c) }))
  const nextTabsRef = useRef(new Map<string, TabCount>())
  const nextTabFor = (key: ColKey): TabCount | null => {
    const found = findNextTab(tabCounts, key)
    if (!found) return null
    const kept = nextTabsRef.current.get(key)
    if (kept && kept.key === found.key && kept.label === found.label && kept.count === found.count) return kept
    nextTabsRef.current.set(key, found)
    return found
  }
  // A tab's ``sections`` for Column: its partition's, or for Saved every
  // save above no marker. The same object while nothing changed (Column's
  // memo compares it by reference).
  const savedSectionsRef = useRef(NO_SECTIONS)
  const sectionsFor = (col: { key: ColKey; entries: Entry[] }) => {
    if (col.key !== SAVED_KEY) return parts.get(col.key)?.sections ?? NO_SECTIONS
    if (savedSectionsRef.current.unseen !== col.entries) savedSectionsRef.current = { unseen: col.entries, earlier: NO_ENTRIES }
    return savedSectionsRef.current
  }
  // Every story App fetched: a key's oldest marks outside these go first
  // when it reaches its cap (``pruneIds``), so pruning never brings back
  // a story still on screen as unseen: the tabs, For You, the saves, the
  // Saved-by-id cache and every wider tab's rows (``keepIdsFor``).
  const extraRows = useMemo(() => [...extraByTab.values()].flat(), [extraByTab])
  const keepIds = useMemo(
    () => keepIdsFor({ entries, forYou, starred: starredEntries, savedById, extra: extraRows }),
    [entries, forYou, starredEntries, savedById, extraRows],
  )
  const readRefs = useRef({ categoriesBySourceId, keepIds })
  readRefs.current = { categoriesBySourceId, keepIds }
  // The one way seen marks are written: each story under its category's
  // ``seen:`` key, one ``addIds`` per key, synchronously (so a mark made
  // as the page hides is in the queue its keepalive flush sends). Returns
  // the ids each key gained, for an Undo. (The seen-on-scroll collector,
  // below, drains into this too.)
  const addSeenMarks = useCallback(
    (stories: readonly Entry[]) => {
      const { categoriesBySourceId: cats, keepIds: keep } = readRefs.current
      const had = latestPrefs().seenEntries
      const added = new Map<`seen:${string}`, number[]>()
      for (const e of stories) {
        const category = cats.get(e.source_id) ?? 'other'
        if ((had[category] ?? []).includes(e.id)) continue
        const key = seenKey(category)
        const ids = added.get(key) ?? []
        if (!ids.includes(e.id)) ids.push(e.id)
        added.set(key, ids)
      }
      for (const [key, ids] of added) addIds(key, ids, keep)
      return added
    },
    [addIds, latestPrefs],
  )
  // Opening a story uses it up (D3), from a tab, the desktop For You row
  // or search, whichever way out (Card's ``onActivate``). It also stamps
  // the open for C1's guard and PR1r's (``noteStoryOpened``). Stable:
  // Column's and Card's memos keep old callbacks.
  const openEntry = useCallback(
    (entry: Entry) => {
      noteStoryOpened()
      // An open calls off C1's second reset (the story stays in place).
      disarmReturnReset()
      addSeenMarks([entry])
      // The ranking's ``view``, recorded the moment the story is used up
      // (D3), as a scroll past records it (``markSeen``); the Card has
      // already sent its ``click``. One per story per session whichever
      // comes first (``recordBatched`` dedups), so the signal doesn't
      // hang on the order of opening and scrolling past. This is where
      // the desktop's views come from.
      recordBatched({ entry_id: entry.id, type: 'view' })
    },
    [addSeenMarks, disarmReturnReset],
  )

  // Edit tabs: the order and the Show switches (``lib/categories``).
  // Each change is one PUT of the whole ``category_layout``, built from
  // ``latestPrefs()`` so two taps in one tick both count. A move stores
  // the full order (from then on a new category goes at the end); a
  // Show switch changes only ``hidden``.
  const [tabsEditorOpen, setTabsEditorOpen] = useState(false)
  // Where focus goes back to when the editor closes: the Edit pill, or
  // the Menu's ≡ button when it was opened from the Menu.
  const tabsEditorReturn = useRef<'pill' | 'menu'>('pill')
  const editPillRef = useRef<HTMLButtonElement | null>(null)
  const openTabsEditor = useCallback((from: 'pill' | 'menu') => {
    tabsEditorReturn.current = from
    setTabsEditorOpen(true)
  }, [])
  const closeTabsEditor = useCallback(() => {
    setTabsEditorOpen(false)
    requestAnimationFrame(() => {
      const back =
        tabsEditorReturn.current === 'pill' && editPillRef.current?.getClientRects().length
          ? editPillRef.current
          : document.querySelector<HTMLElement>('button[aria-label="open menu"]')
      back?.focus()
    })
  }, [])

  // --- Seen on scroll (PR3c) -------------------------------------------------
  // On the phone, a story read on screen (half of it, for SEEN_DWELL_MS)
  // and then scrolled off the top is used up, as is one still on screen
  // once the end of the tab has been (lib/seen.ts has the rules). The
  // phone tab's list is the root (null on the desktop, where only opening
  // uses a story up: C2). Saved, Brief and search are open only.
  //
  // The dwell pauses while one of App's sheets covers the list. App's own
  // state, never ``[aria-modal]``: the closed Drawer is always mounted
  // with it. The card's ⋯ sheet (ActionSheet.tsx, PR5a) reports its
  // state through ``onOpenChange``. (PR8a adds the TabSheet here.)
  const [actionSheetOpen, setActionSheetOpen] = useState(false)
  const modalOpen = drawerOpen || settingsOpen || shortcutsOpen || tabsEditorOpen || actionSheetOpen
  // Read when a card is seen: the phone tab, whose partition holds every
  // story it draws (one a poll dropped too), and what is seen by then.
  const seenReadRef = useRef({ isSeen, phoneKey: phoneCol?.key })
  seenReadRef.current = { isSeen, phoneKey: phoneCol?.key }
  // The collector (``seenBatch``, declared before the partition) drains
  // into the latest ``addSeenMarks``.
  addSeenMarksRef.current = addSeenMarks
  // Cards seen on scroll. One already seen is left alone: an opened one
  // recorded its ``view`` when it was opened (``openEntry``), one marked
  // on another device recorded it there, and Mark all isn't a read. For
  // the rest the ranking's ``view`` is recorded now, the moment the story
  // is used up (D3), not when its card is drawn. A story used up here
  // calls off C1's second reset, as an open does: the end rule uses the
  // last cards up with no touch, scroll or key, and a return's slow
  // refresh landing after that would move them under a folded Earlier.
  const markSeen = useCallback(
    (ids: number[]) => {
      const { isSeen: seen, phoneKey } = seenReadRef.current
      const cache = phoneKey ? latestPart(phoneKey)?.snapshot.cache : undefined
      if (!cache) return
      const stories: Entry[] = []
      for (const id of ids) {
        const e = cache.get(id)
        if (!e || seen(e)) continue
        recordBatched({ entry_id: e.id, type: 'view' })
        stories.push(e)
      }
      if (!stories.length) return
      disarmReturnReset()
      seenBatch.add(stories)
    },
    [latestPart, seenBatch, disarmReturnReset],
  )
  const seenOnScroll = useSeenObserver(isDesktop ? null : phoneScroller, { modalOpen, onSeen: markSeen })
  // The page hiding (or the reload onto a new build): what the observer
  // still holds and what the collector has go into the queue first, so a
  // card that left the top just before is in the keepalive write.
  useEffect(
    () =>
      registerBeforeFlush(() => {
        seenOnScroll.takeRecords()
        seenBatch.flush()
      }),
    [registerBeforeFlush, seenOnScroll, seenBatch],
  )
  const hiddenTabs = useMemo(() => new Set(tabOrder.hidden.map((t) => t.key)), [tabOrder])
  const moveTab = useCallback(
    (key: string, to: Move) => {
      const layout = latestPrefs().categoryLayout
      const present = new Set<string>(availableTabs.map((t) => t.key))
      const order = fullOrder(availableTabs, layout)
      const next = moveKey(order, key, to, (k) => present.has(k))
      if (next.every((k, i) => k === order[i])) return
      setCategoryLayout({ v: 1, order: next, hidden: layout?.hidden ?? [] })
    },
    [availableTabs, latestPrefs, setCategoryLayout],
  )
  const showTab = useCallback(
    (key: string, show: boolean) => {
      const layout = latestPrefs().categoryLayout
      const hidden = (layout?.hidden ?? []).filter((k) => k !== key)
      if (!show) hidden.push(key)
      setCategoryLayout({ v: 1, order: layout?.order ?? [], hidden })
    },
    [latestPrefs, setCategoryLayout],
  )
  // Every feed is a built-in: an empty Saved or For You offers "Add
  // your first feed" (Settings → Feeds). Only once a refresh has brought
  // the feed list back: before that, or after a failed first load,
  // ``sources`` is still empty and says nothing about the owner's feeds.
  const noOwnFeeds = sourcesLoaded && !sources.some((s) => !isBuiltIn(s))
  const openFeeds = useCallback(() => openSettings('feeds'), [openSettings])

  // Note: readEntries, lastViewed, columnPrefs, columnSections,
  // hiddenEntries, starredEntries, filterPresets, and
  // historyGroupBy used to be persisted to localStorage via
  // useEffect blocks here. They are now server-backed
  // (``lib/preferences.tsx``); the provider debounces the PUTs
  // and the in-memory state lives in the context. No
  // per-key useEffect needed.

  // Cancel any in-flight brief-generation loop on unmount. Without
  // this, navigating away mid-generation leaves the polling loop
  // alive in the background; its eventual setState calls would
  // hit an unmounted component (React tolerates but warns) and
  // its ``setGeneratingBrief(false)`` in the finally branch
  // would race a future mount of App.
  useEffect(() => {
    return () => {
      generationAbortRef.current?.abort()
      generationAbortRef.current = null
    }
  }, [])

  // Debounced search. 300ms — standard "feels live but doesn't fire
  // on every keystroke". The mirror into ``searchQuery`` happens
  // inside the timeout so the actual fetch effect only re-runs on
  // the settled value.
  useEffect(() => {
    const trimmed = searchInput.trim()
    if (!trimmed) {
      setSearchQuery('')
      setSearchResults([])
      setSearching(false)
      setSearchError(null)
      return
    }
    setSearching(true)
    setSearchError(null)
    const id = window.setTimeout(() => {
      setSearchQuery(trimmed)
    }, 300)
    return () => window.clearTimeout(id)
  }, [searchInput])

  useEffect(() => {
    if (!searchQuery) return
    let cancelled = false
    api
      .entries({ q: searchQuery, limit: 50 })
      .then((rows) => {
        if (cancelled) return
        setSearchResults(rows)
        setSearchError(null)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setSearchResults([])
        // An error, not an empty list, so the user can tell "no
        // matches" (results=[], error=null) from "the request failed"
        // (results=[], error="…"). Plain words, not the raw text.
        setSearchError(errorMessage(err))
      })
      .finally(() => {
        if (!cancelled) setSearching(false)
      })
    return () => {
      cancelled = true
    }
  }, [searchQuery])

  // Mark all as read's Undo: remove exactly the ids it added, per
  // ``seen:<category>`` key (a change, not a new list: a mark made
  // meanwhile, here or on another device, stays), then start the tab
  // over, so the stories are back above the marker.
  const unmarkColumnRead = useCallback(
    (key: ColKey, added: ReadonlyMap<`seen:${string}`, number[]>) => {
      for (const [k, ids] of added) removeIds(k, ids)
      resetSnapshot(key)
    },
    [removeIds, resetSnapshot],
  )

  // A hide keeps each story and its places in ``entries`` and ``forYou``
  // for its Undo (see ``hiddenStoriesRef``): its neighbours and index
  // (``placeOf``), taken with the stories already kept back in their
  // places, so one hidden earlier that a poll has dropped still counts as
  // a neighbour and the two can come back in either order. A story only
  // Saved holds needs nothing kept: the by-id cache still has it. The
  // latest ``KEPT_HIDDEN`` are kept, and never fewer than this hide's
  // own, so a bulk hide ("Hide all downvoted") keeps all of its stories
  // and pushes out none of the ``KEPT_HIDDEN`` before it. Called before
  // the hide's ``addIds``, so ``latestPrefs`` holds the earlier hides.
  const keepHiddenStories = useCallback((ids: readonly number[]) => {
    const { entries: es, forYou: fy } = storiesRef.current
    const hiddenBefore = new Set(latestPrefs().hiddenEntries)
    const isHidden = (id: number) => hiddenBefore.has(id)
    const kept = hiddenStoriesRef.current
    for (const id of ids) {
      kept.delete(id)
      restoredRef.current.delete(id)
    }
    const withKept = (list: Entry[], side: KeptSide) => putBack(list, putBackItems(kept.values(), side), keptPlaces(kept, side))
    const allEntries = withKept(es, 'inEntries')
    const allForYou = withKept(fy, 'inForYou')
    const entriesAt = new Map(allEntries.map((e, i) => [e.id, i]))
    const forYouAt = new Map(allForYou.map((e, i) => [e.id, i]))
    const copies = new Map([...fy, ...es].map((e) => [e.id, e]))
    let added = 0
    for (const id of ids) {
      const entry = copies.get(id)
      if (!entry) continue
      const ei = entriesAt.get(id)
      const fi = forYouAt.get(id)
      kept.set(id, {
        entry,
        inEntries: ei === undefined ? null : placeOf(allEntries, ei, isHidden),
        inForYou: fi === undefined ? null : placeOf(allForYou, fi, isHidden),
      })
      added++
    }
    for (const id of kept.keys()) {
      if (kept.size <= KEPT_HIDDEN + added) break
      kept.delete(id)
    }
  }, [latestPrefs])
  // An unhide puts the kept stories back in their places, unless a poll
  // already brought them back (``putBack``). A story whose feed is gone
  // since the hide (deleted here or on another device) stays out: it
  // would fall into an "Other" tab. While For You's last request failed,
  // For You is [] until it answers: the story isn't put in there alone,
  // but laid over that answer (``restoredRef``).
  const putBackHiddenStories = useCallback((ids: readonly number[]) => {
    const back: KeptStory[] = []
    const at = Date.now()
    const feedIds = new Set(sourcesRef.current.map((s) => s.id))
    for (const id of ids) {
      const kept = hiddenStoriesRef.current.get(id)
      if (!kept) continue
      hiddenStoriesRef.current.delete(id)
      if (!feedIds.has(kept.entry.source_id)) continue
      restoredRef.current.set(id, { ...kept, at })
      back.push(kept)
    }
    if (back.length === 0) return
    const kept = hiddenStoriesRef.current
    const inEntries = putBackItems(back, 'inEntries')
    const entriesKept = keptPlaces(kept, 'inEntries')
    if (inEntries.length) setEntries((prev) => putBack(prev, inEntries, entriesKept))
    const inForYou = putBackItems(back, 'inForYou')
    const forYouKept = keptPlaces(kept, 'inForYou')
    if (inForYou.length && forYouOkRef.current) setForYou((prev) => putBack(prev, inForYou, forYouKept))
  }, [])

  // Reverse of ``hideEntries``: remove the ids from ``hiddenEntries`` so
  // the stories surface in the dashboard again, in their places (a tab's
  // snapshot kept its place; ``putBackHiddenStories`` the stories
  // themselves, which a poll may have dropped meanwhile). Used by every
  // unhide: the Undo of a card's hide (``hideStory``) and of "Hide all
  // downvoted", and Settings' Hidden list (Restore and Restore all).
  const restoreHiddenEntries = useCallback((ids: readonly number[]) => {
    removeIds(PREFERENCE_KEYS.hiddenEntries, [...ids])
    putBackHiddenStories(ids)
  }, [removeIds, putBackHiddenStories])
  const restoreHiddenEntry = useCallback(
    (entryId: number) => restoreHiddenEntries([entryId]),
    [restoreHiddenEntries],
  )

  // Hide entries from every column + the For You row. The
  // entries stay in the DB; they just get filtered out of the
  // dashboard until the user restores them (Settings → Hidden).
  const hideEntries = useCallback((ids: readonly number[]) => {
    keepHiddenStories(ids)
    addIds(PREFERENCE_KEYS.hiddenEntries, [...ids])
  }, [addIds, keepHiddenStories])
  const hideEntry = useCallback((entryId: number) => hideEntries([entryId]), [hideEntries])

  // Hide a story (PR5a, D2): the card's ⋯ → Hide, a swipe left, and
  // the ``h`` key. The story leaves its tab at once (a hide doesn't
  // mark it seen, PR3b) and the toast's Undo puts it back in its
  // place. The ranking's ``never`` is held on the device until the
  // Undo window closes (D25): the toast expiring releases it, Undo
  // cancels it, and the page hiding sends whatever is still held.
  // (PR5b merges quick hides into one toast, "2 hidden · Undo".)
  //
  // A story already hidden is left alone: a second hide would take a
  // second hold whose toast replaces the first (sending its ``never``
  // at once, with no Undo left to take it back) and add the id again.
  // The keyboard selection goes with the card: the shortcuts never act
  // on a card that isn't drawn, so ``h``, ``b``, ``a`` and ``z`` after a
  // hide do nothing until another card is selected (Undo doesn't select
  // it again).
  //
  // Slice 21: a ``useCallback`` over stable callbacks, so Column's and
  // Card's memos can keep it.
  const hideStory = useCallback(
    (entryId: number) => {
      if (latestPrefs().hiddenEntries.includes(entryId)) return
      hideEntry(entryId)
      dropSelection(entryId)
      const hold = holdInteraction({ entry_id: entryId, type: 'never' })
      toast('Hidden', {
        kind: 'info',
        action: {
          label: 'Undo',
          onClick: () => {
            hold.cancel()
            restoreHiddenEntry(entryId)
          },
        },
        onExpire: hold.release,
      })
    },
    [latestPrefs, hideEntry, dropSelection, restoreHiddenEntry],
  )

  // Star / unstar an entry. Stars are a long-term save-for-later
  // set: starred items surface in a dedicated "Saved" column at
  // the top of the dashboard, in the For You row, and in the
  // Settings/Starred tab. Records a ``bookmark`` engagement event
  // so the ranker can use stars as a positive signal (the
  // opposite of ``hide``'s ``never``).
  //
  // ``setStarred(id, on)`` saves or unsaves; an Undo calls it with
  // what the story was before, so it puts back exactly that whatever
  // happened since (a second toggle would flip it again if the
  // story changed meanwhile, here or on another device).
  const setStarred = useCallback((entryId: number, on: boolean) => {
    if (on) addIds(PREFERENCE_KEYS.starredEntries, [entryId])
    else removeIds(PREFERENCE_KEYS.starredEntries, [entryId])
  }, [addIds, removeIds])
  // Returns whether the story was saved before, for the toast and its
  // Undo (from ``latestPrefs()``, so it matches what the toggle did).
  const toggleStarEntry = useCallback((entryId: number): boolean => {
    const wasStarred = latestPrefs().starredEntries.includes(entryId)
    setStarred(entryId, !wasStarred)
    return wasStarred
  }, [latestPrefs, setStarred])
  // The card's bookmark and the ``b`` key (PR5a, D2): saving is silent
  // (the bookmark fills); an unsave shows "Removed from Saved · Undo",
  // whose Undo saves it again (an add, not a toggle, so it puts back
  // exactly that). In Saved the card leaves the list, so without this
  // Undo an accidental unsave could not be undone.
  const starStory = useCallback(
    (entryId: number) => {
      const wasStarred = toggleStarEntry(entryId)
      if (!wasStarred) return
      toast('Removed from Saved', {
        kind: 'info',
        action: { label: 'Undo', onClick: () => setStarred(entryId, true) },
      })
    },
    [toggleStarEntry, setStarred],
  )

  // Apply a saved preset: replace the active sources filter. The
  // per-column prefs a preset captured are no longer applied: they
  // would overwrite the tabs' Newest | Best (and, later, their time
  // window) every time a chip is tapped. ``savePreset`` and
  // ``updatePreset`` still capture them, so the stored presets keep
  // their shape for an older build, until custom tabs replace presets.
  const applyPreset = useCallback((preset: FilterPreset) => {
    setActiveSources(new Set(preset.activeSources))
  }, [])

  // Save the current view as a preset. Returns the new preset
  // id so the caller can show a "Saved" toast.
  const savePreset = useCallback(
    (name: string): string => {
      const id = `p-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
      const preset: FilterPreset = {
        id,
        name: name.trim() || 'Untitled preset',
        activeSources: Array.from(activeSources),
        columnPrefs,
      }
      setFilterPresets(
        [...filterPresetsRef.current, preset].slice(-MAX_PRESETS),
      )
      return id
    },
    [activeSources, columnPrefs, setFilterPresets],
  )

  // Delete a preset by id. The chip strip handles its own
  // options-menu -> confirm flow.
  const deletePreset = useCallback((id: string) => {
    setFilterPresets(
      filterPresetsRef.current.filter((p) => p.id !== id),
    )
  }, [setFilterPresets])

  // Rename a preset in place. Only the label changes -- the
  // captured filter + column prefs are untouched. The chip's
  // "Rename" action collects the new name via a prompt and calls
  // this with the already-trimmed, already-changed value.
  const renamePreset = useCallback(
    (id: string, name: string) => {
      setFilterPresets(
        filterPresetsRef.current.map((p) => (p.id === id ? { ...p, name } : p)),
      )
    },
    [setFilterPresets],
  )

  // Overwrite a preset's captured filter + column prefs with
  // whatever the dashboard is showing right now, keeping its id
  // and name. Without this the only way to adjust a saved view
  // was delete-and-recreate, which loses the id (and any other
  // metadata a future feature might hang off it).
  const updatePreset = useCallback(
    (id: string) => {
      setFilterPresets(
        filterPresetsRef.current.map((p) =>
          p.id === id
            ? { ...p, activeSources: Array.from(activeSources), columnPrefs }
            : p,
        ),
      )
    },
    [activeSources, columnPrefs, setFilterPresets],
  )

  // Toggle the inline-summary panel for an entry. Independent of
  // seen — expanding a card doesn't mark it seen, and marking a
  // card seen doesn't collapse its summary. ``useCallback`` so the
  // keyboard-shortcut effect can list it as a stable dependency
  // (and so child re-renders are minimized when nothing changes).
  const toggleSummary = useCallback((entryId: number) => {
    setExpandedSummaries((prev) => {
      const next = new Set(prev)
      if (next.has(entryId)) next.delete(entryId)
      else next.add(entryId)
      return next
    })
  }, [])

  // Keyboard navigation. Skips when an input/textarea/select has
  // focus so the LLM picker's free-text fields stay typeable. ``/``
  // focuses search; Esc clears it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const ae = document.activeElement
      if (
        ae instanceof HTMLInputElement ||
        ae instanceof HTMLTextAreaElement ||
        ae instanceof HTMLSelectElement ||
        (ae && (ae as HTMLElement).isContentEditable)
      ) {
        if (ae instanceof HTMLInputElement && ae.id === 'app-search' && e.key === 'Escape') {
          ae.blur()
          setSearchInput('')
          setSearchOpen(false)
        }
        return
      }

      // A sheet is up (the Menu, Settings, the shortcuts, Edit tabs, a
      // card's ⋯): no shortcut acts on the page behind it. Each sheet
      // takes its own Esc in the capture phase.
      if (modalOpen) return

      if (e.key === '/' && columns.length > 0) {
        e.preventDefault()
        // Search is collapsed to an icon until opened; open it, then
        // focus the field once it has mounted.
        setSearchOpen(true)
        requestAnimationFrame(() => document.getElementById('app-search')?.focus())
        return
      }

      if (e.key === ',' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault()
        openSettings('feeds')
        return
      }

      if (e.key === '?' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // ``?`` is Shift+/ on US layouts — key value is the literal
        // ``?`` character regardless. Guard modifiers so it doesn't
        // fire under Cmd+/ or Ctrl+/ which the OS / browser may
        // already bind.
        e.preventDefault()
        setShortcutsOpen(true)
        return
      }

      if (e.key === 'Escape' && searchQuery) {
        setSearchInput('')
        return
      }

      if (keyColumns.length === 0) return

      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setSelectedColumnKey(keyColumns[Math.max(0, selectedColumnIndex - 1)].key)
        setSelectedCardId(null)
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setSelectedColumnKey(keyColumns[Math.min(keyColumns.length - 1, selectedColumnIndex + 1)].key)
        setSelectedCardId(null)
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const col = selectedKeyColumn
        if (!col) return
        // The column as it is drawn: its unseen stories, then Earlier
        // (which the Column opens when the selection reaches it).
        const part = latestPart(col.key)
        const list = part ? [...part.unseen, ...part.earlier] : col.entries
        if (list.length === 0) return
        setSelectedColumnKey(col.key)
        const idx = list.findIndex((e2) => e2.id === selectedCardId)
        const next =
          e.key === 'ArrowDown'
            ? Math.min(list.length - 1, idx + 1)
            : Math.max(0, idx - 1)
        setSelectedCardId(list[next]?.id ?? null)
      } else if (e.key === 'Enter' && selectedCardId != null) {
        e.preventDefault()
        const card = cardRefs.current.get(selectedCardId)
        const link = card?.querySelector('a')
        link?.click()
      } else if (e.key === 's' && selectedCardId != null) {
        // Toggle inline summary on the selected card. Mirrors the
        // chevron button. Modifier guards so Cmd+S (Save Page As)
        // still works when the dashboard has focus.
        if (e.metaKey || e.ctrlKey || e.altKey) return
        e.preventDefault()
        toggleSummary(selectedCardId)
      } else if (e.key === 'b' && selectedCardId != null) {
        // Save or unsave the selected card. ``b`` for bookmark is
        // the standard iOS / Gmail affordance; doesn't conflict with
        // browser defaults (Cmd+B is bold-text in form fields but
        // does nothing on the dashboard surface). Same modifier
        // guards as ``s`` so Cmd+B still works when the dashboard
        // has focus and a form field is active. A held key fires
        // once (``repeat``): ``b``, ``h``, ``a`` and ``z`` each toggle
        // or take something away, so a repeat would undo the first.
        if (e.metaKey || e.ctrlKey || e.altKey || e.repeat) return
        e.preventDefault()
        // Mirrors the bookmark's onClick (Card.tsx) — the ranker
        // needs this event regardless of input path, or the
        // preference vector never learns from keyboard-driven saves.
        // Only a save records it: unsaving is not a save.
        if (!latestPrefs().starredEntries.includes(selectedCardId)) {
          recordImmediate({ entry_id: selectedCardId, type: 'bookmark' })
        }
        starStory(selectedCardId)
      } else if (e.key === 'h' && selectedCardId != null) {
        // Hide the selected card, with Undo. ``h`` for hide is the
        // natural mirror of ``b`` (bookmark). It conflicts with
        // browser-level shortcuts less often than ``e`` (which is
        // "find previous" in some browsers). Same modifier guards as
        // the other single-key shortcuts so Cmd+H (Hide App on
        // macOS) still works when the dashboard has focus.
        if (e.metaKey || e.ctrlKey || e.altKey || e.repeat) return
        e.preventDefault()
        hideStory(selectedCardId)
      } else if ((e.key === 'a' || e.key === 'z') && selectedCardId != null) {
        // ▲ / ▼ on the selected card (C6), as its buttons do: the
        // same key again takes the vote back. Modifier guards so
        // Cmd+A (select all) and Cmd+Z (undo) still work.
        if (e.metaKey || e.ctrlKey || e.altKey || e.repeat) return
        e.preventDefault()
        const dir = e.key === 'a' ? 'up' : 'down'
        const prev = votedMap.get(selectedCardId) ?? null
        const next = prev === dir ? null : dir
        for (const type of voteInteractionEvents(prev, next)) {
          recordImmediate({ entry_id: selectedCardId, type })
        }
        voteEntry(selectedCardId, next)
      } else if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // Refresh the dashboard. Bare ``r``; Cmd+R / Ctrl+R
        // already triggers the browser reload so we don't fight
        // it. Same affordance as the header Refresh button: it
        // starts every tab over once the stories land.
        e.preventDefault()
        void refresh()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [
    columns,
    keyColumns,
    selectedColumnIndex,
    selectedKeyColumn,
    selectedCardId,
    searchQuery,
    latestPart,
    toggleSummary,
    refresh,
    modalOpen,
    hideStory,
    starStory,
    latestPrefs,
    votedMap,
    voteEntry,
    openSettings,
  ])

  // Scroll the keyboard-selected card into view.
  useEffect(() => {
    if (selectedCardId == null) return
    const el = cardRefs.current.get(selectedCardId)
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [selectedCardId])

  // Mark all as read (D4): the ⋯'s first row, and the end row's "N not
  // seen · Mark as read" (D24). Every story of the tab not seen yet (held
  // arrivals and stories a poll dropped included) is marked under its
  // ``seen:<category>`` key, and the tab starts over: they go under
  // "Earlier". Undo removes exactly the marks this added and starts the
  // tab over again, so they are back above the marker.
  //
  // Slice 21: a ``useCallback`` whose body reads the tab's latest
  // partition (``latestPart``) and refs, never the render it was made
  // in: Column's memo keeps old callbacks.
  const markColumnRead = useCallback((key: ColKey) => {
    // Stories seen on scroll and still being collected are written first,
    // as what they are: the Undo takes back only what Mark all marked.
    seenBatch.flush()
    const part = latestPart(key)
    if (!part || part.unseenIds.length === 0) return
    const stories: Entry[] = []
    for (const id of part.unseenIds) {
      const e = part.snapshot.cache.get(id)
      if (e) stories.push(e)
    }
    const added = addSeenMarks(stories)
    resetSnapshot(key)
    // What this marked: the partition is a render old, so a story the
    // flush above just wrote is in it, and isn't counted or undone here.
    let n = 0
    for (const ids of added.values()) n += ids.length
    if (n === 0) return
    toast(`Marked ${n} as read`, {
      kind: 'info',
      action: { label: 'Undo', onClick: () => unmarkColumnRead(key, added) },
    })
  }, [seenBatch, latestPart, addSeenMarks, resetSnapshot, unmarkColumnRead])

  const toggleSource = useCallback((name: string) => {
    setActiveSources((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [setActiveSources])
  // A feed renamed in its editor: the filter chips and every saved
  // view follow the new name at once, instead of losing the feed until
  // the next poll. A no-op if the feed wasn't in either.
  const onSourceRenamed = useCallback((oldName: string, newName: string) => {
    if (oldName === newName) return
    setActiveSources((prev) => {
      if (!prev.has(oldName)) return prev
      const next = new Set(prev)
      next.delete(oldName)
      next.add(newName)
      return next
    })
    const presets = filterPresetsRef.current
    if (presets.some((p) => p.activeSources.includes(oldName))) {
      setFilterPresets(
        presets.map((p) =>
          p.activeSources.includes(oldName)
            ? { ...p, activeSources: p.activeSources.map((n) => (n === oldName ? newName : n)) }
            : p,
        ),
      )
    }
  }, [setActiveSources, setFilterPresets])

  // --- Feed editor (Settings › Feeds) ---------------------------------
  // A row the server returned for a save. The PATCH response carries
  // no vote score, so the shown one is kept. A category change can't
  // move the phone: it follows its tab by key (``resolveTab``).
  const applyServerSource = useCallback((row: Source) => {
    sourceChangesRef.current.written.set(row.id, Date.now())
    setSources((prev) => prev.map((s) => (s.id === row.id ? { ...row, net_vote_score: s.net_vote_score } : s)))
  }, [])

  // A feed added in Settings shows at once, before the next poll.
  const insertSource = useCallback((row: Source) => {
    sourceChangesRef.current.inserted.set(row.id, Date.now())
    setSources((prev) =>
      prev.some((s) => s.id === row.id)
        ? prev.map((s) => (s.id === row.id ? row : s))
        : insertSorted(prev, row),
    )
  }, [])

  // A feed deleted (here, or found deleted by a save): its row, its
  // stories (or they'd fall into an "Other" column) and its name in the
  // filter and saved views all go.
  const removeSource = useCallback((id: number) => {
    const gone = sourcesRef.current.find((s) => s.id === id)
    sourceChangesRef.current.deleted.set(id, Date.now())
    dropSource(id)
    setSources((prev) => prev.filter((s) => s.id !== id))
    setEntries((prev) => prev.filter((e) => e.source_id !== id))
    setForYou((prev) => prev.filter((e) => e.source_id !== id))
    if (gone) {
      setActiveSources((prev) => {
        if (!prev.has(gone.name)) return prev
        const next = new Set(prev)
        next.delete(gone.name)
        return next
      })
      const presets = filterPresetsRef.current
      if (presets.some((p) => p.activeSources.includes(gone.name))) {
        setFilterPresets(
          presets.map((p) =>
            p.activeSources.includes(gone.name)
              ? { ...p, activeSources: p.activeSources.filter((n) => n !== gone.name) }
              : p,
          ),
        )
      }
    }
    void refreshRef.current({ silent: true })
  }, [setActiveSources, setFilterPresets])

  // The latest handlers for the store, which is configured once.
  const storeHandlersRef = useRef({ applyServerSource, onSourceRenamed, removeSource, openSettings })
  storeHandlersRef.current = { applyServerSource, onSourceRenamed, removeSource, openSettings }

  useEffect(() => {
    // One quiet refresh 15 s after an address or headers save on an
    // active feed, so "Last problem" shows how the fetch that the save
    // started went. One timer per feed.
    const followUps = new Map<number, ReturnType<typeof setTimeout>>()
    configureSourceSaves({
      patch: async (id, body, opts) => {
        const row = await api.updateSource(id, body, opts)
        if (row.active && ('url' in body || 'custom_headers' in body)) {
          clearTimeout(followUps.get(id))
          followUps.set(
            id,
            setTimeout(() => {
              followUps.delete(id)
              void refreshRef.current({ silent: true })
            }, 15_000),
          )
        }
        return row
      },
      getSource: (id) => sourcesRef.current.find((s) => s.id === id),
      onServerRow: (row) => storeHandlersRef.current.applyServerSource(row as Source),
      onRenamed: (oldName, newName) => storeHandlersRef.current.onSourceRenamed(oldName, newName),
      onGone: (id) => storeHandlersRef.current.removeSource(id),
      onFailedAway: (id, failures) => {
        const name = sourcesRef.current.find((s) => s.id === id)?.name
        if (!name || failures.length === 0) return
        const message =
          failures.length === 1
            ? `Couldn't save the ${failures[0].label} for ${sourceLabel(name)}: ${failures[0].message}`
            : `Couldn't save ${failures.length} changes for ${sourceLabel(name)}`
        toast(message, {
          kind: 'error',
          action: { label: 'Open', onClick: () => storeHandlersRef.current.openSettings('feeds', id) },
        })
      },
      describe: (err, field, value) => ({
        message: fieldSaveMessage(err, field, value, sourceLabel),
        retryable: isRetryable(err),
      }),
      equal: (field, a, b) =>
        field === 'custom_headers'
          ? sameHeaders(a as HeaderMap | null | undefined, b as HeaderMap | null | undefined)
          : a === b || (a == null && b == null),
    })
    // Leaving the app (another app, the home screen, a closed tab)
    // sends everything queued at once with keepalive, so a save isn't
    // lost while iOS suspends the page. Always on, for saves still
    // under way after Settings closed. Settings adds committing what
    // is being typed.
    const onVisibility = () => setPageHiding(document.visibilityState === 'hidden')
    const onPageHide = () => setPageHiding(true)
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', onPageHide)
    // Back from the back-forward cache.
    window.addEventListener('pageshow', onVisibility)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', onPageHide)
      window.removeEventListener('pageshow', onVisibility)
      followUps.forEach((t) => clearTimeout(t))
    }
  }, [])

  // Tapping a card's source name opens that feed's editor. Stable, as
  // Card and Column's memos ignore callbacks.
  const onSourceTap = useCallback((id: number) => openSettings('feeds', id), [openSettings])
  const onEditSource = useCallback((id: number) => openSettings('feeds', id), [openSettings])
  // Same as ``toggleSource`` but also closes the drawer the first time
  // the user adds a source to an empty filter. The "empty → first tap"
  // transition is the confusing one — the user picked a source, the
  // filter engaged, but the panel stayed open covering the now-filtered
  // dashboard. Auto-closing lets them see the result immediately.
  // Subsequent taps toggle in place; they don't close again (avoids
  // the drawer ping-ponging shut when the user wants to pick two or
  // three sources in a row). The "sizeBefore === 0" check runs on the
  // current state, captured before ``toggleSource`` queues its update.
  const toggleSourceAndMaybeClose = useCallback((name: string) => {
    const wasEmpty = activeSources.size === 0
    toggleSource(name)
    if (wasEmpty) setDrawerOpen(false)
  }, [activeSources, toggleSource, setDrawerOpen])
  const clearSourceFilters = useCallback(() => {
    setActiveSources(new Set())
  }, [setActiveSources])

  // The Menu's Jump to column, by tab key. Clears a search first
  // (results replace the columns), then selects the column on both
  // layouts: the phone's tab (the strip scrolls it into view, as for
  // any tab selected by code) and the desktop grid's column.
  const jumpToCategory = useCallback((key: string) => {
    const col = columns.find((c) => c.key === key)
    if (col) {
      setSearchInput('')
      setSearchQuery('')
      setSearchOpen(false)
      // For You on the desktop is the row above the grid, not a column
      // the arrow keys select: the selection stays where it is.
      if (keyColumns.some((c) => c.key === key)) setSelectedColumnKey(col.key)
      setSelectedCardId(null)
      selectTab(col.key)
      requestAnimationFrame(() => {
        // A column's ref is a ``display: contents`` wrapper, which has
        // no box to scroll to; its first child does (For You's row: its
        // header).
        const el = columnRefs.current.get(key)
        ;(el?.firstElementChild as HTMLElement | null)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
      })
    }
  }, [columns, keyColumns, setSelectedCardId, columnRefs, selectTab])

  const setPrefsFor = useCallback((columnName: string, prefs: ColumnPrefs) => {
    setColumnPrefs(columnName, prefs as ColumnPrefsValue)
  }, [setColumnPrefs])

  // C8's row opens its tab the way the Menu's Jump to column does: the
  // phone's tab, or the desktop column scrolled into view. Stable: the
  // row's Column keeps old callbacks, and ``jumpToCategory`` changes
  // with the columns.
  const jumpRef = useRef(jumpToCategory)
  jumpRef.current = jumpToCategory
  const openTab = useCallback((key: ColKey) => jumpRef.current(key), [])

  // The outer div lets us register a per-column ref on a wrapper
  // without breaking the grid layout — the wrapper uses
  // ``display: contents`` so its child (Column) participates in
  // the grid as if it weren't wrapped.
  const setColumnRef = (key: ColKey) => (el: HTMLElement | null) => {
    if (el) columnRefs.current.set(key, el)
    else columnRefs.current.delete(key)
  }

  // --- Render gates -----------------------------------------------------

  if (!authProbed) {
    // Splash while the ``/api/me`` probe resolves. Centred spinner +
    // "Connecting…" copy so the user knows the app is alive and not
    // blank — an empty div here made the dashboard feel frozen on
    // cold loads. Spinner is the same shape as the Refresh button's
    // ``animate-spin`` so the visual language stays consistent.
    //
    // ``position: fixed; inset: 0`` is INLINE so this surface covers
    // the viewport from the first frame after mount, regardless of
    // whether the Tailwind CSS bundle has loaded yet. Without it,
    // the ``h-full`` Tailwind class only takes effect after
    // ``styles.css`` parses — the window between React replacing
    // the index.html splash and the CSS landing is the visible
    // black flicker. Background is the same pure-black as the
    // index.html splash so the transition is a swap, not a fade.
    return (
      <div
        style={{
          position: 'fixed',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 12,
          backgroundColor: '#000000',
          color: '#ffffff',
          fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", system-ui, sans-serif',
        }}
      >
        <div
          // ``data-spinner="popping"`` lets the styles.css
          // ``@media (prefers-reduced-motion: reduce)`` rule kill
          // the animation — Tailwind's ``motion-safe:`` variant
          // doesn't apply to inline ``animation:`` declarations
          // (it only gates Tailwind utility classes). The data
          // attribute is the cleanest cross-handle.
          data-spinner="popping"
          style={{
            width: 32,
            height: 32,
            borderRadius: '50%',
            border: '2px solid rgba(255,255,255,0.08)',
            borderTopColor: '#0a84ff',
            animation: 'popping-spin 0.9s linear infinite',
          }}
        />
        <p
          style={{
            fontSize: 17,
            fontWeight: 600,
            letterSpacing: '-0.41px',
            color: 'rgba(235, 235, 245, 0.6)',
            margin: 0,
          }}
        >
          Popping
        </p>
      </div>
    )
  }

  if (!oidcDisabled && user === null) {
    // Reload on sign-in/out: PreferencesProvider loads once per page, and
    // a reload makes it fetch the new account's preferences instead of
    // writing the previous state into it.
    return <LoginPage returnTo="/" onSignedIn={() => window.location.reload()} />
  }

  const showSearchView = searchQuery.trim().length > 0

  return (
    <div
      // Inline-styled black background on the dashboard root so the
      // splash→dashboard swap doesn't expose a transparent frame in
      // the brief window where ``h-full`` (Tailwind class) hasn't
      // computed yet because ``styles.css`` is still being parsed.
      // Matches the post-React splash's inline ``#000000`` so the
      // transition is one continuous black surface. Mirrors the
      // same fix as the splash (see comment block above) but on the
      // destination side. No min-height here: html, body and #root
      // are already height:100% (styles.css), and #root adds the
      // bottom safe-area padding. A 100dvh min-height on top of that
      // gave the home-screen app a 34px scroll range, so the whole
      // UI, header included, could be dragged up and down.
      style={{ backgroundColor: '#000000' }}
      // ``isolate`` makes this div its own stacking context. Without
      // it, this div's own inline black background — an in-flow,
      // non-positioned box — paints *after* Wallpaper's negative
      // z-index layer (which pops out to the nearest stacking
      // context, i.e. the page root) per CSS painting order, so the
      // opaque black would cover the wallpaper entirely. Isolating
      // here means this element's own background is step one of
      // *its* stacking context, with Wallpaper's negative z-index
      // painting above that — correctly behind the header/columns,
      // which are the later, non-negative-z-index children.
      className="h-full flex flex-col isolate"
    >
      <Wallpaper />
      {/* Large-title navigation bar, like Apple Mail and Music: a
          34pt title on the left, then search, refresh (phones) and the
          Menu button on the right. On ``sm+`` a second row carries the
          health line, Refresh and Brief. A hairline divider sits at the
          bottom — the UI leans on these 1px ``rgba(255,255,255,0.08)``
          lines as its only border. Stacking: header z-20 < Menu z-30/40
          < Settings z-50 < toasts z-[60]. data-app-header tells this
          bar apart from the Column headers for the smoke test. */}
      <header data-app-header className="sticky top-0 z-20 pt-[env(safe-area-inset-top)] bg-bg-app/85 supports-[backdrop-filter]:backdrop-blur-xl border-b border-hairline">
        {/* Title row. The title never truncates: with one Menu button
            the row has room for "Popping" at 320 and at 312 (Page Zoom),
            and below 360px it drops to 28px for margin (smoke rule C16).
            While search is open on a phone the field takes the row and
            the title steps aside. */}
        <div className="flex items-end justify-between pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] pt-3 pb-2 sm:pt-4 sm:pb-3">
          <h1 className={`shrink-0 whitespace-nowrap text-ios-large-title max-[359px]:text-[28px] max-[359px]:leading-[34px] text-label-primary ${searchOpen ? 'hidden sm:block' : ''}`}>
            Popping
          </h1>
          <div className={`flex items-center gap-1 sm:gap-2 pb-1 ${searchOpen ? 'flex-1 min-w-0 justify-end sm:flex-none' : ''}`}>
            {/* Search affordance. Collapsed: a 44×44 button with a
                magnifying-glass icon. Expanded: full-width input with
                a leading glass + trailing clear-X. The transition is
                a plain width swap — no JS animation library; the
                backdrop-blur above keeps the swap from feeling cheap. */}
            {searchOpen ? (
              <div className="flex items-center gap-2 bg-bg-elevated rounded-ios px-3 h-11 flex-1 min-w-0 sm:flex-none sm:w-72 animate-fade-in">
                <SearchIcon className="w-4 h-4 text-label-secondary shrink-0" />
                <input
                  id="app-search"
                  // A text input with search hints, not type="search":
                  // iOS draws its own clear button inside a search
                  // field, next to ours.
                  type="text"
                  inputMode="search"
                  enterKeyHint="search"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  autoFocus
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  onBlur={() => {
                    // Collapse when the user blurs only if the
                    // field is empty. Otherwise keep the input open
                    // so a stray tap outside doesn't lose the query.
                    if (!searchInput) setSearchOpen(false)
                  }}
                  onKeyDown={(e) => {
                    // Enter dismisses the iOS keyboard and keeps the
                    // query (results are live, there is nothing to submit).
                    if (e.key === 'Enter') e.currentTarget.blur()
                  }}
                  placeholder="search"
                  className="flex-1 min-w-0 bg-transparent text-ios-body text-label-primary placeholder:text-label-tertiary focus:outline-none"
                />
                {searchInput && (
                  <button
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      setSearchInput('')
                      setSearchOpen(false)
                    }}
                    aria-label="clear search"
                    className="w-11 h-11 -mr-3 flex items-center justify-center shrink-0 text-label-secondary active:text-label-primary"
                  >
                    <ClearIcon className="w-4 h-4" />
                  </button>
                )}
              </div>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setSearchOpen(true)
                  // Focus the input on the next tick — the input
                  // mounts in the same render but autoFocus only
                  // fires once. Belt and braces.
                  requestAnimationFrame(() =>
                    document.getElementById('app-search')?.focus(),
                  )
                }}
                aria-label="open search"
                className="w-11 h-11 flex items-center justify-center rounded-full text-label-primary active:bg-bg-elevated"
              >
                <SearchIcon className="w-5 h-5" />
              </button>
            )}
            {/* Refresh affordance. Always-visible icon button so
                mobile users can manually force a refresh without
                having to open the Drawer (the only Refresh path on
                mobile before this fix — one extra tap the user
                shouldn't have to make). On ``sm+`` the icon is
                hidden because the text Refresh button lives on the
                sub-row below. */}
            {!searchOpen && (
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={refreshing}
              aria-label="refresh"
              className="sm:hidden w-11 h-11 flex items-center justify-center rounded-full text-label-primary active:bg-bg-elevated disabled:opacity-40"
            >
              <RefreshIcon className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
            )}
            {/* The one Menu button (≡): Settings, saved views, Jump to
                column and the source filter all live behind it. */}
            <Hamburger open={drawerOpen} onClick={() => setDrawerOpen(true)} />
          </div>
        </div>
        {/* Sub-row: health status + Refresh + Brief. Hidden on mobile
            because the actions duplicate what the Drawer already
            offers — opening the Drawer takes one tap. The row is
            laid out as a single flex line so the trailing buttons
            stay aligned with the title row's right edge. */}
        <div className="hidden sm:flex items-center justify-between pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] pb-2 -mt-1">
          <span className="text-ios-caption text-label-secondary truncate">
            {health
              ? `${health.entries} entries · ${health.sources} sources · ${health.status}`
              : 'connecting…'}
          </span>
          <div className="flex items-center gap-2">
            {/* A newer build is served (useUpdateCheck). Not a status
                region: nothing role="status" stays mounted while idle. */}
            {isDesktop && update.ready && (
              <button
                type="button"
                onClick={update.reload}
                className="min-h-[44px] -my-1.5 rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated"
              >
                New version · Reload
              </button>
            )}
            <button
              onClick={() => void refresh()}
              disabled={refreshing}
              className="min-h-[44px] -my-1.5 rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated disabled:opacity-40"
            >
              {refreshing ? '…' : 'Refresh'}
            </button>
            {llmConfigured === false ? (
              <button
                type="button"
                data-brief-action="set-up-ai"
                onClick={() => openSettings('llm')}
                className="min-h-[44px] -my-1.5 rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated whitespace-nowrap"
              >
                Brief needs an AI provider · Set up
              </button>
            ) : (
              <button
                onClick={() => void triggerBriefGenerate(briefTone, setError)}
                disabled={generatingBrief}
                className="min-h-[44px] -my-1.5 rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated disabled:opacity-40"
                title="Generate today's brief now"
              >
                {generatingBrief ? '…' : 'Brief'}
              </button>
            )}
            {user && <UserBadge user={user} onSignedOut={() => window.location.reload()} />}
          </div>
        </div>
      </header>

      {activeSources.size > 0 && (
        <div data-hscroll className="pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] border-b border-hairline bg-bg-surface flex items-center gap-2 text-ios-caption overflow-x-auto whitespace-nowrap">
          <span className="text-label-secondary shrink-0">filtering by:</span>
          {Array.from(activeSources).map((src) => (
            // ``animate-fade-in`` fires on every mount (the animation
            // restarts when the chip re-mounts after a re-render).
            // 180ms is short enough that it doesn't lag the tap but
            // long enough that the eye notices the chip land when
            // several arrive in quick succession. ``motion-safe:``
            // keeps it off for users who've asked the OS to reduce
            // motion.
            <button
              key={src}
              onClick={() => toggleSource(src)}
              className="group shrink-0 min-h-[44px] flex items-center motion-safe:animate-fade-in"
              aria-label={`remove ${sourceLabel(src)} from filter`}
            >
              <span className="rounded-full bg-accent-soft px-2.5 py-1 text-accent text-ios-caption group-active:opacity-60">
                {sourceLabel(src)} ✕
              </span>
            </button>
          ))}
          <button
            onClick={() => setActiveSources(new Set())}
            className="group shrink-0 ml-auto min-h-[44px] flex items-center"
          >
            <span className="rounded-full border border-hairline px-2.5 py-1 text-ios-caption text-accent group-active:opacity-60">
              clear all
            </span>
          </button>
        </div>
      )}

      {error && (
        <div
          role="alert"
          aria-live="assertive"
          className="sticky top-0 z-10 pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] py-2 bg-red-500/15 border-b border-red-500/40 text-ios-caption text-red-200 flex items-center justify-between gap-2"
        >
          <span className="break-words">{error}</span>
          <button
            onClick={() => setError(null)}
            className="shrink-0 w-11 h-11 -my-2 -mr-3 flex items-center justify-center text-red-200/80 active:text-red-200"
            aria-label="dismiss error"
          >
            ✕
          </button>
        </div>
      )}

      {/* The desktop scrolls as one page: this wrapper is its only
          scroller (``data-scroller``), from Brief down to the grid, and
          nothing inside it scrolls on its own. It is ``min-h-0`` so it
          takes the height under the header; before, the grid was that
          flex scroller and a For You row taller than the window
          squeezed it to its padding while the document scrolled. On a
          phone it is ``contents``, so the chips, the search results and
          the phone ``<main>`` stay this column's own flex items. Always
          the same element, so crossing 768px remounts nothing. */}
      <div
        ref={isDesktop ? setDesktopScroller : undefined}
        data-scroller={isDesktop ? '' : undefined}
        className={isDesktop ? 'flex-1 min-h-0 overflow-y-auto overscroll-contain' : 'contents'}
      >
        {/* Desktop keeps the Brief inline at the top — there's room for
            it. Mobile moves it into its own tab (see the mobile main
            below) so the column content isn't pushed below the fold
            by a card the user has to scroll past every time. */}
        {isDesktop && (
          <div>
            <BriefCard
              brief={brief}
              onBriefChange={setBrief}
              tone={briefTone}
              onToneChange={setBriefTone}
              triggerGenerate={triggerBriefGenerate}
              llmConfigured={llmConfigured}
              onOpenSettings={openSettings}
            />
          </div>
        )}

        <PresetChips
          presets={filterPresets}
          onApply={applyPreset}
          onDelete={deletePreset}
          onRename={renamePreset}
          onUpdate={updatePreset}
        />

        {showSearchView ? (
          <main className={`${isDesktop ? '' : 'flex-1 overflow-y-auto overscroll-contain '}py-3 sm:py-4 pl-[max(0.75rem,env(safe-area-inset-left))] pr-[max(0.75rem,env(safe-area-inset-right))] sm:pl-[max(1rem,env(safe-area-inset-left))] sm:pr-[max(1rem,env(safe-area-inset-right))]`}>
            <SearchResults
              query={searchQuery}
              entries={searchResults.filter((e) => !hiddenSet.has(e.id))}
              sourcesById={sourcesById}
              error={searchError}
              searching={searching}
              categoriesBySourceId={categoriesBySourceId}
              faviconBySourceId={faviconBySourceId}
              onOpenEntry={openEntry}
              expandedSummaries={expandedSummaries}
              onToggleSummary={toggleSummary}
              onHide={hideStory}
              onStar={starStory}
              starredSet={starredSet}
              onVote={voteEntry}
              votedMap={votedMap}
            />
          </main>
        ) : !firstRefreshDone ? (
          // First load in flight: the tabs would otherwise show empty for
          // a moment before the very first response lands. Saved and For
          // You are in the strip from the first render, so the columns are
          // never empty; an empty tab says so itself ("No stories yet").
          <div className={isDesktop ? undefined : 'flex-1 overflow-y-auto overscroll-contain'}>
            <SkeletonGrid />
          </div>
        ) : (
          <>
            {/* Desktop. The view is two surfaces stacked:
                1. For You row — only in 'all' view, whenever its tab is
                   shown (Edit tabs), empty or not, as the phone's For You
                   tab is: Jump to column lands on it. Full-width card grid;
                   no column chrome so the personal feed reads as the "front
                   page". Empty, it says "No stories yet", with the
                   first-feed button while every feed is a built-in.
                2. By-category grid — the All Subs columns. Skipped in
                   'multisub' view because the multi-sub column is the
                   whole dashboard there. */}
            {isDesktop && viewKind === 'all' && !hiddenTabs.has(FORYOU_KEY) && (
              <section ref={setColumnRef(FORYOU_KEY)} className="pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] pt-4 pb-3 border-b border-hairline">
                <header className="flex items-center justify-between mb-2">
                  <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
                    For You
                  </h2>
                </header>
                {visibleForYou.length === 0 && (
                  <div className="px-1 space-y-3">
                    <p className="text-ios-body text-label-secondary">No stories yet</p>
                    {noOwnFeeds && <FirstFeedButton onClick={openFeeds} />}
                  </div>
                )}
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {visibleForYou.slice(0, FORYOU_ROW_TOP).map((e) => {
                    // Per-card engagement props: hide, save and
                    // vote, the same as in a category column.
                    // Opening a card uses its story up
                    // (``onActivate``), here as anywhere. The row
                    // keeps the server's order and draws no marker
                    // and no dimming: on the desktop a story is used
                    // up only by opening it or by Mark all (C2).
                    //
                    // ``selected`` / ``cardRef`` are not passed
                    // because keyboard nav is per-column, not
                    // per-row, and the For You row is not a
                    // "column" the user can scroll into.
                    return (
                      <Card
                        key={e.id}
                        entry={e}
                        sourceName={sourcesById.get(e.source_id)}
                        sourceFaviconPath={faviconBySourceId.get(e.source_id)}
                        category={categoriesBySourceId.get(e.source_id)}
                        expanded={expandedSummaries.has(e.id)}
                        onToggleSummary={() => toggleSummary(e.id)}
                        onActivate={() => openEntry(e)}
                        onHide={() => hideStory(e.id)}
                        onStar={() => starStory(e.id)}
                        starred={starredSet.has(e.id)}
                        onVote={(dir) => voteEntry(e.id, dir)}
                        vote={votedMap.get(e.id) ?? null}
                        onSourceTap={onSourceTap}
                      />
                    )
                  })}
                </div>
              </section>
            )}
            {isDesktop && viewKind === 'all' && <FramingWatch />}
            {isDesktop && (
              <main className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 py-4 pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))] overflow-x-clip">
                {keyColumns.map((col) => (
                  <div key={col.key} ref={setColumnRef(col.key)} className="contents">
                    <Column
                      columnKey={col.key}
                      label={col.label}
                      sourceName={col.sourceName}
                      sentinelRoot={desktopScroller}
                      sections={sectionsFor(col)}
                      unseenCount={countFor(col)}
                      nextTab={nextTabFor(col.key)}
                      generation={parts.get(col.key)?.snapshot.generation}
                      sourcesById={sourcesById}
                      sourcesByName={sourcesByName}
                      noOwnFeeds={noOwnFeeds}
                      onAddFeed={openFeeds}
                      onSourceTap={onSourceTap}
                      selectedId={col.key === selectedKeyColumn?.key ? selectedCardId ?? undefined : undefined}
                      cardRefs={cardRefs}
                      onOpenEntry={openEntry}
                      onMarkAll={() => markColumnRead(col.key)}
                      onOpenTab={openTab}
                      onHoldChange={(hold) => setHold(col.key, hold)}
                      onSelectionFolded={dropSelection}
                      onHideEntry={hideStory}
                      onStarEntry={starStory}
                      starredSet={starredSet}
                      onVoteEntry={voteEntry}
                      votedMap={votedMap}
                      prefs={prefsFor(col)}
                      windowAnswered={windowAnsweredFor(col)}
                      onPrefsChange={(next) => setPrefsFor(col.name, next)}
                      categoriesBySourceId={categoriesBySourceId}
                      faviconBySourceId={faviconBySourceId}
                      expandedSummaries={expandedSummaries}
                      onToggleSummary={toggleSummary}
                    />
                  </div>
                ))}
              </main>
            )}

            <main className="md:hidden flex-1 min-h-0 flex flex-col py-3 pl-[max(0.75rem,env(safe-area-inset-left))] pr-[max(0.75rem,env(safe-area-inset-right))]">
              {/* Tab bar. Replaces the old swipe-to-change-column
                  gesture — that gesture had no direction lock (any
                  60px-plus horizontal touch delta fired it, scroll
                  wobble included) and it collided with the new
                  per-card swipe actions in Card.tsx, which now own
                  the horizontal-drag gesture on mobile. Tabs are the
                  explicit, discoverable replacement; "Brief" is a tab
                  here (not inline above the column, like on desktop)
                  so it doesn't push the column below the fold on a
                  small screen. Horizontally scrollable — a source
                  list with 6+ categories won't fit every tab label on
                  a phone-width screen. */}
              {/* The strip, and the Edit pill pinned at its trailing edge,
                  outside the tablist. */}
              <div className="shrink-0 flex items-start gap-1.5 -mx-1">
                <div
                  ref={stripRef}
                  role="tablist"
                  aria-label="dashboard sections"
                  data-hscroll
                  className="min-w-0 flex-1 flex gap-1.5 overflow-x-auto pb-1 px-1"
                >
                  {/* Each tab is a 44px button around a smaller pill, so the
                      strip stays compact while every tab is easy to hit. */}
                  <button
                    type="button"
                    role="tab"
                    aria-selected={shownTab === BRIEF_KEY}
                    onClick={() => onPhoneTabTap(BRIEF_KEY)}
                    className="group shrink-0 min-h-[44px] flex items-center"
                  >
                    <span
                      className={`rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
                        shownTab === BRIEF_KEY
                          ? 'bg-accent text-white'
                          : 'bg-bg-surface text-label-secondary group-active:bg-bg-elevated'
                      }`}
                    >
                      Brief
                    </span>
                  </button>
                  {columns.map((c) => {
                    // The badge: how many of the tab's stories aren't
                    // seen yet (D4), 99+ past 99. None on Saved (and
                    // Brief has no column). Opening a tab marks nothing.
                    const count = countFor(c)
                    const shown = c.key === shownTab
                    return (
                      <button
                        type="button"
                        key={c.key}
                        role="tab"
                        aria-selected={shown}
                        aria-label={count > 0 ? `${c.label}, ${count} new` : undefined}
                        onClick={() => onPhoneTabTap(c.key)}
                        className="group shrink-0 min-h-[44px] flex items-center"
                      >
                        <span
                          className={`flex items-center gap-1 rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
                            shown
                              ? 'bg-accent text-white'
                              : 'bg-bg-surface text-label-secondary group-active:bg-bg-elevated'
                          }`}
                        >
                          {c.label}
                          {count > 0 && (
                            <span
                              data-badge
                              className={`rounded-full px-1.5 text-[11px] leading-4 font-semibold ${
                                shown ? 'bg-white/25 text-white' : 'bg-accent-soft text-accent'
                              }`}
                            >
                              {count > 99 ? '99+' : count}
                            </span>
                          )}
                        </span>
                      </button>
                    )
                  })}
                </div>
                <button
                  ref={editPillRef}
                  type="button"
                  aria-label="Edit tabs"
                  data-edit-tabs
                  onClick={() => openTabsEditor('pill')}
                  className="group shrink-0 min-h-[44px] flex items-center pr-1"
                >
                  <span className="rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap bg-bg-surface text-accent group-active:bg-bg-elevated">
                    Edit
                  </span>
                </button>
              </div>

              {/* Scrolls only the Brief tab: a Column fills it (h-full) and
                  scrolls its own list, the phone's ``data-scroller``. */}
              <div ref={phoneBodyRef} className="flex-1 min-h-0 overflow-y-auto overscroll-contain">
                {!phoneCol ? (
                  <BriefCard
                    brief={brief}
                    onBriefChange={setBrief}
                    tone={briefTone}
                    onToneChange={setBriefTone}
                    triggerGenerate={triggerBriefGenerate}
                    llmConfigured={llmConfigured}
                    onOpenSettings={openSettings}
                    startExpanded
                  />
                ) : (
                  <Column
                    // A new list per tab: it starts at the top, and the
                    // scroller hooks rebind to it.
                    key={phoneCol.key}
                    // The phone list is the phone's scroller, and its own
                    // sentinel root. While this layout is hidden on a
                    // desktop it holds neither: the desktop's one
                    // ``data-scroller`` is the wrapper above.
                    scrollerRef={isDesktop ? undefined : setPhoneScroller}
                    sentinelRoot={phoneScroller}
                    // Seen on scroll (PR3c): every tab but Saved. The
                    // registry keeps the cards while the phone layout is
                    // hidden on a desktop (no observer runs there).
                    trackSeen={phoneCol.key !== SAVED_KEY}
                    seenRegistry={seenOnScroll}
                    columnKey={phoneCol.key}
                    label={phoneCol.label}
                    sourceName={phoneCol.sourceName}
                    sections={sectionsFor(phoneCol)}
                    unseenCount={countFor(phoneCol)}
                    nextTab={nextTabFor(phoneCol.key)}
                    generation={parts.get(phoneCol.key)?.snapshot.generation}
                    sourcesById={sourcesById}
                    sourcesByName={sourcesByName}
                    noOwnFeeds={noOwnFeeds}
                    onAddFeed={openFeeds}
                    onSourceTap={onSourceTap}
                    selectedId={
                      phoneCol.key === selectedKeyColumn?.key ? selectedCardId ?? undefined : undefined
                    }
                    cardRefs={cardRefs}
                    onOpenEntry={openEntry}
                    onMarkAll={() => markColumnRead(phoneCol.key)}
                    onOpenTab={openTab}
                    onHoldChange={(hold) => setHold(phoneCol.key, hold)}
                    onSelectionFolded={dropSelection}
                    onHideEntry={hideStory}
                    onStarEntry={starStory}
                    starredSet={starredSet}
                    onVoteEntry={voteEntry}
                    votedMap={votedMap}
                    prefs={prefsFor(phoneCol)}
                    windowAnswered={windowAnsweredFor(phoneCol)}
                    onPrefsChange={(next) => setPrefsFor(phoneCol.name, next)}
                    categoriesBySourceId={categoriesBySourceId}
                    faviconBySourceId={faviconBySourceId}
                    expandedSummaries={expandedSummaries}
                    onToggleSummary={toggleSummary}
                  />
                )}
              </div>
            </main>
          </>
        )}
      </div>

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        tabs={tabOrder.shown}
        sources={sources}
        onRefreshSources={() => refresh({ silent: true })}
        activeSources={activeSources}
        onSourceToggle={toggleSourceAndMaybeClose}
        onClearAllFilters={clearSourceFilters}
        onCategoryJump={jumpToCategory}
        onOpenSettings={(tab) => openSettings(tab ?? 'feeds')}
        onEditTabs={() => openTabsEditor('menu')}
        onEditSource={(id) => openSettings('feeds', id)}
        onSavePreset={(name) => {
          savePreset(name)
          toast(`Saved view “${name.trim() || 'Untitled preset'}”`, 'info')
        }}
      />
      <Settings
        open={settingsOpen}
        tab={settingsTab}
        sources={sources}
        onRefreshSources={() => refresh({ silent: true })}
        onClose={closeSettings}
        focusSourceId={settingsSourceId}
        focusNonce={settingsFocusNonce}
        briefTone={briefTone}
        onBriefToneChange={setBriefTone}
        triggerGenerate={triggerBriefGenerate}
        generating={generatingBrief}
        onSourceAdded={insertSource}
        onSourceDeleted={removeSource}
        onEditSource={onEditSource}
        onResetLocalState={resetLocalState}
        hiddenEntries={hiddenEntries}
        onRestoreHidden={(entryId) => restoreHiddenEntry(entryId)}
        onRestoreAllHidden={() => {
          // The ids hidden now; one hidden on another device since
          // this page last read them stays hidden. Restored as any
          // hide's Undo is, so the ones a poll dropped come back at once.
          const ids = latestPrefs().hiddenEntries
          if (ids.length === 0) return
          restoreHiddenEntries(ids)
          toast('All hidden entries restored.', 'info')
        }}
        starredEntries={starredEntries}
        onUnstarAll={() => {
          const cleared = latestPrefs().starredEntries
          if (cleared.length === 0) return
          removeIds(PREFERENCE_KEYS.starredEntries, cleared)
          // Undo puts back what was cleared and keeps anything saved
          // since the toast appeared (an add, not a new list).
          toast('All saved entries cleared.', {
            kind: 'info',
            action: {
              label: 'Undo',
              onClick: () => addIds(PREFERENCE_KEYS.starredEntries, cleared),
            },
          })
        }}
        onUnstarEntry={(entryId) => {
          // Per-row unstar from the Settings → Saved
          // tab list. Mirrors the card's star
          // button + the per-card context-menu
          // "Unsave" action.
          setStarred(entryId, false)
        }}
        downvotedEntries={downvotedEntries}
        onHideDownvoted={(entryId) => hideEntry(entryId)}
        onHideAllDownvoted={() => {
          const ids = downvotedEntries
          if (ids.length === 0) return
          const before = latestPrefs().hiddenEntries
          // Only the ones this hid; Undo leaves the rest as they are now.
          const added = ids.filter((id) => !before.includes(id))
          // Kept for the Undo, which puts them back as a single hide's does.
          hideEntries(added)
          toast(
            `Hid ${ids.length} downvoted ${ids.length === 1 ? 'entry' : 'entries'}.`,
            {
              kind: 'info',
              action: {
                label: 'Undo',
                onClick: () => restoreHiddenEntries(added),
              },
            },
          )
        }}
      />

      <ShortcutsSheet
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />
      <TabsEditor
        open={tabsEditorOpen}
        onClose={closeTabsEditor}
        tabs={tabOrder.all}
        hidden={hiddenTabs}
        onMove={moveTab}
        onShow={showTab}
      />

      {/* The card's ⋯ sheet (PR5a). It reports open / closed so seen on
          scroll pauses under it (``modalOpen``). */}
      <ActionSheetHost onOpenChange={setActionSheetOpen} />
      <ToastHost />
    </div>
  )
}

// iOS-style magnifying-glass icon. Used for the search affordance in
// the top-bar. 1.75px stroke mirrors Apple's SF Symbols line weight
// for "magnifyingglass" at small sizes; rounded caps/joins so the
// ellipse ends look as soft as the SF Symbol rendering.
function SearchIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="11" cy="11" r="7" />
      <line x1="20" y1="20" x2="16" y2="16" />
    </svg>
  )
}

// Small × glyph used to clear the expanded search field. Same stroke
// weight as SearchIcon so the two sit visually paired inside the
// rounded pill.
function ClearIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" fill="currentColor" stroke="none" opacity="0.2" />
      <line x1="9" y1="9" x2="15" y2="15" />
      <line x1="15" y1="9" x2="9" y2="15" />
    </svg>
  )
}

// iOS-style refresh icon — an open arc ending in a chevron. The
// chevron direction tells you which way the circle will rotate
// (clockwise == moving forward). Same stroke weight as the rest of
// the header icons. ``animate-spin`` (Tailwind) drives a CSS-only
// spin when ``refreshing`` is true.
function RefreshIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {/* Two arcs that together describe a near-full circle, broken
          at the 3-o'clock position so the chevron has space to live. */}
      <path d="M20 12a8 8 0 0 1-8 8" />
      <path d="M4 12a8 8 0 0 1 14.5-4.7" />
      <polyline points="20 4 20 8 16 8" />
    </svg>
  )
}



















