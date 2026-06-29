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
import { Column, DEFAULT_PREFS, type ColumnPrefs } from './components/Column'
import { Drawer } from './components/Drawer'
import { Hamburger } from './components/Hamburger'
import { LoginPage } from './components/LoginPage'
import { SearchResults } from './components/SearchResults'
import { ToastHost } from './components/Toast'
import { UserBadge } from './components/UserBadge'

const REFRESH_INTERVAL_MS = 60_000
// Hidden longer than this → treat return as "fresh start"; the new-
// entry indicator resets and all entries surface as unread. Without
// this, returning from a long absence shows nothing flagged (because
// the seen-set still has the old ids).
const HIDDEN_RESET_MS = 2 * 60 * 1000

// localStorage keys. Dotted namespace matches BriefCard's existing
// ``brief.collapsed`` convention — each feature owns its own subtree.
const LS_LAST_VIEWED = 'col.lastViewed'
const LS_COLUMN_PREFS = 'col.prefs'
const LS_MOBILE_COL = 'mobileCol.last'

function loadLastViewed(): Record<string, string> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage?.getItem(LS_LAST_VIEWED)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, string>) : {}
  } catch {
    return {}
  }
}

function loadColumnPrefs(): Record<string, ColumnPrefs> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage?.getItem(LS_COLUMN_PREFS)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const out: Record<string, ColumnPrefs> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (v && typeof v === 'object' && 'sort' in v) {
        out[k] = { ...DEFAULT_PREFS, ...(v as Partial<ColumnPrefs>) }
      }
    }
    return out
  } catch {
    return {}
  }
}

function loadMobileCol(): number {
  if (typeof window === 'undefined') return 0
  try {
    const raw = window.localStorage?.getItem(LS_MOBILE_COL)
    if (!raw) return 0
    const n = Number(raw)
    return Number.isInteger(n) && n >= 0 ? n : 0
  } catch {
    return 0
  }
}

function safeSetItem(key: string, value: string) {
  try {
    window.localStorage?.setItem(key, value)
  } catch {
    // Quota / private-mode — the in-memory state is the source of
    // truth for the current session.
  }
}

export function App() {
  const [entries, setEntries] = useState<Entry[]>([])
  const [forYou, setForYou] = useState<Entry[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
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
  // Refresh in-flight state — drives the Refresh button's disabled
  // state so a second tap doesn't fire a parallel fetch.
  const [refreshing, setRefreshing] = useState(false)
  // Per-column last-viewed timestamps.
  const [lastViewed, setLastViewed] = useState<Record<string, string>>(loadLastViewed)
  // Per-column sort/filter preferences.
  const [columnPrefs, setColumnPrefs] = useState<Record<string, ColumnPrefs>>(loadColumnPrefs)
  // Search state. ``searchInput`` is the controlled input value;
  // ``searchQuery`` is the debounced value used for the fetch.
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Entry[]>([])
  const [searching, setSearching] = useState(false)
  // Search-bar expansion. The header collapses search into a single
  // magnifying-glass icon by default; tapping it expands the input
  // to full width (matching Apple Mail's header behaviour). ``false``
  // on first mount so the large title gets the full row.
  const [searchOpen, setSearchOpen] = useState(false)
  // Keyboard selection.
  const [selectedColumnIndex, setSelectedColumnIndex] = useState(0)
  const [selectedCardId, setSelectedCardId] = useState<number | null>(null)

  const touchStartX = useRef<number | null>(null)
  // Set of entry ids observed on the previous successful refresh.
  // Used to compute "new since last refresh" — entries whose id is
  // not in this set are flagged. ``null`` means "haven't completed a
  // refresh yet" — the new-entry indicator stays hidden until the
  // first refresh lands so the initial dashboard load doesn't flag
  // every entry as new.
  const seenEntryIdsRef = useRef<Set<number> | null>(null)
  // When the tab was last hidden. ``null`` while visible.
  const hiddenAtRef = useRef<number | null>(null)
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

  const byCategory = useMemo(() => {
    const grouped = new Map<string, Entry[]>()
    for (const e of entries) {
      const sourceName = sourcesById.get(e.source_id)
      const src = sourceName != null ? sourcesByName.get(sourceName) : undefined
      const cat = src?.category ?? 'other'
      const arr = grouped.get(cat) ?? []
      arr.push(e)
      grouped.set(cat, arr)
    }
    return grouped
  }, [entries, sourcesByName, sourcesById])

  const categories = useMemo(() => Array.from(byCategory.keys()).sort(), [byCategory])

  const baseColumns = useMemo<Array<{ name: string; entries: Entry[] }>>(() => {
    const out: Array<{ name: string; entries: Entry[] }> = []
    if (forYou.length > 0) out.push({ name: 'For You', entries: forYou })
    for (const cat of categories) {
      out.push({ name: cat, entries: byCategory.get(cat) ?? [] })
    }
    return out
  }, [forYou, categories, byCategory])

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

  // New-entry per column. An entry is "new" when its id isn't in the
  // seen-set from the previous refresh AND/OR it's newer than the
  // user's last visit to this column. Union of both — anything
  // unacknowledged surfaces.
  const newCountByColumn = useMemo(() => {
    const out = new Map<string, number>()
    const seen = seenEntryIdsRef.current
    for (const col of columns) {
      const last = lastViewed[col.name]
      const lastMs = last ? new Date(last).getTime() : 0
      let n = 0
      for (const e of col.entries) {
        const isNewSinceRefresh = seen != null && !seen.has(e.id)
        const isNewSinceVisit =
          lastMs > 0 && e.fetched_at != null && new Date(e.fetched_at).getTime() > lastMs
        if (isNewSinceRefresh || isNewSinceVisit) n++
      }
      if (n > 0) out.set(col.name, n)
    }
    return out
  }, [columns, lastViewed])

  // Unread entry ids per column — used to dim read cards.
  const unreadIdsByColumn = useMemo(() => {
    const out = new Map<string, Set<number>>()
    for (const col of columns) {
      const last = lastViewed[col.name]
      if (!last) continue
      const lastMs = new Date(last).getTime()
      const ids = new Set<number>()
      for (const e of col.entries) {
        if (e.fetched_at && new Date(e.fetched_at).getTime() > lastMs) {
          ids.add(e.id)
        }
      }
      if (ids.size > 0) out.set(col.name, ids)
    }
    return out
  }, [columns, lastViewed])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const sourceArg = activeSources.size > 0 ? Array.from(activeSources) : undefined
      const [e, s, h, fy] = await Promise.all([
        api.entries({ limit: 200, source: sourceArg }),
        api.sources(),
        api.health(),
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
      setHealth(h)
      setForYou(fy)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setRefreshing(false)
    }
  }, [activeSources])

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
    void refresh()
    if (document.visibilityState === 'visible') startPolling()

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      stopPolling()
    }
  }, [refresh, authProbed])

  // Keep ``mobileCol`` in bounds.
  useEffect(() => {
    if (columns.length === 0) return
    if (mobileCol >= columns.length) {
      const next = columns.length - 1
      setMobileCol(next)
      safeSetItem(LS_MOBILE_COL, String(next))
    }
  }, [columns.length, mobileCol])

  useEffect(() => {
    safeSetItem(LS_MOBILE_COL, String(mobileCol))
  }, [mobileCol])

  useEffect(() => {
    safeSetItem(LS_LAST_VIEWED, JSON.stringify(lastViewed))
  }, [lastViewed])

  useEffect(() => {
    safeSetItem(LS_COLUMN_PREFS, JSON.stringify(columnPrefs))
  }, [columnPrefs])

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
      return
    }
    setSearching(true)
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
      })
      .catch(() => {
        if (cancelled) return
        setSearchResults([])
      })
      .finally(() => {
        if (!cancelled) setSearching(false)
      })
    return () => {
      cancelled = true
    }
  }, [searchQuery])

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
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [columns, selectedColumnIndex, selectedCardId, searchQuery])

  // Scroll the keyboard-selected card into view.
  useEffect(() => {
    if (selectedCardId == null) return
    const el = cardRefs.current.get(selectedCardId)
    if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [selectedCardId])

  const onTouchStart = (e: React.TouchEvent) => {
    touchStartX.current = e.touches[0].clientX
  }
  const onTouchEnd = (e: React.TouchEvent) => {
    if (touchStartX.current == null) return
    const delta = e.changedTouches[0].clientX - touchStartX.current
    touchStartX.current = null
    if (Math.abs(delta) < 60) return
    if (delta < 0) setMobileCol((i) => Math.min(i + 1, Math.max(columns.length - 1, 0)))
    else setMobileCol((i) => Math.max(i - 1, 0))
  }

  const markColumnRead = (columnName: string) => {
    const col = columns.find((c) => c.name === columnName)
    setLastViewed((prev) => ({ ...prev, [columnName]: new Date().toISOString() }))
    if (col && seenEntryIdsRef.current != null) {
      const merged = new Set(seenEntryIdsRef.current)
      for (const e of col.entries) merged.add(e.id)
      seenEntryIdsRef.current = merged
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
    setColumnPrefs((prev) => ({ ...prev, [columnName]: prefs }))
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
    return <div className="h-full" />
  }

  if (!oidcDisabled && user === null) {
    return <LoginPage returnTo="/" onSignedIn={setUser} />
  }

  const showSearchView = searchQuery.trim().length > 0

  return (
    <div className="h-full flex flex-col">
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
            {/* Hamburger lives next to the search affordance — both
                are 44×44 tappable targets that match iOS nav-bar
                icon-button conventions. On ``sm+`` it appears at the
                right edge of the title row. */}
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
              onClick={async () => {
                if (generatingBrief) return
                setGeneratingBrief(true)
                try {
                  const next = await api.briefGenerate(briefTone)
                  setBrief(next)
                } catch (err) {
                  setError((err as Error).message)
                } finally {
                  setGeneratingBrief(false)
                }
              }}
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
        <div className="px-4 py-2 bg-red-500/15 border-b border-red-500/40 text-ios-caption text-red-200">
          {error}
        </div>
      )}

      <BriefCard
        brief={brief}
        onBriefChange={setBrief}
        tone={briefTone}
        onToneChange={setBriefTone}
      />

      {showSearchView ? (
        <main className="flex-1 overflow-y-auto p-3 sm:p-4">
          <SearchResults
            query={searchQuery}
            entries={searchResults}
            sourcesById={sourcesById}
          />
          {searching && (
            <p className="mt-2 text-ios-caption text-label-secondary px-1">searching…</p>
          )}
        </main>
      ) : columns.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-ios-body text-label-secondary px-4 text-center">
          no entries yet — the scheduler will fetch the first batch shortly, or hit Refresh
        </div>
      ) : (
        <>
          <main className="hidden md:grid md:grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 p-4 flex-1 overflow-y-auto">
            {columns.map((col, ci) => (
              <div key={col.name} ref={setColumnRef(col.name)} className="contents">
                <Column
                  name={col.name}
                  entries={col.entries}
                  sourcesById={sourcesById}
                  newCount={newCountByColumn.get(col.name)}
                  unreadIds={unreadIdsByColumn.get(col.name)}
                  selectedId={ci === selectedColumnIndex ? selectedCardId ?? undefined : undefined}
                  cardRefs={cardRefs}
                  onMarkRead={() => markColumnRead(col.name)}
                  prefs={columnPrefs[col.name] ?? DEFAULT_PREFS}
                  onPrefsChange={(next) => setPrefsFor(col.name, next)}
                  totalCount={col.totalCount}
                  categoriesBySourceId={categoriesBySourceId}
                />
              </div>
            ))}
          </main>

          <main
            className="md:hidden flex-1 overflow-hidden p-3"
            onTouchStart={onTouchStart}
            onTouchEnd={onTouchEnd}
          >
            <Column
              name={columns[mobileCol]?.name ?? ''}
              entries={columns[mobileCol]?.entries ?? []}
              sourcesById={sourcesById}
              newCount={newCountByColumn.get(columns[mobileCol]?.name ?? '')}
              unreadIds={unreadIdsByColumn.get(columns[mobileCol]?.name ?? '')}
              selectedId={
                mobileCol === selectedColumnIndex ? selectedCardId ?? undefined : undefined
              }
              cardRefs={cardRefs}
              onMarkRead={() => markColumnRead(columns[mobileCol]?.name ?? '')}
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
            />
            {columns.length > 1 && (
              <div className="flex justify-center gap-1 mt-2">
                {columns.map((c, i) => (
                  <button
                    key={c.name}
                    onClick={() => {
                      setMobileCol(i)
                      markColumnRead(c.name)
                    }}
                    aria-label={`go to column ${c.name}`}
                    className={`h-2 w-2 rounded-full transition ${
                      i === mobileCol ? 'bg-label-primary w-4' : 'bg-label-tertiary'
                    }`}
                  />
                ))}
              </div>
            )}
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
        briefTone={briefTone}
        onBriefToneChange={setBriefTone}
        onError={setError}
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