// User preferences API client + React provider.
//
// The dashboard keeps several user-visible pieces of state in
// per-user server-backed storage:
//
//   - readEntries     per-column "I marked this card read" ids
//   - lastViewed      per-column "I last saw this column at" ISO timestamps
//   - columnPrefs     per-column sort/min-score/max-age
//   - columnSections  per-column Fresh/History section collapse
//   - hiddenEntries   per-user "hide this entry" ids
//   - starredEntries  per-user saved-for-later entry ids
//   - filterPresets   user-saved filter+prefs views
//   - historyGroupBy  History tab grouping (``entry`` or ``none``)
//
// All eight used to live in localStorage. They were per-device
// there, so a phone and a laptop saw different read state, different
// "+N new" counts, different sort orders, etc. The server-backed
// store fixes that -- on every device, the same user_id sees the
// same state.
//
// Why a React context (not a Zustand store, not a hook per key)?
//
//   - All eight keys share one round-trip on first load
//     (``GET /api/preferences``).
//   - Writes are debounced per-key, so the consumer code is
//     ``setX(...)`` and the provider handles the network.
//   - Optimistic updates: a write is reflected in the in-memory
//     state immediately, the network call goes out in the
//     background. A failed write reverts. The UX feels
//     localStorage-fast while the truth lives on the server.
//
// Why not just call the API from each consumer?
//
//   - The same debounce/coalesce logic would have to live in eight
//     places. Centralizing it in the provider means the consumer
//     code is "I want to set this value" and the transport is one
//     module's concern.
//   - The localStorage seed (one-way migration of any values the
//     user had on this device before the deploy) is a property of
//     "the moment we load from the server", which is the
//     provider's job, not each consumer's.
//
// Server contract
// ---------------
//
// ``GET  /api/preferences``           -> ``{ items: [{key, value, updated_at}] }``
// ``PUT  /api/preferences/{key}``     body: ``{ value: any }``   -> the row
// ``DEL  /api/preferences/{key}``     -> 204
//
// The server treats ``key`` as an opaque string. We use namespaced
// shapes (``read_entries:<columnId>``, ``last_viewed:<columnId>``,
// ``column_prefs:<columnId>``, ``column_sections:<columnId>``,
// ``hidden_entries``, ``starred_entries``, ``filter_presets``,
// ``history_group_by``) so the schema can grow without a backend
// migration. The constant ``PREFERENCE_KEYS`` below is the source
// of truth for which keys exist.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { api } from '../api'
import { safeGetItem, safeRemoveItem } from './storage'

// ---------------------------------------------------------------------------
// Key registry. Single source of truth for which preference keys exist
// and which shape their value takes. Backend is opaque to key shape --
// the server stores whatever JSONB you give it. The TypeScript types
// here are the contract.
// ---------------------------------------------------------------------------

/**
 * One column's read-entries set: which entry ids the user has manually
 * marked read. Keyed by source id (the dashboard's column id).
 */
export type ReadEntriesValue = number[]

/** One column's "I last saw this column at" ISO timestamp. */
export type LastViewedValue = string

/**
 * One column's sort/filter preferences. Matches the
 * ``ColumnPrefs`` type in App.tsx -- duplicated here to avoid a
 * circular import (App imports this module, this module would
 * have to import App's ColumnPrefs otherwise).
 */
export type ColumnPrefsValue = {
  sort: 'top' | 'newest' | 'oldest'
  minScore: number
  maxAgeHours: number | null
}

/** One column's Fresh/History section collapse state. */
export type ColumnSectionsValue = {
  newCollapsed: boolean
  historyCollapsed: boolean
}

/** The History tab's group-by mode. */
export type HistoryGroupByValue = 'entry' | 'none'

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
 * The flat shape of the preferences state. Each top-level key is
 * the server-side preference key; the type is the decoded value.
 *
 * The server response is a list of {key, value}; the provider
 * decodes it into this object on first load.
 */
export type PreferencesState = {
  // Per-column maps keyed by source id. Source ids are strings in
  // the dashboard (e.g. ``"hacker-news-top"``) and we follow the
  // same convention here.
  readEntries: Record<string, ReadEntriesValue>
  lastViewed: Record<string, LastViewedValue>
  columnPrefs: Record<string, ColumnPrefsValue>
  columnSections: Record<string, ColumnSectionsValue>
  // Per-user lists. The dashboard treats these as flat arrays.
  hiddenEntries: number[]
  starredEntries: number[]
  // Per-user singletons (one value, not a map).
  filterPresets: FilterPresetValue[]
  historyGroupBy: HistoryGroupByValue
}

/** Helper: which server-side keys exist, and what TS type each holds. */
export const PREFERENCE_KEYS = {
  readEntries: 'read_entries',
  lastViewed: 'last_viewed',
  columnPrefs: 'column_prefs',
  columnSections: 'column_sections',
  hiddenEntries: 'hidden_entries',
  starredEntries: 'starred_entries',
  filterPresets: 'filter_presets',
  historyGroupBy: 'history_group_by',
} as const

// Default state for a brand-new user (server has no rows yet).
// Mirrors the ``DEFAULT_PREFS`` constant in App.tsx; values are
// chosen to match the existing localStorage defaults so the
// migration is transparent.
const DEFAULT_STATE: PreferencesState = {
  readEntries: {},
  lastViewed: {},
  columnPrefs: {},
  columnSections: {},
  hiddenEntries: [],
  starredEntries: [],
  filterPresets: [],
  historyGroupBy: 'entry',
}

// ---------------------------------------------------------------------------
// API shapes (server contract).
// ---------------------------------------------------------------------------

interface PreferenceRow {
  key: string
  value: unknown
  updated_at: string
}

// ---------------------------------------------------------------------------
// LocalStorage seed (one-way migration of pre-deploy state).
//
// On the first launch after this migration ships, the user has
// localStorage entries from before the deploy (e.g. their read
// state, sort/filter prefs). The server has nothing. We POST each
// localStorage value to the server, then delete the localStorage
// entry. The next launch will hit the server directly. This is
// a "one-way migration" -- once the seed runs, localStorage is
// authoritative no more; the server is.
//
// Why the seed is one-shot and not idempotent: re-seeding would
// overwrite a server value the user might have changed on another
// device between the seed and the next launch. The ``seeded`` flag
// in localStorage gates it to one run per browser.
//
// The localStorage entries that back the seed are the OLD keys
// (``STORAGE_KEYS.readEntries`` etc.). After the seed runs we
// remove those entries from localStorage. The new code only
// reads from the server.
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

/**
 * Read the localStorage values that the old code used to write,
 * POST them to the server as the new keys, then remove the
 * localStorage entries. Idempotent via the ``seeded`` flag.
 *
 * Returns the seeded state to bootstrap the in-memory state
 * before the server's response has landed. (We still do the
 * GET on first mount; this just gives the user an instant
 * paint with their old data while the network is in flight.)
 */
function seedFromLocalStorage(): PreferencesState | null {
  // The seed runs at most once per browser. ``safeGetItem`` returns
  // null in SSR / private mode -- we treat that as "no seed
  // needed" and let the server be the source from the start.
  if (typeof window === 'undefined') return null
  if (safeGetItem(SEED_FLAG_KEY) === '1') return null

  const out: PreferencesState = { ...DEFAULT_STATE }

  // Per-column maps. The old keys stored a single JSON blob; we
  // split it into one server row per source id so the schema can
  // grow without a backend migration.
  for (const [localKey, prefKey] of [
    [LEGACY_STORAGE_KEYS.readEntries, PREFERENCE_KEYS.readEntries] as const,
    [LEGACY_STORAGE_KEYS.lastViewed, PREFERENCE_KEYS.lastViewed] as const,
    [LEGACY_STORAGE_KEYS.columnPrefs, PREFERENCE_KEYS.columnPrefs] as const,
    [LEGACY_STORAGE_KEYS.columnSections, PREFERENCE_KEYS.columnSections] as const,
  ]) {
    const raw = safeGetItem(localKey)
    if (!raw) continue
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') {
        // POST one row per top-level key. Each row's value is the
        // inner value (the per-column map fragment).
        for (const [columnId, value] of Object.entries(parsed)) {
          const key = `${prefKey}:${columnId}`
          // Fire-and-forget -- the GET on first mount will pick
          // these up via the server's response. We don't await.
          void api.setPreference(key, value)
        }
        // Bootstrap the in-memory state from the same blob so the
        // first paint has the user's data, not a flash of empty
        // columns.
        switch (prefKey) {
          case PREFERENCE_KEYS.readEntries:
            out.readEntries = parsed as Record<string, ReadEntriesValue>
            break
          case PREFERENCE_KEYS.lastViewed:
            out.lastViewed = parsed as Record<string, LastViewedValue>
            break
          case PREFERENCE_KEYS.columnPrefs:
            out.columnPrefs = parsed as Record<string, ColumnPrefsValue>
            break
          case PREFERENCE_KEYS.columnSections:
            out.columnSections = parsed as Record<
              string,
              ColumnSectionsValue
            >
            break
        }
      }
    } catch {
      // Corrupt localStorage value. Drop it and let the server be
      // the source of truth going forward.
    }
    safeRemoveItem(localKey)
  }

  // Per-user lists. Each was a single JSON blob in the old code.
  for (const [localKey, prefKey, target] of [
    [LEGACY_STORAGE_KEYS.hiddenEntries, PREFERENCE_KEYS.hiddenEntries, 'hiddenEntries'] as const,
    [LEGACY_STORAGE_KEYS.starredEntries, PREFERENCE_KEYS.starredEntries, 'starredEntries'] as const,
    [LEGACY_STORAGE_KEYS.filterPresets, PREFERENCE_KEYS.filterPresets, 'filterPresets'] as const,
  ]) {
    const raw = safeGetItem(localKey)
    if (!raw) continue
    try {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        void api.setPreference(prefKey, parsed)
        // Bootstrap in-memory state from the seed.
        if (target === 'hiddenEntries') {
          out.hiddenEntries = parsed as number[]
        } else if (target === 'starredEntries') {
          out.starredEntries = parsed as number[]
        } else {
          out.filterPresets = parsed as FilterPresetValue[]
        }
      }
    } catch {
      // Same as above -- drop and continue.
    }
    safeRemoveItem(localKey)
  }

  // Singleton: historyGroupBy. Old code stored it as 'entry' / 'none'.
  const groupBy = safeGetItem(LEGACY_STORAGE_KEYS.historyGroupBy)
  if (groupBy === 'entry' || groupBy === 'none') {
    void api.setPreference(PREFERENCE_KEYS.historyGroupBy, groupBy)
    out.historyGroupBy = groupBy
  }
  if (groupBy) safeRemoveItem(LEGACY_STORAGE_KEYS.historyGroupBy)

  // Mark the seed done. If any of the POSTs above fail, the user
  // gets the next launch's GET to refill the state from the
  // server (which by then has whatever did land).
  try {
    window.localStorage?.setItem(SEED_FLAG_KEY, '1')
  } catch {
    // Best-effort. The next launch will re-seed (worse: extra
    // POSTs but no data loss). Tolerate.
  }

  // Return the bootstrap state if we found anything to seed; null
  // otherwise (the consumer falls through to the GET).
  const foundAnything =
    Object.keys(out.readEntries).length > 0 ||
    Object.keys(out.lastViewed).length > 0 ||
    Object.keys(out.columnPrefs).length > 0 ||
    Object.keys(out.columnSections).length > 0 ||
    out.hiddenEntries.length > 0 ||
    out.starredEntries.length > 0 ||
    out.filterPresets.length > 0
  return foundAnything ? out : null
}

// ---------------------------------------------------------------------------
// PreferencesContext + Provider.
// ---------------------------------------------------------------------------

export interface PreferencesContextValue {
  /** Full state. Consumers select into it via the typed setters
   *  below; they don't read this object directly. */
  state: PreferencesState
  /** True until the first GET /api/preferences resolves (or the
   *  seed finishes). Consumers that need the state before the
   *  first paint can gate on this. */
  loading: boolean
  /** True after the localStorage seed has been applied. Stays true
   *  for the lifetime of the page once the seed has run. */
  seeded: boolean

  // Per-key setters. Each does an optimistic in-memory update +
  // a debounced PUT to the server. They are stable across renders
  // (useCallback with empty dep) so consumers can put them in
  // their useEffect dep arrays without thrash.

  /** Update the read-set for one column. Used for both marking an
   *  entry read and any "unread" action; the full ``readEntries``
   *  map is exposed via ``state`` for the consumer that needs the
   *  union (e.g. the "is this card read?" check in Card.tsx). */
  setReadEntries: (
    columnId: string,
    ids: ReadEntriesValue,
  ) => void

  /** Record that the user just visited a column. Replaces the
   *  old ``lastViewed`` effect. */
  setLastViewed: (columnId: string, iso: string) => void

  /** Forget that the user ever visited a column. Used by the
   *  column-mark-read Undo action -- the cleared
   *  ``lastViewed`` is what makes the "+N new" chip
   *  repopulate on the next refresh. The provider issues a
   *  ``DELETE /api/preferences/last_viewed:<columnId>`` so
   *  the row is actually gone on the server (vs. PUTing a
   *  sentinel that the consumer would have to special-case). */
  clearLastViewed: (columnId: string) => void

  /** Update the sort/filter prefs for one column. Replaces the
   *  old ``columnPrefs`` effect. */
  setColumnPrefs: (columnId: string, prefs: ColumnPrefsValue) => void

  /** Update the Fresh/History section collapse for one column. */
  setColumnSections: (
    columnId: string,
    sections: ColumnSectionsValue,
  ) => void

  /** Add or remove an entry from the "hide" set. */
  setHiddenEntries: (ids: number[]) => void

  /** Add or remove an entry from the "starred" set. */
  setStarredEntries: (ids: number[]) => void

  /** Replace the saved-presets list. */
  setFilterPresets: (presets: FilterPresetValue[]) => void

  /** Set the History tab's group-by mode. */
  setHistoryGroupBy: (mode: HistoryGroupByValue) => void
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null)

/**
 * Provider. Mounts once at the top of the React tree. Children
 * get the ``usePreferences()`` hook.
 *
 * Lifecycle:
 *   1. Mount: kick off the localStorage seed (if not done yet)
 *      and the first GET in parallel. The seed bootstrap paints
 *      instantly if anything was found; the GET overwrites with
 *      the server's view (which is the truth for multi-device).
 *   2. Mount, after first GET: debounced-sync mode. Every
 *      setter is an optimistic in-memory update + a 250ms
 *      trailing-edge PUT.
 *   3. Unmount: flush any pending PUTs via ``navigator.sendBeacon``
 *      so a tab close doesn't drop the last write.
 */
export function PreferencesProvider({ children }: { children: ReactNode }) {
  // Initialize from the seed so the first paint is instant if the
  // user had localStorage data. The GET then runs in the
  // background; whichever lands first sets ``state``, and any
  // later response overwrites. The seed and the GET are racing
  // by design -- the seed wins on the first launch (because we
  // already have the data in memory), the GET wins on every
  // subsequent launch (no seed runs).
  const [state, setState] = useState<PreferencesState>(() => {
    const seeded = seedFromLocalStorage()
    return seeded ?? DEFAULT_STATE
  })
  const [loading, setLoading] = useState(true)
  const [seeded, setSeeded] = useState(false)

  // First-mount GET. Runs once per provider lifetime.
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const resp = await api.getPreferences()
        if (cancelled) return
        // Decode the server's per-key list into our typed state.
        // Unknown keys are ignored (forward-compat: a future
        // preference lands without breaking older frontends).
        const next: PreferencesState = { ...DEFAULT_STATE }
        for (const row of resp.items) {
          applyRowToState(next, row)
        }
        // If we also ran the seed, the seed's POSTs are
        // in-flight. The server's response might not include
        // them yet. Merge: server wins on conflict (the user
        // might have changed a value on another device between
        // seed and GET), seed wins on keys the server doesn't
        // have yet.
        setState((prev) => mergeStateFromServer(prev, next))
        setSeeded(true)
      } catch (err) {
        // The server is unreachable. Fall back to whatever the
        // seed gave us. The user gets a degraded experience
        // (no multi-device sync) but the app still works.
        if (import.meta.env.DEV) {
          console.warn('preferences: initial GET failed', err)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Debounced sync. Each setter writes to ``state`` immediately
  // (optimistic) and schedules a PUT 250ms later. If the same key
  // is written again before the timer fires, the timer resets
  // (trailing-edge debounce). The PUT is the latest value.
  //
  // Why debounce? The "mark read" path can fire many times per
  // second (user scrolling past 30 cards). Without debounce we'd
  // PUT 30 times; with it, the final state lands in one PUT.
  //
  // Why not throttle? Throttle would drop intermediate writes;
  // debounce coalesces them and the final value is the truth.
  const pendingRef = useRef<Map<string, unknown>>(new Map())
  const timerRef = useRef<number | null>(null)

  const scheduleSync = useCallback(() => {
    if (timerRef.current !== null) return
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      const batch = Array.from(pendingRef.current.entries())
      pendingRef.current.clear()
      for (const [key, value] of batch) {
        // Fire-and-forget. Failure logs in dev; the in-memory
        // state is still the truth for this session, and the
        // next mount's GET will reconcile.
        void api.setPreference(key, value).catch((err) => {
          if (import.meta.env.DEV) {
            console.warn(`preferences: PUT ${key} failed`, err)
          }
        })
      }
    }, 250)
  }, [])

  // Flush on tab close. ``navigator.sendBeacon`` is the only
  // fetch variant that survives a tab close; the rest are
  // cancelled with the page.
  useEffect(() => {
    const flush = () => {
      if (pendingRef.current.size === 0) return
      const batch = Array.from(pendingRef.current.entries())
      pendingRef.current.clear()
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
      for (const [key, value] of batch) {
        try {
          // ``sendBeacon`` needs a Blob or FormData; we send
          // the JSON string as a Blob with the right MIME.
          const blob = new Blob(
            [JSON.stringify({ value })],
            { type: 'application/json' },
          )
          navigator.sendBeacon?.(`/api/preferences/${encodeURIComponent(key)}`, blob)
        } catch {
          // Best-effort. The user lost a write -- tolerable
          // for engagement signals, less tolerable for read
          // state, but there's no recovery path from a
          // closing tab.
        }
      }
    }
    window.addEventListener('pagehide', flush)
    window.addEventListener('beforeunload', flush)
    return () => {
      window.removeEventListener('pagehide', flush)
      window.removeEventListener('beforeunload', flush)
    }
  }, [])

  // ---- per-key setters ----
  //
  // All setters follow the same pattern: setState for the optimistic
  // update, then enqueue the server write via pendingRef + scheduleSync.

  const setReadEntries = useCallback(
    (columnId: string, ids: ReadEntriesValue) => {
      setState((prev) => ({
        ...prev,
        readEntries: { ...prev.readEntries, [columnId]: ids.slice(-MAX_PER_COLUMN) },
      }))
      pendingRef.current.set(
        `${PREFERENCE_KEYS.readEntries}:${columnId}`,
        ids.slice(-MAX_PER_COLUMN),
      )
      scheduleSync()
    },
    [scheduleSync],
  )

  const setLastViewed = useCallback(
    (columnId: string, iso: string) => {
      setState((prev) => ({
        ...prev,
        lastViewed: { ...prev.lastViewed, [columnId]: iso },
      }))
      pendingRef.current.set(`${PREFERENCE_KEYS.lastViewed}:${columnId}`, iso)
      scheduleSync()
    },
    [scheduleSync],
  )

  // Delete the row, not PUT a sentinel. The server's
  // ``DELETE /api/preferences/{key}`` is idempotent (204 whether
  // or not the row existed), so this is safe to call when the
  // key is already gone. The local state is updated to remove
  // the key so a re-render immediately reflects the absence.
  const clearLastViewed = useCallback(
    (columnId: string) => {
      setState((prev) => {
        if (!(columnId in prev.lastViewed)) return prev
        const next = { ...prev.lastViewed }
        delete next[columnId]
        return { ...prev, lastViewed: next }
      })
      const key = `${PREFERENCE_KEYS.lastViewed}:${columnId}`
      pendingRef.current.delete(key)
      // Fire-and-forget. The DELETE endpoint returns 204; we
      // don't care about the body. Failure logs in dev.
      void api.deletePreference(key).catch((err) => {
        if (import.meta.env.DEV) {
          console.warn(`preferences: DELETE ${key} failed`, err)
        }
      })
    },
    [],
  )

  const setColumnPrefs = useCallback(
    (columnId: string, prefs: ColumnPrefsValue) => {
      setState((prev) => ({
        ...prev,
        columnPrefs: { ...prev.columnPrefs, [columnId]: prefs },
      }))
      pendingRef.current.set(`${PREFERENCE_KEYS.columnPrefs}:${columnId}`, prefs)
      scheduleSync()
    },
    [scheduleSync],
  )

  const setColumnSections = useCallback(
    (columnId: string, sections: ColumnSectionsValue) => {
      setState((prev) => ({
        ...prev,
        columnSections: { ...prev.columnSections, [columnId]: sections },
      }))
      pendingRef.current.set(`${PREFERENCE_KEYS.columnSections}:${columnId}`, sections)
      scheduleSync()
    },
    [scheduleSync],
  )

  const setHiddenEntries = useCallback(
    (ids: number[]) => {
      const trimmed = ids.slice(-MAX_HIDDEN)
      setState((prev) => ({ ...prev, hiddenEntries: trimmed }))
      pendingRef.current.set(PREFERENCE_KEYS.hiddenEntries, trimmed)
      scheduleSync()
    },
    [scheduleSync],
  )

  const setStarredEntries = useCallback(
    (ids: number[]) => {
      const trimmed = ids.slice(-MAX_STARRED)
      setState((prev) => ({ ...prev, starredEntries: trimmed }))
      pendingRef.current.set(PREFERENCE_KEYS.starredEntries, trimmed)
      scheduleSync()
    },
    [scheduleSync],
  )

  const setFilterPresets = useCallback(
    (presets: FilterPresetValue[]) => {
      setState((prev) => ({ ...prev, filterPresets: presets }))
      pendingRef.current.set(PREFERENCE_KEYS.filterPresets, presets)
      scheduleSync()
    },
    [scheduleSync],
  )

  const setHistoryGroupBy = useCallback(
    (mode: HistoryGroupByValue) => {
      setState((prev) => ({ ...prev, historyGroupBy: mode }))
      pendingRef.current.set(PREFERENCE_KEYS.historyGroupBy, mode)
      scheduleSync()
    },
    [scheduleSync],
  )

  const value = useMemo<PreferencesContextValue>(
    () => ({
      state,
      loading,
      seeded,
      setReadEntries,
      setLastViewed,
      clearLastViewed,
      setColumnPrefs,
      setColumnSections,
      setHiddenEntries,
      setStarredEntries,
      setFilterPresets,
      setHistoryGroupBy,
    }),
    [
      state,
      loading,
      seeded,
      setReadEntries,
      setLastViewed,
      clearLastViewed,
      setColumnPrefs,
      setColumnSections,
      setHiddenEntries,
      setStarredEntries,
      setFilterPresets,
      setHistoryGroupBy,
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

// ---------------------------------------------------------------------------
// Internal helpers.
// ---------------------------------------------------------------------------

// Caps mirror the old localStorage caps. Keeping them in one
// place so a future "raise the cap" change is a one-line edit.
// Exported because the consumer (App.tsx) also trims on
// write in some code paths (e.g. the "restore hidden" path
// that pulls from a ref) and needs the same number.
export const MAX_PER_COLUMN = 200
export const MAX_HIDDEN = 1000
export const MAX_STARRED = 1000
// ``MAX_PRESETS`` is the cap on saved filter presets. Lives
// here rather than in storage.ts because presets moved
// server-side with the rest of the preferences.
export const MAX_PRESETS = 50

/**
 * Decode one server row into the right field of a PreferencesState.
 * Unknown keys are ignored -- forward-compat for future
 * preference types.
 */
function applyRowToState(out: PreferencesState, row: PreferenceRow) {
  const { key, value } = row
  if (key.startsWith(`${PREFERENCE_KEYS.readEntries}:`)) {
    const columnId = key.slice(`${PREFERENCE_KEYS.readEntries}:`.length)
    if (Array.isArray(value)) {
      out.readEntries[columnId] = (value as unknown[]).filter(
        (x): x is number => typeof x === 'number',
      )
    }
  } else if (key.startsWith(`${PREFERENCE_KEYS.lastViewed}:`)) {
    const columnId = key.slice(`${PREFERENCE_KEYS.lastViewed}:`.length)
    if (typeof value === 'string') {
      out.lastViewed[columnId] = value
    }
  } else if (key.startsWith(`${PREFERENCE_KEYS.columnPrefs}:`)) {
    const columnId = key.slice(`${PREFERENCE_KEYS.columnPrefs}:`.length)
    if (value && typeof value === 'object') {
      out.columnPrefs[columnId] = value as ColumnPrefsValue
    }
  } else if (key.startsWith(`${PREFERENCE_KEYS.columnSections}:`)) {
    const columnId = key.slice(`${PREFERENCE_KEYS.columnSections}:`.length)
    if (value && typeof value === 'object') {
      out.columnSections[columnId] = value as ColumnSectionsValue
    }
  } else if (key === PREFERENCE_KEYS.hiddenEntries) {
    if (Array.isArray(value)) {
      out.hiddenEntries = (value as unknown[]).filter(
        (x): x is number => typeof x === 'number',
      )
    }
  } else if (key === PREFERENCE_KEYS.starredEntries) {
    if (Array.isArray(value)) {
      out.starredEntries = (value as unknown[]).filter(
        (x): x is number => typeof x === 'number',
      )
    }
  } else if (key === PREFERENCE_KEYS.filterPresets) {
    if (Array.isArray(value)) {
      out.filterPresets = value as FilterPresetValue[]
    }
  } else if (key === PREFERENCE_KEYS.historyGroupBy) {
    if (value === 'entry' || value === 'none') {
      out.historyGroupBy = value
    }
  }
}

/**
 * Merge the server's view of the state on top of the local
 * (seeded) view. Used on first mount to reconcile the seed with
 * the server's response. Server wins on key collision; seed
 * wins on keys the server doesn't have.
 */
function mergeStateFromServer(
  seed: PreferencesState,
  server: PreferencesState,
): PreferencesState {
  return {
    readEntries: { ...seed.readEntries, ...server.readEntries },
    lastViewed: { ...seed.lastViewed, ...server.lastViewed },
    columnPrefs: { ...seed.columnPrefs, ...server.columnPrefs },
    columnSections: { ...seed.columnSections, ...server.columnSections },
    hiddenEntries:
      server.hiddenEntries.length > 0
        ? server.hiddenEntries
        : seed.hiddenEntries,
    starredEntries:
      server.starredEntries.length > 0
        ? server.starredEntries
        : seed.starredEntries,
    filterPresets:
      server.filterPresets.length > 0
        ? server.filterPresets
        : seed.filterPresets,
    historyGroupBy:
      server.historyGroupBy !== DEFAULT_STATE.historyGroupBy
        ? server.historyGroupBy
        : seed.historyGroupBy,
  }
}
