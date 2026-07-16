// App: 3-4 column desktop grid, single-column mobile with swipe.
// Top bar has the hamburger (opens Drawer), search input, and refresh.
// When OIDC is enabled and the user isn't logged in, the dashboard
// content is replaced with a LoginPage.
//
// Layered UX features (each documented at the call site):
//   - Active source filter chips (multi-select via Drawer)
//   - Per-column sort/filter popover (the ⋯ button)
//   - Per-column read/unread state in localStorage
//   - New-entry chip in column headers (union of refresh-delta +
//     last-viewed-delta)
//   - Search (debounced, replaces columns when query is set)
//   - Keyboard navigation on desktop (←/→/↑/↓/Enter)
//   - Tab-visibility-aware polling (pauses when tab is hidden,
//     force-refresh + reset seen-set when it returns)
//   - Brief tone picker (terse / narrative / alert)
//   - Source filter chips in the header

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Brief, type CurrentUser, type Entry, type Health, type Source } from './api'
import { BriefCard } from './components/BriefCard'
import { Card } from './components/Card'
import { Column, DEFAULT_PREFS, type ColumnPrefs } from './components/Column'
import { Drawer } from './components/Drawer'
import { FramingWatch } from './components/FramingWatch'
import { Hamburger } from './components/Hamburger'
import { LoginPage } from './components/LoginPage'
import { SearchResults } from './components/SearchResults'
import { ShortcutsSheet } from './components/ShortcutsSheet'
import { ToastHost, toast } from './components/Toast'
import { UserBadge } from './components/UserBadge'
import { Settings, type SettingsTab } from './components/Settings'
import { recordImmediate } from './lib/interactions'
// Per-user server-backed preferences. Replaces the 8 localStorage
// keys the dashboard used to write (readEntries, lastViewed,
// columnPrefs, columnSections, hiddenEntries, starredEntries,
// filterPresets, historyGroupBy). The localStorage versions
// were per-device, so a phone and a laptop saw different read
// state, different "+N new" counts, and different sort
// orders. The server-backed store keys by user_id, so all
// devices on the same LAN (sharing the bypass user_id) see
// the same state. See ``lib/preferences.tsx`` for the
// provider, the debounced sync, and the one-time
// localStorage seed that runs on first launch.
import {
  PREFERENCE_KEYS,
  usePreferences,
  type ColumnPrefsValue,
  type ColumnSectionsValue,
  type FilterPresetValue,
  type ReadEntriesValue,
  MAX_HIDDEN,
  MAX_STARRED,
  MAX_PRESETS,
} from './lib/preferences'
import { STORAGE_KEYS, safeGetItem, safeSetItem } from './lib/storage'

const REFRESH_INTERVAL_MS = 60_000
// Health pings don't need 60s cadence — DB / Redis status is slow-
// moving and the user only needs to see "everything is green" most
// of the time. 5 min cuts the request count by 5x for the cost of
// up-to-5-minute staleness on the status chip.
const HEALTH_INTERVAL_MS = 5 * 60_000
// Hidden longer than this → treat return as "fresh start"; the new-
// entry indicator resets and all entries surface as unread. Without
// this, returning from a long absence shows nothing flagged (because
// the seen-set still has the old ids).
const HIDDEN_RESET_MS = 2 * 60 * 1000

// localStorage keys live in ``lib/storage.ts`` so feature
// modules share one namespaced key registry. Only two keys
// remain there (mobileColLast, briefCollapsed) -- the rest
// moved to the server-backed ``user_preferences`` store
// (``lib/preferences.tsx``) so all of a user's devices see
// the same state.

// -1 is the "Brief" tab, a sibling of the column tabs rather than an
// index into ``columns``. Valid range is [-1, columns.length).
function loadMobileCol(): number {
  if (typeof window === 'undefined') return 0
  try {
    const raw = safeGetItem(STORAGE_KEYS.mobileColLast)
    if (!raw) return 0
    const n = Number(raw)
    return Number.isInteger(n) && n >= -1 ? n : 0
  } catch {
    return 0
  }
}

// Per-card manual read-set. Stored as ``{columnName: number[]}`` so
// JSON.stringify round-trips cleanly. Entries that disappear from
// the dashboard (older than the column's filter, or pruned by the
// 200-row fetch cap) are eventually garbage-collected when their
// column isn't rendered, but a few stale ids are cheap — they just
// sit in localStorage. Valued-shaped (numbers only) so the per-card
// JSON.parse stays fast at boot.
//
// ``MAX_PER_COLUMN`` caps each column's list so a long-running
// install doesn't grow ``readEntries`` past the 5 MB localStorage
// quota (the bug sweep found the cap was previously unbounded —
// every mark-read appended forever and ``safeSetItem``'s quota
// rejection was silently swallowed by App). When the cap is hit
// we keep the LAST ``MAX_PER_COLUMN`` ids (newest reads win; the
// oldest "I marked this read" decisions are also the ones most
// likely to have been rotated out of the column's fetch window).


// Per-column upper bound on manually-marked read entries. Chosen
// to fit comfortably in localStorage across all columns combined:
// 8 active columns × 200 entries × ~12 bytes JSON each ≈ 19 KB,
// well under the 5 MB quota even for a power user with extra
// columns. If a single column has more than this many manual marks
// (it shouldn't, given the 50-item entry fetch cap), the oldest
// marks are dropped — which is fine, because those entries have
// long since aged out of the column's view.
const MAX_PER_COLUMN = 200


// Per-column Fresh/History section collapse state. The
// shape is forgiving on read: a missing column name, a
// missing field, or a non-boolean value silently falls back to
// "expanded" so a future shape change can't strand the user with
// permanently-collapsed sections.
type SectionCollapse = { newCollapsed: boolean; historyCollapsed: boolean }
type ColumnSections = Record<string, SectionCollapse>

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
      className="flex gap-1.5 overflow-x-auto px-4 sm:px-6 py-2 border-b border-hairline bg-bg-app"
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
  const [mode, setMode] = useState<'idle' | 'actions' | 'confirmDelete'>('idle')

  if (mode === 'confirmDelete') {
    return (
      <div className="shrink-0 flex items-center gap-1.5 rounded-full bg-red-500/10 border border-red-500/30 pl-2.5 pr-1 py-0.5">
        <span className="text-ios-caption text-red-400">Delete?</span>
        <button
          onClick={onDelete}
          className="text-ios-caption text-red-400 active:opacity-60 font-semibold"
          aria-label={`confirm delete preset ${preset.name}`}
        >
          Yes
        </button>
        <button
          onClick={() => setMode('idle')}
          className="text-ios-caption text-label-secondary active:opacity-60"
          aria-label={`cancel delete preset ${preset.name}`}
        >
          No
        </button>
      </div>
    )
  }

  if (mode === 'actions') {
    return (
      <div className="shrink-0 flex items-center gap-2 rounded-full bg-bg-elevated border border-hairline pl-2.5 pr-1.5 py-0.5">
        <button
          onClick={() => {
            // Native prompt, pre-filled with the current name —
            // same low-ceremony pattern "Save current view" uses.
            const name = window.prompt('Rename this view', preset.name)
            setMode('idle')
            if (name === null) return
            const trimmed = name.trim()
            if (!trimmed || trimmed === preset.name) return
            onRename(trimmed)
            toast(`Renamed to “${trimmed}”.`, 'info')
          }}
          className="text-ios-caption text-label-primary active:opacity-60"
          aria-label={`rename preset ${preset.name}`}
        >
          Rename
        </button>
        <button
          onClick={() => {
            onUpdate()
            setMode('idle')
            toast(`“${preset.name}” updated to the current view.`, 'info')
          }}
          className="text-ios-caption text-label-primary active:opacity-60"
          aria-label={`update preset ${preset.name} to the current filter and column settings`}
          title="Overwrite this view with the current filter + per-column settings"
        >
          Update
        </button>
        <button
          onClick={() => setMode('confirmDelete')}
          className="text-ios-caption text-red-400 active:opacity-60"
          aria-label={`delete preset ${preset.name}`}
        >
          Delete
        </button>
        <button
          onClick={() => setMode('idle')}
          className="text-label-tertiary active:opacity-60 px-0.5"
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
        className="shrink-0 rounded-full bg-accent-soft text-accent border border-accent-soft hover:bg-accent-soft/80 active:opacity-60 pl-3 pr-2 py-0.5 text-ios-caption font-medium"
      >
        {preset.name}
      </button>
      <button
        onClick={() => setMode('actions')}
        aria-label={`options for preset ${preset.name}`}
        title="Rename, update, or delete this view"
        className="shrink-0 rounded-full bg-accent-soft text-accent/70 border border-accent-soft hover:bg-accent-soft/80 active:opacity-60 px-2 py-0.5 text-ios-caption"
      >
        ⋯
      </button>
    </div>
  )
}

export function App() {
  const [entries, setEntries] = useState<Entry[]>([])
  const [forYou, setForYou] = useState<Entry[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  // Settings overlay state. Driven by the URL so the back button
  // and refresh both behave correctly. ``?view=settings`` opens it;
  // ``?view=settings&tab=feeds|llm|notifications|history|hidden|starred|reset``
  // picks the tab. ``null`` = closed.
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('feeds')

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
        // SettingsTab union: feeds | llm | notifications | hidden | reset.
        // The Settings overlay renders a tab strip across the top;
        // any value in the union is valid, anything else falls
        // through to the default ``feeds`` tab. Keeping the
        // explicit allow-list here (vs ``as SettingsTab``) makes
        // the URL surface area narrow and avoids a typo'd
        // ``?tab=feed`` (singular) silently rendering a blank
        // overlay with no tab active.
        if (
          t === 'feeds' ||
          t === 'llm' ||
          t === 'notifications' ||
          t === 'history' ||
          t === 'hidden' ||
          t === 'starred' ||
          t === 'reset'
        ) {
          setSettingsTab(t)
        }
      } else {
        setSettingsOpen(false)
      }
    }
    sync()
    window.addEventListener('popstate', sync)
    return () => window.removeEventListener('popstate', sync)
  }, [])

  const openSettings = (tab: SettingsTab = 'feeds') => {
    const u = new URL(window.location.href)
    u.searchParams.set('view', 'settings')
    u.searchParams.set('tab', tab)
    window.history.pushState(null, '', u.toString())
    setSettingsTab(tab)
    setSettingsOpen(true)
  }
  const closeSettings = () => {
    const u = new URL(window.location.href)
    u.searchParams.delete('view')
    u.searchParams.delete('tab')
    window.history.pushState(null, '', u.toString())
    setSettingsOpen(false)
  }
  const [mobileCol, setMobileCol] = useState<number>(loadMobileCol)
  const [error, setError] = useState<string | null>(null)
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [authProbed, setAuthProbed] = useState(false)
  const [oidcDisabled, setOidcDisabled] = useState(false)
  // Multi-source filter (empty set = no filter).
  const [activeSources, setActiveSources] = useState<Set<string>>(() => new Set())
  const [brief, setBrief] = useState<Brief | null>(null)
  const [generatingBrief, setGeneratingBrief] = useState(false)
  // Brief tone — lifted from BriefCard so the header Brief button,
  // the Drawer "Generate brief now" button, and the BriefCard pills
  // all stay in sync.
  const [briefTone, setBriefTone] = useState<'terse' | 'narrative' | 'alert'>('terse')
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
        const TIMEOUT_MS = 30_000
        const startedAt = Date.now()
        while (!ac.signal.aborted) {
          if (Date.now() - startedAt > TIMEOUT_MS) {
            onError?.('brief generation timed out')
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
            onError?.('brief job no longer tracked — please try again')
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
            onError?.(status.error ?? 'brief generation failed')
            return
          }
        }
      } catch (err) {
        onError?.((err as Error).message)
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
  // Per-user server-backed preferences. The localStorage that
  // used to back these has moved to ``lib/preferences.tsx``'s
  // provider; the setters here debounce the PUTs to the server.
  // On first mount the provider seeds from the old localStorage
  // values, so the migration is invisible to the user.
  const prefs = usePreferences()
  const readEntries = prefs.state.readEntries
  const lastViewed = prefs.state.lastViewed
  const columnPrefs = prefs.state.columnPrefs as Record<string, ColumnPrefs>
  const columnSections = prefs.state.columnSections as ColumnSections
  const hiddenEntries = prefs.state.hiddenEntries
  const starredEntries = prefs.state.starredEntries
  const filterPresets = prefs.state.filterPresets as FilterPreset[]
  const historyGroupBy = prefs.state.historyGroupBy
  // Stable references to the typed setters. Aliased locally so
  // the existing handlers can call ``setReadEntries(...)`` etc.
  // without renaming every call site. The provider's setters
  // are the only writers; the local ``useState`` versions are
  // gone (the in-memory state lives in the provider now).
  const setReadEntries: (
    columnId: string,
    ids: ReadEntriesValue,
  ) => void = prefs.setReadEntries
  const setLastViewed: (
    columnId: string,
    iso: string,
  ) => void = prefs.setLastViewed
  const clearLastViewed: (columnId: string) => void = prefs.clearLastViewed
  const setColumnPrefs: (
    columnId: string,
    prefs: ColumnPrefsValue,
  ) => void = prefs.setColumnPrefs
  const setColumnSections: (
    columnId: string,
    sections: ColumnSectionsValue,
  ) => void = prefs.setColumnSections
  const setHiddenEntries: (ids: number[]) => void = prefs.setHiddenEntries
  const setStarredEntries: (ids: number[]) => void = prefs.setStarredEntries
  const setFilterPresets: (presets: FilterPresetValue[]) => void =
    prefs.setFilterPresets
  // Note: no local ``setHistoryGroupBy`` alias — Settings.tsx's
  // History tab calls ``usePreferences()`` directly for that setter
  // rather than receiving it as a prop from here.
  // Mirror the 8 preference values into refs so handlers can
  // read the latest value without stale-closure bugs. Without
  // this, a handler that did
  //   ``setReadEntries(col, [...readEntries[col], id])``
  // would read the ``readEntries`` snapshot from the render
  // that captured the closure, not the value after a previous
  // handler's update. Two ``setX`` calls in the same tick
  // (e.g. ``markEntryRead`` and a follow-up
  // ``restoreHiddenEntry``) would lose the second write.
  // The pattern: a tiny useEffect keeps the ref in sync with
  // the state on every render; the handlers read the ref.
  const readEntriesRef = useRef(readEntries)
  const lastViewedRef = useRef(lastViewed)
  const columnPrefsRef = useRef(columnPrefs)
  const columnSectionsRef = useRef(columnSections)
  const hiddenEntriesRef = useRef(hiddenEntries)
  const starredEntriesRef = useRef(starredEntries)
  const filterPresetsRef = useRef(filterPresets)
  const historyGroupByRef = useRef(historyGroupBy)
  useEffect(() => {
    readEntriesRef.current = readEntries
  }, [readEntries])
  useEffect(() => {
    lastViewedRef.current = lastViewed
  }, [lastViewed])
  useEffect(() => {
    columnPrefsRef.current = columnPrefs
  }, [columnPrefs])
  useEffect(() => {
    columnSectionsRef.current = columnSections
  }, [columnSections])
  useEffect(() => {
    hiddenEntriesRef.current = hiddenEntries
  }, [hiddenEntries])
  useEffect(() => {
    starredEntriesRef.current = starredEntries
  }, [starredEntries])
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
  // Keyboard selection.
  const [selectedColumnIndex, setSelectedColumnIndex] = useState(0)
  const [selectedCardId, setSelectedCardId] = useState<number | null>(null)
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

  // Set of entry ids observed on the previous successful refresh.
  // Used to compute "new since last refresh" — entries whose id is
  // not in this set are flagged. ``null`` means "haven't completed a
  // refresh yet" — the new-entry indicator stays hidden until the
  // first refresh lands so the initial dashboard load doesn't flag
  // every entry as new.
  const seenEntryIdsRef = useRef<Set<number> | null>(null)
  // When the tab was last hidden. ``null`` while visible.
  const hiddenAtRef = useRef<number | null>(null)
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
  // Same key as ``sourcesById`` but the value is the source's
  // category. Passed down to Column → Card so each card can render
  // its category-colored left stripe. Optional because older source
  // rows (pre-Phase-5) have no category in the DB until they're
  // touched — we just skip the stripe for those.
  const categoriesBySourceId = useMemo(
    () => new Map(sources.map((s) => [s.id, s.category])),
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
      const sourceName = sourcesById.get(e.source_id)
      const src = sourceName != null ? sourcesByName.get(sourceName) : undefined
      const cat = src?.category ?? 'other'
      const arr = grouped.get(cat) ?? []
      arr.push(e)
      grouped.set(cat, arr)
    }
    return grouped
  }, [visibleEntries, sourcesByName, sourcesById])

  const categories = useMemo(() => Array.from(byCategory.keys()).sort(), [byCategory])
  const visibleForYou = useMemo(
    () => forYou.filter((e) => !hiddenSet.has(e.id)),
    [forYou, hiddenSet],
  )
  // Build a Set for O(1) starred-entry lookup. Mirrors the
  // ``hiddenSet`` pattern: cheap to construct on every render,
  // saves a linear pass on each downstream filter.
  const starredSet = useMemo(() => new Set(starredEntries), [starredEntries])
  // "Saved" column entries. The Saved column shows entries the
  // user has starred, in chronological order (most recent first).
  // Filter out hidden (a star + a hide is "I saved this but
  // don't want to see it now" — hide wins). The order is
  // chosen as published_at desc so the user sees their freshest
  // saves at the top.
  const visibleStarred = useMemo(
    () =>
      entries
        .filter((e) => starredSet.has(e.id) && !hiddenSet.has(e.id))
        .sort((a, b) => {
          // published_at is a string (ISO 8601) so string
          // comparison works for chronological order.
          if (!a.published_at) return 1
          if (!b.published_at) return -1
          return b.published_at.localeCompare(a.published_at)
        }),
    [entries, starredSet, hiddenSet],
  )

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

  // All-subs columns. For You is the first column on mobile so swiping
  // between tabs goes For You → news → tech → … The desktop grid
  // renders For You as a separate row above the category columns (see
  // the render block); on mobile the For You column *is* the For You
  // row, in a single-column swipe.
  const allSubsColumns = useMemo<Array<{ name: string; entries: Entry[] }>>(() => {
    const out: Array<{ name: string; entries: Entry[] }> = []
    // Saved column — the user's starred entries. Pinned at the
    // top of the dashboard so intentional saves are the first
    // thing they see on every load. Skipped when empty so a
    // user who hasn't starred anything doesn't see an empty
    // column wasting horizontal space.
    if (visibleStarred.length > 0) {
      out.push({ name: 'Saved', entries: visibleStarred })
    }
    if (visibleForYou.length > 0) out.push({ name: 'For You', entries: visibleForYou })
    for (const cat of categories) {
      out.push({ name: cat, entries: byCategory.get(cat) ?? [] })
    }
    return out
  }, [visibleForYou, categories, byCategory, visibleStarred])

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

  const multisubColumn = useMemo<Array<{ name: string; entries: Entry[] }>>(() => {
    if (activeSources.size === 0) {
      // Defensive: this branch shouldn't be reached because
      // ``viewKind`` switches to 'all' when ``activeSources``
      // is empty, but we keep the empty case explicit so
      // a stale memo doesn't accidentally render a
      // unfiltered column.
      return [{ name: multisubColumnName, entries: [] }]
    }
    const filtered = visibleEntries.filter((e) => {
      const name = sourcesById.get(e.source_id)
      return name != null && activeSources.has(name)
    })
    return [{ name: multisubColumnName, entries: filtered }]
  }, [visibleEntries, activeSources, sourcesById, multisubColumnName])

  const baseColumns = viewKind === 'multisub' ? multisubColumn : allSubsColumns

  // Apply per-column prefs. For You skips the sort filter (backend
  // pre-sorts by composite_score) but still respects min-score +
  // max-age.
  const columns = useMemo<Array<{ name: string; entries: Entry[]; totalCount: number }>>(() => {
    const now = Date.now()
    return baseColumns.map((col) => {
      const prefs = columnPrefs[col.name] ?? DEFAULT_PREFS
      let filtered = col.entries
      if (prefs.minScore > 0) {
        filtered = filtered.filter((e) => e.composite_score >= prefs.minScore)
      }
      if (prefs.maxAgeHours != null) {
        const cutoff = now - prefs.maxAgeHours * 60 * 60 * 1000
        filtered = filtered.filter((e) => {
          if (!e.fetched_at) return true
          return new Date(e.fetched_at).getTime() >= cutoff
        })
      }
      if (col.name !== 'For You' && prefs.sort !== 'top') {
        filtered = [...filtered].sort((a, b) => {
          const ta = a.published_at ? new Date(a.published_at).getTime() : 0
          const tb = b.published_at ? new Date(b.published_at).getTime() : 0
          return prefs.sort === 'newest' ? tb - ta : ta - tb
        })
      }
      return { name: col.name, entries: filtered, totalCount: col.entries.length }
    })
  }, [baseColumns, columnPrefs])

  // Read state is stored per-column (``readEntries[columnName]``)
  // because "mark all read" and its Undo are column-scoped
  // actions, but the same entry can appear in several columns at
  // once (For You, its category column, a multi-source filter).
  // "Read" is a property of the entry, not of any one column it
  // happens to appear in — so membership here is the UNION of
  // every column's manual read-set, not just this column's own.
  // Without this, marking (or hiding, which also marks read) an
  // entry from For You left it looking unread in its category
  // column, and vice versa.
  const globalReadIds = useMemo(() => {
    const out = new Set<number>()
    for (const ids of Object.values(readEntries)) {
      for (const id of ids) out.add(id)
    }
    return out
  }, [readEntries])

  // New-entry per column. An entry is "new" when it hasn't already
  // been marked read (from ANY view — see ``globalReadIds`` above)
  // AND either its id isn't in the seen-set from the previous
  // refresh, or it's newer than the user's last visit to this
  // column.
  //
  // The read-state exclusion matters because an entry frequently
  // shows up in more than one place (its category column AND the
  // For You row, say). Without it, reading something from For You
  // didn't stop its category column's "+N new" chip from counting
  // it — the chip would claim N new items, but expanding the
  // column showed mostly already-read (History) entries, because
  // the chip and the Fresh/History split were reading two
  // completely different signals (fetch/visit timing vs. manual
  // read state).
  const newCountByColumn = useMemo(() => {
    const out = new Map<string, number>()
    const seen = seenEntryIdsRef.current
    for (const col of columns) {
      const last = lastViewed[col.name]
      const lastMs = last ? new Date(last).getTime() : 0
      let n = 0
      for (const e of col.entries) {
        if (globalReadIds.has(e.id)) continue
        const isNewSinceRefresh = seen != null && !seen.has(e.id)
        const isNewSinceVisit =
          lastMs > 0 && e.fetched_at != null && new Date(e.fetched_at).getTime() > lastMs
        if (isNewSinceRefresh || isNewSinceVisit) n++
      }
      if (n > 0) out.set(col.name, n)
    }
    return out
  }, [columns, lastViewed, globalReadIds])

  // Fresh/History split per column. Fresh = the user has not
  // manually marked this entry read. History = the user has.
  // Independent of ``lastViewed`` so the column always has
  // content — a first-time visit (no ``lastViewed`` set)
  // still shows all entries as Fresh, and a "mark all read"
  // move (which sets ``lastViewed``) doesn't make the column
  // body empty. The ``lastViewed`` is still used for the
  // "N new" chip on the column header, computed separately
  // in ``newCountByColumn``. ``globalReadIds`` is defined above,
  // next to ``newCountByColumn``, since both now depend on it.
  const unreadIdsByColumn = useMemo(() => {
    const out = new Map<string, Set<number>>()
    for (const col of columns) {
      const ids = new Set<number>()
      for (const e of col.entries) {
        if (!globalReadIds.has(e.id)) ids.add(e.id)
      }
      if (ids.size > 0) out.set(col.name, ids)
    }
    return out
  }, [columns, globalReadIds])

  // New / History split per column. The split happens AFTER the
  // per-column sort/filter (the previous useMemo), so the per-
  // column ``sort: 'top' | 'newest' | 'oldest'`` preference still
  // gates which entries appear — and within each section we always
  // sort newest-first (the user's "newest in each section" choice
  // supersedes the per-column ``sort`` for the visual order).
  //
  // Membership rule (matches ``unreadIdsByColumn``): an entry is
  // "new" if it isn't in the manual ``readEntries`` set AND its
  // ``fetched_at`` is after the column's ``lastViewed`` timestamp.
  // Everything else is "history". The Card's ``unread`` prop is
  // derived from section membership in Column, so we don't need
  // to pass the Set down anymore.
  const sectionsByColumn = useMemo(() => {
    // Reused comparator. ``published_at`` is the user-facing
    // "when was this posted" — what readers want. ``fetched_at``
    // is the fallback for entries whose publisher didn't include
    // a clean published timestamp (some RSS feeds). ``id`` is the
    // final tiebreaker so equal-timestamp entries have a stable
    // order across renders.
    const byNewest = (a: Entry, b: Entry): number => {
      const ta = a.published_at
        ? new Date(a.published_at).getTime()
        : a.fetched_at
          ? new Date(a.fetched_at).getTime()
          : 0
      const tb = b.published_at
        ? new Date(b.published_at).getTime()
        : b.fetched_at
          ? new Date(b.fetched_at).getTime()
          : 0
      if (tb !== ta) return tb - ta
      return b.id - a.id
    }
    const out = new Map<string, { new: Entry[]; history: Entry[] }>()
    for (const col of columns) {
      const unread = unreadIdsByColumn.get(col.name) ?? new Set<number>()
      const fresh: Entry[] = []
      const past: Entry[] = []
      for (const e of col.entries) {
        if (unread.has(e.id)) fresh.push(e)
        else past.push(e)
      }
      // Sort each slice. ``[...arr].sort`` so we don't mutate the
      // upstream ``columns[].entries`` array (which would also
      // affect ``unreadIdsByColumn``'s iteration order on the
      // next render).
      fresh.sort(byNewest)
      past.sort(byNewest)
      out.set(col.name, { new: fresh, history: past })
    }
    return out
  }, [columns, unreadIdsByColumn])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const sourceArg = activeSources.size > 0 ? Array.from(activeSources) : undefined
      // A flat global `limit` on the unfiltered "give me everything"
      // fetch lets one high-volume or high-scoring category (Hacker
      // News' 5-minute refresh, say) crowd a slower/lower-scoring one
      // out of the response entirely once the table has enough rows
      // — the category still shows the "+N new" chip (it's fed by
      // whatever landed) but the column itself renders almost
      // nothing, because its actual rows never made the cut. The
      // multi-source filter view doesn't have this problem (it's
      // already scoped to a handful of sources), so only the
      // unfiltered path uses ``perCategoryLimit`` instead of ``limit``.
      const entriesReq = sourceArg
        ? api.entries({ limit: 200, source: sourceArg })
        : api.entries({ perCategoryLimit: 100 })
      // Hot path: entries + sources + for-you. These change with
      // every ingest and the user expects them to be near-real-time
      // when the dashboard is open.
      const [e, s, fy] = await Promise.all([
        entriesReq,
        api.sources(),
        api.forYou({ limit: 25 }).catch(() => [] as Entry[]),
      ])
      const prevSeen = seenEntryIdsRef.current
      const newSeen = new Set(e.map((x) => x.id))
      if (
        hiddenAtRef.current != null &&
        Date.now() - hiddenAtRef.current > HIDDEN_RESET_MS
      ) {
        // Long absence → reset so everything surfaces as new.
        seenEntryIdsRef.current = new Set()
      } else if (prevSeen == null) {
        // First refresh — don't flag every entry.
        seenEntryIdsRef.current = newSeen
      } else {
        const merged = new Set(prevSeen)
        for (const id of newSeen) merged.add(id)
        seenEntryIdsRef.current = merged
      }
      hiddenAtRef.current = null
      setEntries(e)
      setSources(s)
      setForYou(fy)
      setError(null)
      // Surface a brief success toast so the user knows the
      // pull actually happened — the Refresh button's …→
      // normal transition is otherwise invisible if the request
      // was sub-second. The count is the most useful signal:
      // "0 new" tells the user nothing landed, which is honest
      // and saves them a second click.
      const newCount = prevSeen == null
        ? 0
        : e.reduce((acc, row) => (prevSeen.has(row.id) ? acc : acc + 1), 0)
      toast(newCount > 0 ? `${newCount} new since last refresh` : 'refreshed', 'info')
    } catch (err) {
      setError((err as Error).message)
      toast((err as Error).message, 'error')
    } finally {
      setRefreshing(false)
    }
  }, [activeSources])

  // Cold path: health pings (DB / Redis / sources count). Pulled on
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

  // Wipe every namespaced localStorage key and reload. Wired
  // through to the Drawer's "Clear local state" button. A full
  // page reload (rather than local state reset + re-render)
  // because every component has its own state mirrors; rebuilding
  // them inline is more code than ``location.reload()`` and
  // prone to one-component-behind-another race conditions.
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
    // Server-backed preferences need an explicit reset too --
    // the localStorage wipe above only affects what's still
    // on this device. We DELETE every per-user preference
    // key, then reload. The next mount's GET will see an
    // empty server and the provider initializes to defaults.
    // The eight singleton + per-column keys cover every
    // preference the provider exposes. ``Promise.allSettled``
    // so a single failure doesn't block the rest of the
    // reset (the reload happens regardless).
    const allKeys: string[] = [
      PREFERENCE_KEYS.hiddenEntries,
      PREFERENCE_KEYS.starredEntries,
      PREFERENCE_KEYS.filterPresets,
      PREFERENCE_KEYS.historyGroupBy,
    ]
    // Per-column keys: the keys we know about from the
    // current state plus the four per-column kinds.
    const perColumnKinds = [
      PREFERENCE_KEYS.readEntries,
      PREFERENCE_KEYS.lastViewed,
      PREFERENCE_KEYS.columnPrefs,
      PREFERENCE_KEYS.columnSections,
    ]
    const columnIds = new Set<string>()
    for (const k of perColumnKinds) {
      const m = prefs.state
      if (k === PREFERENCE_KEYS.readEntries) {
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
    void Promise.allSettled(
      allKeys.map((k) => api.deletePreference(k)),
    ).finally(() => {
      window.location.reload()
    })
  }, [prefs.state])

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
        void refresh()
      }, REFRESH_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (intervalId != null) {
        window.clearInterval(intervalId)
        intervalId = null
      }
    }

    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        hiddenAtRef.current = Date.now()
        stopPolling()
      } else {
        void refresh()
        startPolling()
      }
    }

    document.addEventListener('visibilitychange', onVisibility)
    if (!pollingMountedRef.current) {
      pollingMountedRef.current = true
      void refresh()
    }
    if (document.visibilityState === 'visible') startPolling()

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      stopPolling()
    }
  }, [refresh, authProbed])

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
      void refresh()
    }, 250)
    return () => window.clearTimeout(id)
  }, [activeSources, refresh, authProbed])

  // Keep ``mobileCol`` in bounds AND persist to localStorage. This
  // effect is the only writer to ``STORAGE_KEYS.mobileColLast``: a
  // previous version had a separate write effect that fired every
  // time ``mobileCol`` changed, but React runs effects in source
  // order within a single commit — so when a stored value came back
  // out-of-bounds, the clamp's ``setMobileCol(next)`` would queue a
  // re-render and the write effect would immediately stamp the OLD
  // (bad) value back to storage before the queue settled. Merging
  // clamp + write into a single effect with both deps in the array
  // gives us "validate, then persist" in the right order.
  useEffect(() => {
    if (columns.length === 0) return
    if (mobileCol >= columns.length) {
      const next = columns.length - 1
      setMobileCol(next)
      safeSetItem(STORAGE_KEYS.mobileColLast, String(next))
    } else {
      safeSetItem(STORAGE_KEYS.mobileColLast, String(mobileCol))
    }
  }, [columns.length, mobileCol])

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
        // Surface the error verbatim — the user can tell the
        // difference between "no matches" (results=[], error=null)
        // and "the request failed" (results=[], error="…").
        setSearchError(err?.message ?? 'search failed')
      })
      .finally(() => {
        if (!cancelled) setSearching(false)
      })
    return () => {
      cancelled = true
    }
  }, [searchQuery])

  // Per-card mark-read. Hoisted above the keyboard effect so the
  // ``m`` shortcut (defined further down) can reference it without
  // tripping TypeScript's "used before declaration" check. Adds the
  // entry id to the column's manual read-set; ``unreadIdsByColumn``
  // uses the set to exclude that entry on the next render, dimming
  // its ring without touching the column-wide ``lastViewed``
  // timestamp. Idempotent — re-marking a read card is a no-op so the
  // keyboard ``m`` shortcut stays side-effect free when pressed on
  // a card the user has already acknowledged.
  //
  // Recording the engagement event (``view``) is the caller's job
  // — the keyboard shortcut and the ✓ button each fire
  // ``recordImmediate`` themselves so the ranker sees both paths.
  // This function is a pure UI flip.
  //
  // ``useCallback`` so the keyboard-shortcut effect doesn't re-bind
  // on every render. ``setReadEntries`` is stable across renders.


  // Reverse of ``markColumnRead``: remove a set
  // of ids from ``readEntries[col]`` and clear
  // ``lastViewed[col]``. Used by the column-mark-read
  // Undo action. The cleared lastViewed means the
  // "N new" chip will repopulate on the next
  // refresh.
  const unmarkColumnRead = useCallback(
    (columnName: string, ids: number[]) => {
      if (ids.length === 0) return
      const idSet = new Set(ids)
      // Read latest from the ref -- not the closed-over
      // ``readEntries`` -- so a previous unmark in the same
      // tick (e.g. an Undo chain) is reflected here.
      const cur = readEntriesRef.current[columnName] ?? []
      const next = cur.filter((id) => !idSet.has(id))
      if (next.length !== cur.length) {
        setReadEntries(columnName, next)
      }
      // Clear lastViewed for this column so the "N new" chip
      // repopulates on the next refresh. The provider issues
      // a DELETE on the server (not a sentinel PUT) so the
      // row is actually gone.
      if (columnName in lastViewedRef.current) {
        clearLastViewed(columnName)
      }
      if (seenEntryIdsRef.current != null) {
        const merged = new Set(seenEntryIdsRef.current)
        for (const id of ids) merged.delete(id)
        seenEntryIdsRef.current = merged
      }
    },
    [setReadEntries, clearLastViewed],
  )

  // Reverse of ``markEntryRead``: remove the id
  // from ``readEntries[col]`` so the entry moves
  // back to the Fresh section. Used by the
  // mark-read Undo action on the per-card check
  // click.
  const unmarkEntryRead = useCallback(
    (columnName: string, entryId: number) => {
      const cur = readEntriesRef.current[columnName] ?? []
      const next = cur.filter((id) => id !== entryId)
      if (next.length !== cur.length) {
        setReadEntries(columnName, next)
      }
    },
    [setReadEntries],
  )

  // Reverse of ``hideEntry`` / ``toggleHideEntry``
  // (hide branch): remove the id from
  // ``hiddenEntries`` so the entry surfaces in
  // the dashboard again. Used by the hide Undo
  // action on both the per-card eye button and
  // the context-menu "Hide this entry" item.
  // Note: this does NOT reverse the
  // ``markEntryRead`` that ``toggleHideEntry``
  // also fires. The user has to tap check Undo
  // separately to restore the Fresh/History
  // split. This matches the prior
  // one-step-at-a-time model.
  const restoreHiddenEntry = useCallback((entryId: number) => {
    const next = hiddenEntriesRef.current.filter((id) => id !== entryId)
    if (next.length !== hiddenEntriesRef.current.length) {
      setHiddenEntries(next)
    }
  }, [setHiddenEntries])

  const markEntryRead = useCallback((columnName: string, entryId: number) => {
    const cur = readEntriesRef.current[columnName] ?? []
    if (cur.includes(entryId)) return
    // Trim on write so the list doesn't grow unboundedly
    // across many mark-reads. ``slice(-MAX_PER_COLUMN)``
    // keeps the newest reads.
    const next = [...cur, entryId].slice(-MAX_PER_COLUMN)
    setReadEntries(columnName, next)
  }, [setReadEntries])

  // Hide an entry from every column + the For You row. The
  // entry stays in the DB; it just gets filtered out of the
  // dashboard until the user clears the hidden set (a future
  // "show hidden" affordance in Settings). Records a ``never``
  // engagement event so the ranker can also learn the user's
  // dismissal signal — same pattern as ``markEntryRead``'s
  // ``view`` event for the "I read this" signal.
  const hideEntry = useCallback((entryId: number) => {
    const cur = hiddenEntriesRef.current
    if (cur.includes(entryId)) return
    const next = [...cur, entryId].slice(-MAX_HIDDEN)
    setHiddenEntries(next)
  }, [setHiddenEntries])

  // Toggle the eye-button hidden state. When transitioning
  // from visible -> hidden, ALSO mark the entry read so it
  // moves to the column's History section. The user can
  // still scroll the column to find it; the eye button
  // effectively combines "I read this" + "I never want to
  // see it in the For You row again".
  //
  // The context menu's "Hide this entry" item uses the
  // bare ``hideEntry`` (above) and is the stronger
  // "permanently dismiss" affordance — the entry
  // disappears from every column, surfaces only in
  // Settings/Hidden.
  //
  // The column lookup here uses the currently-selected
  // column's lastViewed for the mark-read. The user's
  // exact current column is best-effort (we use
  // selectedColumnIndex or 0) because the mark-read flip
  // is per-column, not per-entry.
  const toggleHideEntry = useCallback(
    (columnName: string, entryId: number) => {
      const isHidden = hiddenSet.has(entryId)
      if (isHidden) {
        // Unhide: just remove from the hidden set. Don't
        // touch readEntries — the entry stays in its
        // current section (typically History because we
        // marked it read on the original hide).
        const next = hiddenEntriesRef.current.filter((id) => id !== entryId)
        if (next.length !== hiddenEntriesRef.current.length) {
          setHiddenEntries(next)
        }
        toast('Entry unhidden.', 'info')  // no Undo: the act of un-hiding IS the undo
      } else {
        // Hide: add to hidden set AND mark read so the
        // entry moves to the column's History section.
        const cur = hiddenEntriesRef.current
        if (!cur.includes(entryId)) {
          setHiddenEntries([...cur, entryId].slice(-MAX_HIDDEN))
        }
        markEntryRead(columnName, entryId)
        toast('Entry moved to history.', {
          kind: 'info',
          action: {
            label: 'Undo',
            // Undo the WHOLE action, not just the hidden-set half —
            // this used to only call restoreHiddenEntry, which
            // unhid the entry but left it marked read, so it stayed
            // sitting in History instead of returning to Fresh. A
            // user hitting Undo on "Entry moved to history" expects
            // the entry back where it was, not half-moved.
            onClick: () => {
              restoreHiddenEntry(entryId)
              unmarkEntryRead(columnName, entryId)
            },
          },
        })
      }
    },
    [hiddenSet, markEntryRead, restoreHiddenEntry, unmarkEntryRead, setHiddenEntries],
  )

  // Star / unstar an entry. Stars are a long-term save-for-later
  // set: starred items surface in a dedicated "Saved" column at
  // the top of the dashboard, in the For You row, and in the
  // Settings/Starred tab. Records a ``bookmark`` engagement event
  // so the ranker can use stars as a positive signal (the
  // opposite of ``hide``'s ``never``).
  const toggleStarEntry = useCallback((entryId: number) => {
    const cur = starredEntriesRef.current
    if (cur.includes(entryId)) {
      // Unstar. Filter out, don't trim — the user's removal
      // is intentional and we don't want a subsequent star to
      // bring it back.
      setStarredEntries(cur.filter((id) => id !== entryId))
    } else {
      setStarredEntries([...cur, entryId].slice(-MAX_STARRED))
    }
  }, [setStarredEntries])

  // Apply a saved preset: replace the active sources filter and
  // per-column prefs with the preset's values. A no-op when the
  // preset's data is empty (matches "all sources" / "default
  // prefs" semantically).
  //
  // ``columnPrefs`` is a per-column map keyed by source id; the
  // provider's setter is per-column, so we apply each one. (The
  // number of columns a preset touches is small -- typically
  // 1-5 -- so the per-column PUTs are a non-issue.)
  const applyPreset = useCallback((preset: FilterPreset) => {
    setActiveSources(new Set(preset.activeSources))
    // ``preset.columnPrefs`` round-trips through JSON; the
    // object is structurally a ``ColumnPrefs`` but the
    // server can't enforce that. Cast through ``unknown``
    // so the structural shape lands in
    // ``ColumnPrefsValue`` without an extra validator
    // (the values are still validated by ``applyPreset``'s
    // own consumer -- the Column component's prefs popover
    // -- and a malformed value just becomes a no-op).
    for (const [columnId, prefs] of Object.entries(preset.columnPrefs)) {
      setColumnPrefs(columnId, prefs as unknown as ColumnPrefsValue)
    }
  }, [setColumnPrefs])

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
  // mark-read — expanding a card doesn't mark it, and marking a
  // card doesn't collapse its summary. ``useCallback`` so the
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
        if (ae instanceof HTMLInputElement && ae.type === 'search' && e.key === 'Escape') {
          ae.blur()
          setSearchInput('')
        }
        return
      }

      if (e.key === '/' && columns.length > 0) {
        e.preventDefault()
        document.getElementById('app-search')?.focus()
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

      if (columns.length === 0) return

      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        setSelectedColumnIndex((i) => Math.max(0, i - 1))
        setSelectedCardId(null)
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        setSelectedColumnIndex((i) => Math.min(columns.length - 1, i + 1))
        setSelectedCardId(null)
      } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const col = columns[selectedColumnIndex]
        if (!col || col.entries.length === 0) return
        const idx = col.entries.findIndex((e2) => e2.id === selectedCardId)
        const next =
          e.key === 'ArrowDown'
            ? Math.min(col.entries.length - 1, idx + 1)
            : Math.max(0, idx - 1)
        setSelectedCardId(col.entries[next]?.id ?? null)
      } else if (e.key === 'Enter' && selectedCardId != null) {
        e.preventDefault()
        const card = cardRefs.current.get(selectedCardId)
        const link = card?.querySelector('a')
        link?.click()
      } else if (e.key === 'm' && selectedCardId != null) {
        // Mark the selected card read. Mirrors the ✓ button on the
        // card so keyboard users get the same affordance. Modifier
        // guards below — Cmd+M minimizes the window on macOS, so we
        // only fire on bare ``m``.
        if (e.metaKey || e.ctrlKey || e.altKey) return
        e.preventDefault()
        const col = columns[selectedColumnIndex]
        if (!col) return
        markEntryRead(col.name, selectedCardId)
        recordImmediate({ entry_id: selectedCardId, type: 'view' })
      } else if (e.key === 's' && selectedCardId != null) {
        // Toggle inline summary on the selected card. Mirrors the
        // chevron button. Same modifier guards as ``m`` so Cmd+S
        // (Save Page As) still works when the dashboard has focus.
        if (e.metaKey || e.ctrlKey || e.altKey) return
        e.preventDefault()
        toggleSummary(selectedCardId)
      } else if (e.key === 'b' && selectedCardId != null) {
        // Toggle star (save-for-later) on the selected card.
        // ``b`` for bookmark is the standard iOS / Gmail
        // affordance; doesn't conflict with browser defaults
        // (Cmd+B is bold-text in form fields but does nothing
        // on the dashboard surface). Same modifier guards as
        // ``s`` so Cmd+B still works when the dashboard has
        // focus and a form field is active.
        if (e.metaKey || e.ctrlKey || e.altKey) return
        e.preventDefault()
        const wasStarred = starredSet.has(selectedCardId)
        // Mirrors the star button's onClick (Card.tsx) — the ranker
        // needs this event regardless of input path, or the
        // preference vector never learns from keyboard-driven saves.
        recordImmediate({ entry_id: selectedCardId, type: 'bookmark' })
        toggleStarEntry(selectedCardId)
        toast(
                          wasStarred
                            ? 'Removed from Saved.'
                            : 'Saved for later — see the Saved column.',
                          {
                            kind: 'info',
                            action: {
                              label: 'Undo',
                              onClick: () => toggleStarEntry(selectedCardId),
                            },
                          },
                        )
      } else if (e.key === 'h' && selectedCardId != null) {
        // Toggle the eye button (hide) on the selected card.
        // ``h`` for hide is the natural mirror of ``m``
        // (mark read) and ``b`` (bookmark). It conflicts
        // with browser-level shortcuts less often than
        // ``e`` (which is "find previous" in some
        // browsers). Same modifier guards as the other
        // single-key shortcuts so Cmd+H (Hide App on
        // macOS) still works when the dashboard has
        // focus.
        if (e.metaKey || e.ctrlKey || e.altKey) return
        e.preventDefault()
        const col = columns[selectedColumnIndex]
        if (!col) return
        // Mirrors the eye button's onClick (Card.tsx): record the
        // 'never' signal only on the hide direction (not unhide),
        // same guard the button uses. Without this the preference
        // vector never sees keyboard-driven hides — a card hidden
        // with 'h' looked identical to one never touched, so the
        // ranker kept recommending the same kind of thing.
        if (!hiddenSet.has(selectedCardId)) {
          recordImmediate({ entry_id: selectedCardId, type: 'never' })
        }
        toggleHideEntry(col.name, selectedCardId)
      } else if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        // Refresh the dashboard. Bare ``r``; Cmd+R / Ctrl+R
        // already triggers the browser reload so we don't fight
        // it. Same affordance as the header Refresh button.
        e.preventDefault()
        void refresh()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [
    columns,
    selectedColumnIndex,
    selectedCardId,
    searchQuery,
    markEntryRead,
    toggleSummary,
    refresh,
    toggleHideEntry,
    hiddenSet,
    starredSet,
    toggleStarEntry,
  ])

  // Scroll the keyboard-selected card into view.
  useEffect(() => {
    if (selectedCardId == null) return
    const el = cardRefs.current.get(selectedCardId)
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [selectedCardId])

  const markColumnRead = (columnName: string) => {
    // Three updates in one render cycle:
    //   1. lastViewed[columnName] = now  (chips to 0)
    //   2. readEntries[columnName] += all current entry ids
    //      (moves every entry from Fresh to History)
    //   3. seenEntryIdsRef adds them  (next-refresh delta
    //      doesn't re-flag them as "new since refresh")
    //
    // We read from the refs (not the closed-over state) so
    // two markColumnRead calls in the same tick don't lose
    // each other's writes. The debounced sync on the
    // provider coalesces the resulting server PUTs.
    const col = columns.find((c) => c.name === columnName)
    setLastViewed(columnName, new Date().toISOString())
    const addedIds: number[] = []
    if (col) {
      const cur = readEntriesRef.current[columnName] ?? []
      // Add only ids not already in the set. The
      // dedup happens in O(n + m) where n is the
      // column's entry count and m is the existing
      // set size.
      const curSet = new Set(cur)
      const toAdd: number[] = []
      for (const e of col.entries) {
        if (!curSet.has(e.id)) toAdd.push(e.id)
      }
      if (toAdd.length > 0) {
        addedIds.push(...toAdd)
        // ``MAX_PER_COLUMN`` cap mirrors the old
        // localStorage version.
        setReadEntries(columnName, [...cur, ...toAdd].slice(-MAX_PER_COLUMN))
      }
      if (seenEntryIdsRef.current != null) {
        const merged = new Set(seenEntryIdsRef.current)
        for (const e of col.entries) merged.add(e.id)
        seenEntryIdsRef.current = merged
      }
    }
    // Surface the action as an Undo-able toast.
    // The user can tap Undo to restore the column
    // to its pre-mark state. We capture
    // ``addedIds`` by reference (mutated by the
    // setReadEntries updater) and the column name;
    // the Undo handler unmarks them in lockstep.
    if (addedIds.length > 0) {
      toast(`Marked ${addedIds.length} ${addedIds.length === 1 ? 'entry' : 'entries'} as read`, {
        kind: 'info',
        action: {
          label: 'Undo',
          onClick: () => {
            unmarkColumnRead(columnName, addedIds)
            toast('Column restored.', 'info')
          },
        },
      })
    }
  }

  const toggleSource = (name: string) => {
    setActiveSources((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }
  // When a user renames a source via the FeedManager inline editor,
  // remap any active filter chip in the same render cycle so the
  // chip bar doesn't briefly lose the source between PATCH and the
  // next ``refresh()`` resolving. A no-op if the renamed source
  // wasn't in the active set.
  const onSourceRenamed = (oldName: string, newName: string) => {
    if (oldName === newName) return
    setActiveSources((prev) => {
      if (!prev.has(oldName)) return prev
      const next = new Set(prev)
      next.delete(oldName)
      next.add(newName)
      return next
    })
  }
  // Same as ``toggleSource`` but also closes the drawer the first time
  // the user adds a source to an empty filter. The "empty → first tap"
  // transition is the confusing one — the user picked a source, the
  // filter engaged, but the panel stayed open covering the now-filtered
  // dashboard. Auto-closing lets them see the result immediately.
  // Subsequent taps toggle in place; they don't close again (avoids
  // the drawer ping-ponging shut when the user wants to pick two or
  // three sources in a row). The "sizeBefore === 0" check runs on the
  // current state, captured before ``toggleSource`` queues its update.
  const toggleSourceAndMaybeClose = (name: string) => {
    const wasEmpty = activeSources.size === 0
    toggleSource(name)
    if (wasEmpty) setDrawerOpen(false)
  }
  const clearSourceFilters = () => {
    setActiveSources(new Set())
  }

  const jumpToCategory = (category: string) => {
    const idx = columns.findIndex((c) => c.name === category)
    if (idx >= 0) {
      setSelectedColumnIndex(idx)
      setSelectedCardId(null)
      const el = columnRefs.current.get(category)
      if (el) el.scrollIntoView({ block: 'start', behavior: 'smooth' })
    }
  }

  const setPrefsFor = (columnName: string, prefs: ColumnPrefs) => {
    setColumnPrefs(columnName, prefs as ColumnPrefsValue)
  }

  // Toggle a single section's collapsed bit for a column. Lazily
  // creates the column's entry on first toggle so the persisted
  // record stays small (columns the user has never collapsed
  // don't take up a key at all). The default is "expanded" — we
  // only ever write when the user explicitly flips the bit, so
  // the next mount picks up the right state.
  //
  // The server-backed store has the same "drop the key when
  // both bits are false" behavior. The provider's
  // ``setColumnSections`` replaces the per-column value, so
  // we just compute the next value (or the empty marker) and
  // hand it off.
  const setColumnSection = (columnName: string, key: 'new' | 'history') => {
    const cur = columnSectionsRef.current[columnName] ?? { newCollapsed: false, historyCollapsed: false }
    const flag = key === 'new' ? 'newCollapsed' : 'historyCollapsed'
    const next: SectionCollapse = { ...cur, [flag]: !cur[flag] }
    if (!next.newCollapsed && !next.historyCollapsed) {
      // Both bits are back to false. Persist an empty
      // object so the loader treats the column as
      // "both expanded" (it ignores extra keys but
      // bails on missing ones).
      setColumnSections(columnName, {} as ColumnSectionsValue)
    } else {
      setColumnSections(columnName, next as ColumnSectionsValue)
    }
  }

  // Defaulting lookup for the per-column collapse state. Missing
  // column names (or missing fields) silently fall through to
  // "both expanded" — matches the loader's contract.
  const sectionsCollapsedFor = (columnName: string): { new: boolean; history: boolean } => {
    const cur = columnSections[columnName]
    return {
      new: cur?.newCollapsed ?? false,
      history: cur?.historyCollapsed ?? false,
    }
  }

  // The outer div lets us register a per-column ref on a wrapper
  // without breaking the grid layout — the wrapper uses
  // ``display: contents`` so its child (Column) participates in
  // the grid as if it weren't wrapped.
  const setColumnRef = (name: string) => (el: HTMLElement | null) => {
    if (el) columnRefs.current.set(name, el)
    else columnRefs.current.delete(name)
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
    return <LoginPage returnTo="/" onSignedIn={setUser} />
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
      // destination side. ``minHeight: '100dvh'`` is the dynamic-
      // viewport fallback for iOS Safari, where ``100vh`` is the
      // *layout* viewport (height *including* the area hidden by
      // the address bar / bottom toolbar) and overshoots — the
      // dashboard scrolls past where the user expects the bottom
      // to be. ``100dvh`` is the small-viewport height that
      // adjusts as the bars collapse, matching what the user sees.
      // ``100vh`` is kept alongside as the fallback for browsers
      // that don't understand ``dvh`` (older Android WebView).
      style={{ backgroundColor: '#000000', minHeight: '100dvh' }}
      className="h-full flex flex-col"
    >
      {/* Large-title navigation bar. Mirrors Apple Mail / Music:
          the title is large (34pt, iOS ``largeTitle`` weight) when the
          bar is at the top of the scroll, and the row beneath it
          carries the action buttons (hamburger, search, refresh,
          brief). A hairline divider sits at the bottom — the entire
          UI leans on these 1px ``rgba(255,255,255,0.08)`` lines as
          the only border treatment. ``z-20`` keeps the bar above
          column content but below the Drawer (z-30+). */}
      <header className="sticky top-0 z-20 bg-bg-app/85 supports-[backdrop-filter]:backdrop-blur-xl border-b border-hairline">
        {/* Title row. The hamburger sits on the trailing edge of the
            row (right-aligned) rather than the leading edge — this
            matches iOS where leading space is reserved for the title
            and the trailing chrome owns the actions. The large title
            is left-aligned at full width on its own row so it lands
            with the iOS-weight feel. On ``sm+`` the row also includes
            the search affordance and trailing health/brief buttons;
            on mobile the trailing controls are stripped (the icons
            only would crowd the small viewport). */}
        <div className="flex items-end justify-between px-4 pt-3 pb-2 sm:pt-4 sm:pb-3">
          <h1 className="text-ios-large-title text-label-primary truncate">
            Popping
          </h1>
          <div className="flex items-center gap-1 sm:gap-2 pb-1">
            {/* Search affordance. Collapsed: a 44×44 button with a
                magnifying-glass icon. Expanded: full-width input with
                a leading glass + trailing clear-X. The transition is
                a plain width swap — no JS animation library; the
                backdrop-blur above keeps the swap from feeling cheap. */}
            {searchOpen ? (
              <div className="flex items-center gap-2 bg-bg-elevated rounded-ios px-3 h-11 w-44 sm:w-72 animate-fade-in">
                <SearchIcon className="w-4 h-4 text-label-secondary shrink-0" />
                <input
                  id="app-search"
                  type="search"
                  inputMode="search"
                  autoFocus
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  onBlur={() => {
                    // Collapse when the user blurs only if the
                    // field is empty. Otherwise keep the input open
                    // so a stray tap outside doesn't lose the query.
                    if (!searchInput) setSearchOpen(false)
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
                    className="shrink-0 text-label-secondary active:text-label-primary"
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
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={refreshing}
              aria-label="refresh"
              className="sm:hidden w-11 h-11 flex items-center justify-center rounded-full text-label-primary active:bg-bg-elevated disabled:opacity-40"
            >
              <RefreshIcon className={`w-5 h-5 ${refreshing ? 'animate-spin' : ''}`} />
            </button>

{/* Settings gear. Opens the Settings overlay (a
            full-page sheet with tabs for feeds / LLM /
            notifications / reset). Lives to the LEFT of the
            hamburger so the hamburger keeps its right-edge
            position; the two together read as "more menu
            options → main menu." */}
          <button
            type="button"
            onClick={() => openSettings('feeds')}
            aria-label="open settings"
              data-settings-gear
              className="w-11 h-11 flex items-center justify-center rounded-full text-label-primary active:bg-bg-elevated"
            >
              <svg
                width="22"
                height="22"
                viewBox="0 0 22 22"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.5}
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="3" />
                <path d="M11 2v2M11 18v2M2 11h2M18 11h2M4.93 4.93l1.41 1.41M15.66 15.66l1.41 1.41M4.93 17.07l1.41-1.41M15.66 6.34l1.41-1.41" />
              </svg>
            </button>
            {/* Hamburger lives next to the search/refresh affordances
                — each is a 44×44 tappable target that matches iOS
                nav-bar icon-button conventions. */}
            <Hamburger onClick={() => setDrawerOpen(true)} />
          </div>
        </div>
        {/* Sub-row: health status + Refresh + Brief. Hidden on mobile
            because the actions duplicate what the Drawer already
            offers — opening the Drawer takes one tap. The row is
            laid out as a single flex line so the trailing buttons
            stay aligned with the title row's right edge. */}
        <div className="hidden sm:flex items-center justify-between px-4 pb-2 -mt-1">
          <span className="text-ios-caption text-label-secondary truncate">
            {health
              ? `${health.entries} entries · ${health.sources} sources · ${health.status}`
              : 'connecting…'}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => void refresh()}
              disabled={refreshing}
              className="min-h-[32px] rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated disabled:opacity-40"
            >
              {refreshing ? '…' : 'Refresh'}
            </button>
            <button
              onClick={() => void triggerBriefGenerate(briefTone, setError)}
              disabled={generatingBrief}
              className="min-h-[32px] rounded-ios px-3 text-ios-body text-accent active:bg-bg-elevated disabled:opacity-40"
              title="Generate today's brief now"
            >
              {generatingBrief ? '…' : 'Brief'}
            </button>
            {user && <UserBadge user={user} onSignedOut={() => setUser(null)} />}
          </div>
        </div>
      </header>

      {activeSources.size > 0 && (
        <div className="px-4 py-2 border-b border-hairline bg-bg-surface flex items-center gap-2 text-ios-caption overflow-x-auto whitespace-nowrap">
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
              className="shrink-0 rounded-full bg-accent-soft px-2.5 py-0.5 text-accent text-ios-caption motion-safe:animate-fade-in"
              aria-label={`remove ${src} from filter`}
            >
              {src} ✕
            </button>
          ))}
          <button
            onClick={() => setActiveSources(new Set())}
            className="shrink-0 ml-auto text-ios-caption text-accent active:opacity-60"
          >
            clear all
          </button>
        </div>
      )}

      {error && (
        <div
          role="alert"
          aria-live="assertive"
          className="sticky top-0 z-15 px-4 py-2 bg-red-500/15 border-b border-red-500/40 text-ios-caption text-red-200 flex items-center justify-between gap-2"
        >
          <span className="break-words">{error}</span>
          <button
            onClick={() => setError(null)}
            className="shrink-0 text-red-200/80 active:text-red-200"
            aria-label="dismiss error"
          >
            ✕
          </button>
        </div>
      )}

      {/* Desktop keeps the Brief inline at the top — there's room for
          it. Mobile moves it into its own tab (see the mobile main
          below) so the column content isn't pushed below the fold
          by a card the user has to scroll past every time. */}
      <div className="hidden md:block">
        <BriefCard
          brief={brief}
          onBriefChange={setBrief}
          tone={briefTone}
          onToneChange={setBriefTone}
          triggerGenerate={triggerBriefGenerate}
        />
      </div>

      <PresetChips
        presets={filterPresets}
        onApply={applyPreset}
        onDelete={deletePreset}
        onRename={renamePreset}
        onUpdate={updatePreset}
      />

      {showSearchView ? (
        <main className="flex-1 overflow-y-auto p-3 sm:p-4">
          <SearchResults
            query={searchQuery}
            entries={searchResults.filter((e) => !hiddenSet.has(e.id))}
            sourcesById={sourcesById}
            error={searchError}
            searching={searching}
            categoriesBySourceId={categoriesBySourceId}
            readIds={globalReadIds}
            expandedSummaries={expandedSummaries}
            onToggleSummary={toggleSummary}
            onMarkRead={(entryId) => {
              markEntryRead('Search', entryId)
              toast('Marked as read', {
                kind: 'info',
                action: {
                  label: 'Undo',
                  onClick: () => unmarkEntryRead('Search', entryId),
                },
              })
            }}
            onHide={(entryId) => {
              hideEntry(entryId)
              toast('Entry hidden.', {
                kind: 'info',
                action: {
                  label: 'Undo',
                  onClick: () => restoreHiddenEntry(entryId),
                },
              })
            }}
            onHideToggle={(entryId) => toggleHideEntry('Search', entryId)}
            hiddenSet={hiddenSet}
            onStar={(entryId) => {
              const wasStarred = starredSet.has(entryId)
              toggleStarEntry(entryId)
              toast(
                wasStarred
                  ? 'Removed from Saved.'
                  : 'Saved for later — see the Saved column.',
                {
                  kind: 'info',
                  action: {
                    label: 'Undo',
                    onClick: () => toggleStarEntry(entryId),
                  },
                },
              )
            }}
            starredSet={starredSet}
          />
        </main>
      ) : columns.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-ios-body text-label-secondary px-4 text-center">
          no entries yet — the scheduler will fetch the first batch shortly, or hit Refresh
        </div>
      ) : (
        <>
          {/* Desktop. The view is two surfaces stacked:
              1. For You row — only in 'all' view, only when forYou is
                 non-empty. Full-width card grid; no column chrome so
                 the personal feed reads as the "front page".
              2. By-category grid — the All Subs columns. Skipped in
                 'multisub' view because the multi-sub column is the
                 whole dashboard there. */}
          {viewKind === 'all' && visibleForYou.length > 0 && (
            <section className="hidden md:block px-4 pt-4 pb-3 border-b border-hairline">
              <header className="flex items-center justify-between mb-2">
                <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
                  For You
                </h2>
                <span className="text-ios-caption text-label-tertiary">
                  {visibleForYou.length} {visibleForYou.length === 1 ? 'entry' : 'entries'}
                </span>
              </header>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {visibleForYou.map((e) => {
                  // Per-card engagement props. The For You
                  // row was previously read-only — no
                  // mark-read, no hide, no star. The user
                  // could see the personal feed but
                  // couldn't engage with it. Now every
                  // card has the full set of per-card
                  // actions wired to App-level callbacks
                  // so the user can mark-read, hide, or
                  // star a For You card the same way
                  // they would in a category column.
                  //
                  // ``unread`` reflects ``globalReadIds``, the
                  // union of every column's manual read-set —
                  // so marking this card read here also dims
                  // it in its category column, and marking it
                  // read there dims it here too. The per-column
                  // lastViewed isn't used here (the For You row
                  // has no "mark all read" button).
                  //
                  // ``selected`` / ``onActivate`` /
                  // ``cardRef`` are not passed because
                  // keyboard nav is per-column, not
                  // per-row, and the For You row is
                  // not a "column" the user can scroll
                  // into.
                  const isRead = globalReadIds.has(e.id)
                  return (
                    <Card
                      key={e.id}
                      entry={e}
                      sourceName={sourcesById.get(e.source_id)}
                      category={categoriesBySourceId.get(e.source_id)}
                      unread={!isRead}
                      expanded={expandedSummaries.has(e.id)}
                      onToggleSummary={() => toggleSummary(e.id)}
                      onMarkRead={() => {
                        markEntryRead('For You', e.id)
                        toast('Marked as read', {
                          kind: 'info',
                          action: {
                            label: 'Undo',
                            onClick: () => unmarkEntryRead('For You', e.id),
                          },
                        })
                      }}
                      onHide={() => {
                        hideEntry(e.id)
                        toast('Entry hidden.', {
                          kind: 'info',
                          action: {
                            label: 'Undo',
                            onClick: () => restoreHiddenEntry(e.id),
                          },
                        })
                      }}
                      onHideToggle={() => toggleHideEntry('For You', e.id)}
                      hidden={hiddenSet.has(e.id)}
                      onStar={() => {
                        const wasStarred = starredSet.has(e.id)
                        toggleStarEntry(e.id)
                        toast(
                          wasStarred
                            ? 'Removed from Saved.'
                            : 'Saved for later — see the Saved column.',
                          {
                            kind: 'info',
                            action: {
                              label: 'Undo',
                              onClick: () => toggleStarEntry(e.id),
                            },
                          },
                        )
                      }}
                      starred={starredSet.has(e.id)}
                    />
                  )
                })}
              </div>
            </section>
          )}
          {viewKind === 'all' && <FramingWatch />}
          <main className="hidden md:grid md:grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 p-4 flex-1 overflow-y-auto">
            {columns
              .filter((col) => viewKind === 'multisub' || col.name !== 'For You')
              .map((col, ci) => (
                <div key={col.name} ref={setColumnRef(col.name)} className="contents">
                  <Column
                    name={col.name}
                    sections={sectionsByColumn.get(col.name) ?? { new: [], history: [] }}
                    sourcesById={sourcesById}
                    newCount={newCountByColumn.get(col.name)}
                    onRefresh={refresh}
                    selectedId={ci === selectedColumnIndex ? selectedCardId ?? undefined : undefined}
                    cardRefs={cardRefs}
                    onMarkRead={() => markColumnRead(col.name)}
                    onMarkEntryRead={(entryId) => {
                      markEntryRead(col.name, entryId)
                      toast('Marked as read', {
                        kind: 'info',
                        action: {
                          label: 'Undo',
                          onClick: () => unmarkEntryRead(col.name, entryId),
                        },
                      })
                    }}
                    onHideEntry={(entryId) => {
                      hideEntry(entryId)
                      toast('Entry hidden.', {
                        kind: 'info',
                        action: {
                          label: 'Undo',
                          onClick: () => restoreHiddenEntry(entryId),
                        },
                      })
                    }}
                    onHideToggle={(entryId) => toggleHideEntry(col.name, entryId)}
                    hiddenSet={hiddenSet}
                    onStarEntry={(entryId) => {
                      const wasStarred = starredSet.has(entryId)
                      toggleStarEntry(entryId)
                      toast(
                          wasStarred
                            ? 'Removed from Saved.'
                            : 'Saved for later — see the Saved column.',
                          {
                            kind: 'info',
                            action: {
                              label: 'Undo',
                              onClick: () => toggleStarEntry(entryId),
                            },
                          },
                        )
                    }}
                    starredSet={starredSet}
                    prefs={columnPrefs[col.name] ?? DEFAULT_PREFS}
                    onPrefsChange={(next) => setPrefsFor(col.name, next)}
                    totalCount={col.totalCount}
                    categoriesBySourceId={categoriesBySourceId}
                    expandedSummaries={expandedSummaries}
                    onToggleSummary={toggleSummary}
                    sectionsCollapsed={sectionsCollapsedFor(col.name)}
                    onToggleSection={(key) => setColumnSection(col.name, key)}
                  />
                </div>
              ))}
          </main>

          <main className="md:hidden flex-1 min-h-0 flex flex-col p-3">
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
            <div
              role="tablist"
              aria-label="dashboard sections"
              className="shrink-0 flex gap-1.5 overflow-x-auto pb-2 -mx-1 px-1"
            >
              <button
                type="button"
                role="tab"
                aria-selected={mobileCol === -1}
                onClick={() => setMobileCol(-1)}
                className={`shrink-0 rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
                  mobileCol === -1
                    ? 'bg-accent text-white'
                    : 'bg-bg-surface text-label-secondary active:bg-bg-elevated'
                }`}
              >
                Brief
              </button>
              {columns.map((c, i) => {
                const newCount = newCountByColumn.get(c.name)
                return (
                  // Navigation, not mark-read. Merely peeking at a
                  // column shouldn't drop its "+N new" chip — that
                  // violates the universal-inbox rule. The column
                  // header (desktop) and the per-card ✓ button are
                  // the explicit mark-read affordances.
                  <button
                    type="button"
                    key={c.name}
                    role="tab"
                    aria-selected={i === mobileCol}
                    onClick={() => setMobileCol(i)}
                    className={`shrink-0 flex items-center gap-1 rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
                      i === mobileCol
                        ? 'bg-accent text-white'
                        : 'bg-bg-surface text-label-secondary active:bg-bg-elevated'
                    }`}
                  >
                    {c.name}
                    {!!newCount && (
                      <span
                        className={`rounded-full px-1.5 text-[10px] leading-4 font-semibold ${
                          i === mobileCol ? 'bg-white/25 text-white' : 'bg-accent-soft text-accent'
                        }`}
                      >
                        {newCount}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto">
              {mobileCol === -1 ? (
                <BriefCard
                  brief={brief}
                  onBriefChange={setBrief}
                  tone={briefTone}
                  onToneChange={setBriefTone}
                  triggerGenerate={triggerBriefGenerate}
                />
              ) : (
                <Column
                  name={columns[mobileCol]?.name ?? ''}
                  sections={
                    sectionsByColumn.get(columns[mobileCol]?.name ?? '') ?? { new: [], history: [] }
                  }
                  sourcesById={sourcesById}
                  newCount={newCountByColumn.get(columns[mobileCol]?.name ?? '')}
                  onRefresh={refresh}
                  selectedId={
                    mobileCol === selectedColumnIndex ? selectedCardId ?? undefined : undefined
                  }
                  cardRefs={cardRefs}
                  onMarkRead={() => markColumnRead(columns[mobileCol]?.name ?? '')}
                  onMarkEntryRead={(entryId) => {
                    const col = columns[mobileCol]?.name ?? ''
                    markEntryRead(col, entryId)
                    toast('Marked as read', {
                      kind: 'info',
                      action: {
                        label: 'Undo',
                        onClick: () => unmarkEntryRead(col, entryId),
                      },
                    })
                  }}
                  onHideEntry={(entryId) => {
                    hideEntry(entryId)
                    toast('Entry hidden.', {
                      kind: 'info',
                      action: {
                        label: 'Undo',
                        onClick: () => restoreHiddenEntry(entryId),
                      },
                    })
                  }}
                  onHideToggle={(entryId) => toggleHideEntry(columns[mobileCol]?.name ?? '', entryId)}
                  hiddenSet={hiddenSet}
                  onStarEntry={(entryId) => {
                    const wasStarred = starredSet.has(entryId)
                    toggleStarEntry(entryId)
                    toast(
                              wasStarred
                                ? 'Removed from Saved.'
                                : 'Saved for later — see the Saved column.',
                              {
                                kind: 'info',
                                action: {
                                  label: 'Undo',
                                  onClick: () => toggleStarEntry(entryId),
                                },
                              },
                            )
                  }}
                  starredSet={starredSet}
                  prefs={
                    columns[mobileCol]
                      ? columnPrefs[columns[mobileCol].name] ?? DEFAULT_PREFS
                      : DEFAULT_PREFS
                  }
                  onPrefsChange={(next) =>
                    columns[mobileCol] && setPrefsFor(columns[mobileCol].name, next)
                  }
                  totalCount={columns[mobileCol]?.totalCount}
                  categoriesBySourceId={categoriesBySourceId}
                  expandedSummaries={expandedSummaries}
                  onToggleSummary={toggleSummary}
                  sectionsCollapsed={sectionsCollapsedFor(columns[mobileCol]?.name ?? '')}
                  onToggleSection={(key) => setColumnSection(columns[mobileCol]?.name ?? '', key)}
                />
              )}
            </div>
          </main>
        </>
      )}

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        categories={categories}
        activeSources={activeSources}
        onSourceToggle={toggleSourceAndMaybeClose}
        onClearAllFilters={clearSourceFilters}
        onCategoryJump={jumpToCategory}
        onResetLocalState={resetLocalState}
        onOpenSettings={(tab) => openSettings(tab ?? 'feeds')}
        onSavePreset={() => {
          // Use a native prompt for the preset name.
          // Simple, no extra state, no overlay. A
          // custom modal would be 80 lines of code
          // and not noticeably better UX. The
          // savePreset callback returns the new
          // preset id; we just toast the name.
          //
          // ``window.prompt`` returns ``null`` when
          // the user dismisses (Esc / Cancel) —
          // skip the save in that case. An empty
          // string falls through to the default
          // "Untitled preset" label that savePreset
          // already provides.
          const name = window.prompt(
            'Name this view (e.g. “AI safety”, “Hockey + deals”, “muted everything”)',
          )
          if (name === null) return
          savePreset(name)
          toast(`Saved view “${name.trim() || 'Untitled preset'}”`, 'info')
        }}
      />
      <Settings
        open={settingsOpen}
        tab={settingsTab}
        sources={sources}
        onRefreshSources={async () => { void refresh() }}
        onError={setError}
        onClose={closeSettings}
        briefTone={briefTone}
        onBriefToneChange={setBriefTone}
        triggerGenerate={triggerBriefGenerate}
        generating={generatingBrief}
        onSourceRenamed={onSourceRenamed}
        onResetLocalState={resetLocalState}
        hiddenEntries={hiddenEntries}
        onRestoreHidden={(entryId) => {
          const next = hiddenEntriesRef.current.filter((id) => id !== entryId)
          if (next.length !== hiddenEntriesRef.current.length) {
            setHiddenEntries(next)
          }
        }}
        onRestoreAllHidden={() => {
          if (hiddenEntriesRef.current.length === 0) return
          setHiddenEntries([])
          toast('All hidden entries restored.', 'info')
        }}
        starredEntries={starredEntries}
        onUnstarAll={() => {
          if (starredEntriesRef.current.length === 0) return
          setStarredEntries([])
          toast('All saved entries cleared.', 'info')
        }}
        onUnstarEntry={(entryId) => {
          // Per-row unstar from the Settings → Saved
          // tab list. Mirrors the card's star
          // button + the per-card context-menu
          // "Unsave" action.
          setStarredEntries(
            starredEntriesRef.current.filter((id) => id !== entryId),
          )
        }}
      />

      <ShortcutsSheet
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />

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



















