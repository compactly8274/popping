// User preferences API client + React provider.
//
// The dashboard keeps several user-visible pieces of state in
// per-user server-backed storage:
//
//   - seenEntries     per-category "I've seen this story" ids (PR3b)
//   - readEntries     per-column "I marked this card read" ids, from
//                     builds before PR3b: read, never written
//   - lastViewed      per-column "I last saw this column at" ISO timestamps
//                     (before PR3b; read only so Reset can delete them)
//   - columnPrefs     per-column sort/min-score/max-age
//   - columnSections  per-column Fresh/History section collapse (before
//                     PR3b; read only so Reset can delete them)
//   - hiddenEntries   per-user "hide this entry" ids
//   - starredEntries  per-user saved-for-later entry ids
//   - votedEntries    per-user entry id -> up / down
//   - filterPresets   user-saved filter+prefs views
//   - historyGroupBy  History tab grouping (``entry`` or ``none``)
//   - categoryLayout  the tab order and the hidden tabs
//
// They used to live in localStorage. They were per-device there, so a
// phone and a laptop saw different read state, different sort orders,
// etc. The server-backed store fixes that -- on every device, the same
// user_id sees the same state.
//
// How it syncs (PR2b; the rules and their tests are in
// ``lib/prefSync.ts``, which this provider wires to React, the API and
// the page's lifecycle):
//
//   - Set keys (read marks, hides, saves, votes) are written as
//     changes: ``addIds`` / ``removeIds`` / ``setVotes`` queue a PATCH
//     of just that change, so two devices, or two calls in one tick,
//     never undo each other. The other keys are whole values and stay
//     PUT (``setColumnPrefs`` etc.).
//   - Optimistic: a write shows at once. The screen is the server's
//     value with every write not yet acknowledged applied on top.
//   - Set keys go out in a 1 s batch, the others in a 250 ms one.
//     Leaving the page (hidden, pagehide, beforeunload) sends what is
//     queued with ``keepalive``.
//   - ``resync()`` reads what changed on the server since the last
//     read (App calls it on each poll); ``resync({ full: true })``
//     reads everything, which is also how deleted rows arrive (App
//     calls it when the page comes back to the front).
//   - A write that doesn't get through is tried again (2, 4, 8 s, and
//     again when the page comes back); one the server refuses is
//     dropped. Each ends in one toast. A write still unanswered after
//     30 s, or when the page comes back, is not sent again: the next
//     full read shows whether it landed.
//
// Server contract
// ---------------
//
// ``GET    /api/preferences[?since=<iso>]``  -> ``{ items: [{key, value, updated_at}] }``
// ``PUT    /api/preferences/{key}``   body: ``{ value: any }``   -> the row
// ``PATCH  /api/preferences/{key}``   body: ``{add?, remove?}`` or ``{set?}`` -> the row
// ``DELETE /api/preferences/{key}``   -> 204
//
// We use namespaced keys (``seen:<category>``, ``read_entries:<columnId>``,
// ``last_viewed:<columnId>``, ``column_prefs:<columnId>``,
// ``column_sections:<columnId>``, ``hidden_entries``,
// ``starred_entries``, ``voted_entries``, ``filter_presets``,
// ``history_group_by``, ``category_layout``) on the server's allowlist
// (routes/preferences.py). The constant ``PREFERENCE_KEYS`` below is
// the source of truth for which keys exist.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError } from '../api'
import {
  coerceIds,
  coerceVotes,
  createPrefSync,
  legacySeedPlan,
  type PrefSync,
  type Rows,
  type SyncTransport,
  type Vote,
} from './prefSync'
import { safeGetItem, safeRemoveItem } from './storage'
import { normalizeLayout, type CategoryLayout } from './categories'
import { toast } from '../components/Toast'

// ---------------------------------------------------------------------------
// Key registry. Single source of truth for which preference keys exist
// and which shape their value takes. The server stores whatever JSONB a
// PUT gives it (set keys it checks and trims); the TypeScript types here
// are the contract.
// ---------------------------------------------------------------------------

/**
 * One column's read-entries set: which entry ids the user has manually
 * marked read. Keyed by source id (the dashboard's column id).
 */
export type ReadEntriesValue = number[]

/** One column's "I last saw this column at" ISO timestamp. */
export type LastViewedValue = string

/**
 * One column's sort/filter preferences. Matches ``TabPrefs`` in
 * ``lib/sorting.ts`` (Column's ``ColumnPrefs``), which reads it:
 * ``top`` and ``oldest`` are older builds' values, and ``minScore`` is
 * kept in the shape but no longer filters. ``window`` is the tab's
 * window (``lib/window``, PR4d); rows older builds wrote hold a numeric
 * ``maxAgeHours`` instead, which is read (``windowOf``). New writes
 * keep ``maxAgeHours`` null next to ``window``, so an older build still
 * open somewhere keeps ``window`` when it writes the row.
 */
export type ColumnPrefsValue = {
  sort: 'top' | 'newest' | 'oldest' | 'best'
  minScore: number
  window?: '1d' | '2d' | '7d' | 'all'
  maxAgeHours?: number | null
}

/** One column's Fresh/History section collapse state. */
export type ColumnSectionsValue = {
  newCollapsed: boolean
  historyCollapsed: boolean
}

/** The History tab's group-by mode. */
export type HistoryGroupByValue = 'entry' | 'none'

/** The tab order and the hidden tabs (``lib/categories.ts``). One whole
 *  value, written by PUT on every change; a stale read can't undo a
 *  change still on its way (PR2b's per-key write sequence). */
export type CategoryLayoutValue = CategoryLayout

/**
 * Per-entry vote direction. Keyed by entry id (as a string — JSON
 * object keys are always strings; consumers convert with
 * ``String(entryId)`` / ``Number(key)``). Absence of a key means "no
 * vote" — there's no explicit ``null`` entry, matching how
 * hiddenEntries/starredEntries represent "not a member" as "not in
 * the array" rather than storing an explicit false.
 */
export type VotedEntriesValue = Record<string, 'up' | 'down'>

/**
 * One filter preset. Captures a complete dashboard view:
 * which sources are filtered, and the per-column prefs.
 */
export type FilterPresetValue = {
  id: string
  name: string
  activeSources: string[]
  columnPrefs: Record<string, ColumnPrefsValue>
}

/**
 * The flat shape of the preferences state, decoded from the server's
 * rows (see ``decodeState``).
 */
export type PreferencesState = {
  // Seen marks, keyed by the story's category (``seen:<category>``):
  // where every mark goes from PR3b on, wherever the story was seen.
  seenEntries: Record<string, ReadEntriesValue>
  // Per-column maps keyed by the column's name (a category, "For You",
  // "Saved", "Search" ...). ``read_entries`` rows are the read marks of
  // builds before PR3b: still read (a story marked there counts as
  // seen), never written.
  readEntries: Record<string, ReadEntriesValue>
  lastViewed: Record<string, LastViewedValue>
  columnPrefs: Record<string, ColumnPrefsValue>
  columnSections: Record<string, ColumnSectionsValue>
  // Per-user lists. The dashboard treats these as flat arrays.
  hiddenEntries: number[]
  starredEntries: number[]
  // Per-user map: entry id -> vote direction. A map (not a list, like
  // hidden/starred) because a vote is three-state (up/down/none), not
  // a boolean membership test.
  votedEntries: VotedEntriesValue
  // Per-user singletons (one value, not a map).
  filterPresets: FilterPresetValue[]
  historyGroupBy: HistoryGroupByValue
  // Null until the tabs have been reordered or hidden once (every
  // device then shows the default order).
  categoryLayout: CategoryLayoutValue | null
}

/** Helper: which server-side keys exist, and what TS type each holds. */
export const PREFERENCE_KEYS = {
  seen: 'seen',
  readEntries: 'read_entries',
  lastViewed: 'last_viewed',
  columnPrefs: 'column_prefs',
  columnSections: 'column_sections',
  hiddenEntries: 'hidden_entries',
  starredEntries: 'starred_entries',
  votedEntries: 'voted_entries',
  filterPresets: 'filter_presets',
  historyGroupBy: 'history_group_by',
  categoryLayout: 'category_layout',
} as const

/** A key whose value is a set of story ids: changed with ``addIds`` /
 *  ``removeIds``, never written whole. New seen marks go only to
 *  ``seen:<category>`` (PR3b); ``read_entries:*`` is only read. */
export type IdSetKey =
  | typeof PREFERENCE_KEYS.starredEntries
  | typeof PREFERENCE_KEYS.hiddenEntries
  | `${typeof PREFERENCE_KEYS.readEntries}:${string}`
  | `seen:${string}`

// ``MAX_PRESETS`` is the cap on saved filter presets. The set keys'
// caps (``MAX_HIDDEN`` and the rest) live in ``lib/prefSync.ts``,
// which prunes to them.
export const MAX_PRESETS = 50

// Empty values shared by every decode, so a key with no row keeps one
// reference across renders (Column's memo compares by reference).
const NO_IDS: number[] = []
const NO_VOTES: VotedEntriesValue = {}
const NO_PRESETS: FilterPresetValue[] = []

// ---------------------------------------------------------------------------
// Decoding rows into the typed state.
// ---------------------------------------------------------------------------

type Decoded = { rows: Rows; state: PreferencesState }

const PER_COLUMN = [
  ['seenEntries', PREFERENCE_KEYS.seen],
  ['readEntries', PREFERENCE_KEYS.readEntries],
  ['lastViewed', PREFERENCE_KEYS.lastViewed],
  ['columnPrefs', PREFERENCE_KEYS.columnPrefs],
  ['columnSections', PREFERENCE_KEYS.columnSections],
] as const

const isObject = (v: unknown): v is object => !!v && typeof v === 'object' && !Array.isArray(v)

// One per-column value, or undefined when it has the wrong shape.
function columnValue(field: (typeof PER_COLUMN)[number][0], value: unknown): unknown {
  switch (field) {
    case 'seenEntries':
    case 'readEntries':
      return coerceIds(value)
    case 'lastViewed':
      return typeof value === 'string' ? value : undefined
    default:
      return isObject(value) ? value : undefined
  }
}

/**
 * The typed state for ``rows``. Each part (a per-column map, a list)
 * whose rows didn't change since ``prev`` is ``prev``'s object, and
 * with nothing changed ``prev.state`` itself comes back, so a resync
 * re-renders only what it changed. Unknown keys are ignored (a
 * newer build's keys).
 */
function decodeState(rows: Rows, prev: Decoded | null): PreferencesState {
  if (prev && prev.rows === rows) return prev.state
  const changed = (key: string) => !prev || prev.rows[key] !== rows[key]
  const anyChanged = (prefix: string) => {
    if (!prev) return true
    for (const k of Object.keys(rows)) if (k.startsWith(prefix) && rows[k] !== prev.rows[k]) return true
    for (const k of Object.keys(prev.rows)) if (k.startsWith(prefix) && !(k in rows)) return true
    return false
  }
  const next = { ...(prev?.state ?? {}) } as PreferencesState
  let same = !!prev
  for (const [field, name] of PER_COLUMN) {
    const prefix = `${name}:`
    if (!anyChanged(prefix)) continue
    same = false
    const map: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(rows)) {
      if (!k.startsWith(prefix)) continue
      const value = columnValue(field, v)
      if (value !== undefined) map[k.slice(prefix.length)] = value
    }
    ;(next as Record<string, unknown>)[field] = map
  }
  const single = <K extends keyof PreferencesState>(field: K, key: string, decode: (v: unknown) => PreferencesState[K]) => {
    if (prev && !changed(key)) return
    same = false
    next[field] = decode(rows[key])
  }
  single('hiddenEntries', PREFERENCE_KEYS.hiddenEntries, (v) => (v === undefined ? NO_IDS : coerceIds(v)))
  single('starredEntries', PREFERENCE_KEYS.starredEntries, (v) => (v === undefined ? NO_IDS : coerceIds(v)))
  single('votedEntries', PREFERENCE_KEYS.votedEntries, (v) => (v === undefined ? NO_VOTES : coerceVotes(v)))
  single('filterPresets', PREFERENCE_KEYS.filterPresets, (v) => (Array.isArray(v) ? (v as FilterPresetValue[]) : NO_PRESETS))
  single('historyGroupBy', PREFERENCE_KEYS.historyGroupBy, (v) => (v === 'none' ? 'none' : 'entry'))
  single('categoryLayout', PREFERENCE_KEYS.categoryLayout, normalizeLayout)
  return same && prev ? prev.state : next
}

// A decoder that remembers its last answer, so the render and
// ``latest()`` share one object per set of rows.
function makeDecoder() {
  let last: Decoded | null = null
  return (rows: Rows): PreferencesState => {
    const state = decodeState(rows, last)
    last = { rows, state }
    return state
  }
}

// ---------------------------------------------------------------------------
// LocalStorage seed (a one-way migration from builds before the server
// store).
//
// Those builds kept these values in localStorage. On the first launch
// with an old value there, it is written to the server once, as a
// union: set keys add only the ids the server lacks (never a whole-list
// PUT, which could take saves made on another device), and the other
// keys are written only where the server has no row. That happens after
// the first full GET has read the server's rows; only then are the old
// entries removed and the ``seeded`` flag set, so a failed GET keeps
// them for the next launch.
// ---------------------------------------------------------------------------

const SEED_FLAG_KEY = 'popping.v1.preferences.seeded'

// Old localStorage key paths the previous version of the code
// wrote. These are inline here (not imported from
// ``lib/storage.ts``) because that file no longer exports
// them -- the storage backend moved to the server. The
// strings are preserved EXACTLY so this seed function can
// read values written by a build that still used
// localStorage. Don't change them without a migration plan.
const LEGACY_STORAGE_KEYS = {
  readEntries: 'popping.v1.col.readEntries',
  lastViewed: 'popping.v1.col.lastViewed',
  columnPrefs: 'popping.v1.col.prefs',
  columnSections: 'popping.v1.col.sections',
  hiddenEntries: 'popping.v1.hidden.entries',
  starredEntries: 'popping.v1.starred.entries',
  filterPresets: 'popping.v1.presets.list',
  historyGroupBy: 'popping.v1.history.groupBy',
} as const

/** What an old build left in localStorage, as server rows (key ->
 *  value). Null once the seed has run. Reads only. */
function readLegacy(): Rows | null {
  if (typeof window === 'undefined' || safeGetItem(SEED_FLAG_KEY) === '1') return null
  const out: Record<string, unknown> = {}
  const parse = (key: string): unknown => {
    const raw = safeGetItem(key)
    if (!raw) return undefined
    try {
      return JSON.parse(raw)
    } catch {
      // Corrupt: dropped with the rest once the seed has run.
      return undefined
    }
  }
  // Per-column maps. The old keys stored one JSON blob each; on the
  // server each column is its own row.
  for (const [localKey, prefKey] of [
    [LEGACY_STORAGE_KEYS.readEntries, PREFERENCE_KEYS.readEntries],
    [LEGACY_STORAGE_KEYS.lastViewed, PREFERENCE_KEYS.lastViewed],
    [LEGACY_STORAGE_KEYS.columnPrefs, PREFERENCE_KEYS.columnPrefs],
    [LEGACY_STORAGE_KEYS.columnSections, PREFERENCE_KEYS.columnSections],
  ] as const) {
    const parsed = parse(localKey)
    if (!isObject(parsed)) continue
    for (const [columnId, value] of Object.entries(parsed)) if (columnId) out[`${prefKey}:${columnId}`] = value
  }
  for (const [localKey, prefKey] of [
    [LEGACY_STORAGE_KEYS.hiddenEntries, PREFERENCE_KEYS.hiddenEntries],
    [LEGACY_STORAGE_KEYS.starredEntries, PREFERENCE_KEYS.starredEntries],
    [LEGACY_STORAGE_KEYS.filterPresets, PREFERENCE_KEYS.filterPresets],
  ] as const) {
    const parsed = parse(localKey)
    if (Array.isArray(parsed)) out[prefKey] = parsed
  }
  // Old code stored the History grouping as a bare 'entry' / 'none'.
  const groupBy = safeGetItem(LEGACY_STORAGE_KEYS.historyGroupBy)
  if (groupBy === 'entry' || groupBy === 'none') out[PREFERENCE_KEYS.historyGroupBy] = groupBy
  return out
}

function finishLegacy() {
  for (const key of Object.values(LEGACY_STORAGE_KEYS)) safeRemoveItem(key)
  try {
    window.localStorage?.setItem(SEED_FLAG_KEY, '1')
  } catch {
    // Best-effort. The next launch reads the old keys again, finds
    // them gone and sets the flag then.
  }
}

// ---------------------------------------------------------------------------
// Transport.
// ---------------------------------------------------------------------------

const transport: SyncTransport = {
  get: async (since, signal) => (await api.getPreferences(since, signal)).items,
  patch: (key, delta, keepalive) => api.patchPreference(key, delta, { keepalive }),
  put: (key, value, keepalive) => api.setPreference(key, value, { keepalive }),
  delete: async (key, keepalive) => {
    const res = await api.deletePreference(key, { keepalive })
    if (!res.ok) throw new ApiError(`${res.status} ${res.statusText} for DELETE ${key}`, undefined, res.status)
  },
}

// ---------------------------------------------------------------------------
// PreferencesContext + Provider.
// ---------------------------------------------------------------------------

export interface PreferencesContextValue {
  /** Full state. Consumers select into it via the typed setters
   *  below; they don't read this object directly. */
  state: PreferencesState
  /** True until the first GET /api/preferences settles. Consumers that
   *  need the state before the first paint can gate on this. */
  loading: boolean
  /** True once a full GET /api/preferences has answered and its rows
   *  are in ``state``. False while the first is on the way, and after
   *  it failed until a later one (a poll) gets through: ``state`` then
   *  holds only the defaults and this page's own writes, so a
   *  preference that is missing from it may only be unread. */
  loaded: boolean
  /** True once the localStorage seed has run (with the first full
   *  GET). */
  seeded: boolean

  /** ``state`` as of now, including writes made earlier in this tick.
   *  For handlers that Column's memo keeps across renders, which would
   *  read an old ``state`` from their closure. */
  latest: () => PreferencesState

  // Set keys: changes, never whole lists. Each shows at once and queues
  // a PATCH of just that change. Stable across renders.

  /** Add story ids to a set key. Over the key's cap the oldest ids go
   *  (never these, nor ``keep``), in the same PATCH. */
  addIds: (key: IdSetKey, ids: number[], keep?: Iterable<number>) => void
  /** Remove story ids from a set key. */
  removeIds: (key: IdSetKey, ids: number[]) => void
  /** Set (or clear, via ``null``) votes, by entry id. */
  setVotes: (changes: Record<number, Vote | null>) => void

  // The other keys: whole values, PUT. Stable across renders.
  // (``last_viewed:*`` and ``column_sections:*`` are no longer written
  // from PR3b on: the "+N new" chip and the Fresh / History folds went.
  // Reset still deletes their rows.)

  /** Update the sort/filter prefs for one column. */
  setColumnPrefs: (columnId: string, prefs: ColumnPrefsValue) => void

  /** Replace the saved-presets list. */
  setFilterPresets: (presets: FilterPresetValue[]) => void

  /** Set the History tab's group-by mode. */
  setHistoryGroupBy: (mode: HistoryGroupByValue) => void

  /** Replace the tab order and hidden tabs (one PUT per change). */
  setCategoryLayout: (layout: CategoryLayoutValue) => void

  /** Read the server's rows: by default only those changed since the
   *  last read (each poll), with ``full`` all of them, which is also how
   *  deleted rows arrive (the page coming back to the front). One read
   *  at a time; calls during one share it. */
  resync: (opts?: { full?: boolean }) => Promise<void>

  /** Send every write still queued, or waiting on a retry, now; wait for
   *  those and the writes already on the way. True when none of them is
   *  left to retry: each was saved, or refused for good, or answered with
   *  a 404 or 405 (a server rolled back to before PATCH), which a reload
   *  onto the matching build is the cure for. The reload onto a new
   *  build runs it first and doesn't reload on false (PR1r). */
  flushPending: () => Promise<boolean>

  /** Run ``fn`` before the queue is read when the page leaves or
   *  ``flushPending`` runs (collectors that drain into ``addIds``).
   *  Returns the unregister function. */
  registerBeforeFlush: (fn: () => void) => () => void

  /** Reset only, which reloads next. ``drop`` names the keys Reset
   *  deletes: their queued writes and retries are dropped, and nothing
   *  more is queued or sent for them, not even when the page goes
   *  (otherwise the reload's leave flush would write the rows back after
   *  Reset's DELETEs). Writes for every other key (saves, hides, votes,
   *  saved views) go now. Waits for those and the writes on the way, at
   *  most 3 s: a request that never settles must not stop Reset. */
  discardPending: (drop: (key: string) => boolean) => Promise<void>
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

// When the last "couldn't save" toast showed: one per burst.
const FAILURE_TOAST_MS = 30_000

/**
 * Provider. Mounts once at the top of the React tree. Children
 * get the ``usePreferences()`` hook.
 *
 * Lifecycle:
 *   1. Mount: a full GET. Its rows become ``state``; then the
 *      localStorage seed (if an old build left anything) is written.
 *   2. Writes: optimistic in ``state`` at once, then batched to the
 *      server (``lib/prefSync.ts``).
 *   3. Hidden, pagehide, beforeunload: queued writes go with
 *      ``keepalive``. Back in front: writes still unanswered are given
 *      up on (App's full GET shows what the server holds), and failed
 *      ones are tried again.
 */
export function PreferencesProvider({ children }: { children: ReactNode }) {
  const [rows, setRows] = useState<Rows>({})
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [seeded, setSeeded] = useState(false)
  const [decode] = useState(makeDecoder)

  const [sync] = useState((): PrefSync => {
    let legacy = readLegacy()
    let failureToastAt = -Infinity
    const engine = createPrefSync({
      transport,
      onRows: setRows,
      onLoad: (ok) => {
        setLoading(false)
        if (!ok) return
        setLoaded(true)
        setSeeded(true)
        if (!legacy) return
        const plan = legacySeedPlan(legacy, engine.rows())
        for (const [key, ids] of plan.adds) engine.addIds(key, ids)
        for (const [key, value] of plan.puts) engine.put(key, value)
        legacy = null
        finishLegacy()
      },
      onError: (kind) => {
        const now = Date.now()
        if (now - failureToastAt <= FAILURE_TOAST_MS) return
        failureToastAt = now
        toast(
          kind === 'unsaved'
            ? "Couldn't save your latest changes to Popping. They'll be lost when the app reloads."
            : "Couldn't save that change. Reload Popping to try again.",
          'error',
        )
      },
      visible: () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
    })
    return engine
  })

  // First-mount GET. (Under StrictMode's second run it joins the first.)
  useEffect(() => {
    void sync.resync({ full: true })
  }, [sync])

  // Leaving and coming back. ``visibilitychange: hidden`` is the one a
  // home-screen app gets when a story opens; ``pagehide`` and
  // ``beforeunload`` cover a closing tab and a reload.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') sync.leave()
      else sync.resume()
    }
    const onLeave = () => sync.leave()
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', onLeave)
    window.addEventListener('beforeunload', onLeave)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', onLeave)
      window.removeEventListener('beforeunload', onLeave)
    }
  }, [sync])

  const state = useMemo(() => decode(rows), [decode, rows])
  const latest = useCallback(() => decode(sync.rows()), [decode, sync])

  const addIds = useCallback((key: IdSetKey, ids: number[], keep?: Iterable<number>) => sync.addIds(key, ids, keep), [sync])
  const removeIds = useCallback((key: IdSetKey, ids: number[]) => sync.removeIds(key, ids), [sync])
  const setVotes = useCallback((changes: Record<number, Vote | null>) => sync.setVotes(changes), [sync])

  const setColumnPrefs = useCallback(
    (columnId: string, prefs: ColumnPrefsValue) => sync.put(`${PREFERENCE_KEYS.columnPrefs}:${columnId}`, prefs),
    [sync],
  )
  const setFilterPresets = useCallback(
    (presets: FilterPresetValue[]) => sync.put(PREFERENCE_KEYS.filterPresets, presets),
    [sync],
  )
  const setHistoryGroupBy = useCallback(
    (mode: HistoryGroupByValue) => sync.put(PREFERENCE_KEYS.historyGroupBy, mode),
    [sync],
  )
  const setCategoryLayout = useCallback(
    (layout: CategoryLayoutValue) => sync.put(PREFERENCE_KEYS.categoryLayout, layout),
    [sync],
  )

  const value = useMemo<PreferencesContextValue>(
    () => ({
      state,
      loading,
      loaded,
      seeded,
      latest,
      addIds,
      removeIds,
      setVotes,
      setColumnPrefs,
      setFilterPresets,
      setHistoryGroupBy,
      setCategoryLayout,
      resync: sync.resync,
      flushPending: sync.flushPending,
      registerBeforeFlush: sync.registerBeforeFlush,
      discardPending: sync.discardPending,
    }),
    [
      state,
      loading,
      loaded,
      seeded,
      latest,
      addIds,
      removeIds,
      setVotes,
      setColumnPrefs,
      setFilterPresets,
      setHistoryGroupBy,
      setCategoryLayout,
      sync,
    ],
  )

  return (
    <PreferencesContext.Provider value={value}>
      {children}
    </PreferencesContext.Provider>
  )
}

/** Hook for child components. Throws if used outside the provider. */
export function usePreferences(): PreferencesContextValue {
  const ctx = useContext(PreferencesContext)
  if (!ctx) {
    throw new Error(
      'usePreferences must be used inside <PreferencesProvider>',
    )
  }
  return ctx
}
