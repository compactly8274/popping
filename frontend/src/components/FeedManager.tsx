// Phase 5 FeedManager — Drawer section for adding, editing, and
// removing dynamic RSS feeds. Three tabs:
//
//   • My feeds — the existing source list, with per-row actions:
//     toggle active, edit refresh interval, delete (dynamic rows
//     only). Plugin-backed rows show the actions disabled / hidden.
//
//   • Recommended — curated list from GET /api/feed-recommendations,
//     minus anything the user already has. Tap "Add" to fire
//     POST /api/sources.
//
//   • Add custom — name + URL + category form for an RSS URL not in
//     the curated list. Client-side URL validation; the backend is
//     the source of truth on errors and returns 422 with field-level
//     details.
//
// Errors flow up to App's red error banner via `onError` so the
// visual treatment matches refresh failures — single, consistent
// error surface across the dashboard.

import { useEffect, useState } from 'react'
import { api, type Source } from '../api'

type Props = {
  sources: Source[]
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
}

type Tab = 'mine' | 'recommended' | 'add'

// Refresh interval presets the user picks from. Values are seconds;
// the dropdown renders the human label. Keeps the UI to a single
// choice rather than a freeform input — typos would silently break
// scheduling otherwise.
const REFRESH_PRESETS: Array<{ value: number; label: string }> = [
  { value: 900,   label: '15 min' },
  { value: 3600,  label: '1 hour' },
  { value: 21600, label: '6 hours' },
  { value: 86400, label: '24 hours' },
]

// Categories the user can pick from when adding a custom source.
// Existing source categories + a couple of common extras the
// recommendations list uses. Free-form text is also accepted in the
// backend (the column is just a 40-char string), but a dropdown is
// friendlier than asking the user to type "tech" vs "news" — and it
// matches existing column groupings.
const CATEGORY_OPTIONS = [
  'news',
  'tech',
  'vulns',
  'science',
  'finance',
  'policy',
  'longform',
  'deals',
  'other',
]

// Names of the registered plugin sources (BBC, HN, GitHub Releases,
// NVD, CISA, Wikipedia OTD). These rows are managed by the scheduler
// at import time and are not user-deletable — the UI hides the
// delete affordance and the backend rejects DELETE with a 400.
//
// We derive this list client-side by hitting /api/sources on mount
// and looking up each row against `registeredPluginNames`. The
// backend is the source of truth via the 400 response on DELETE, so
// a stale client can't accidentally delete a built-in.
const KNOWN_BUILT_INS = new Set([
  'bbc_news',
  'hn_top',
  'github_releases',
  'wikipedia_on_this_day',
  'nvd_recent',
  'cisa_kev',
])

function isBuiltIn(source: Source): boolean {
  return KNOWN_BUILT_INS.has(source.name)
}

function refreshLabel(seconds: number): string {
  const preset = REFRESH_PRESETS.find((p) => p.value === seconds)
  if (preset) return preset.label
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`
  return `${Math.round(seconds / 86400)} d`
}

function parseApiError(err: unknown, fallback: string): string {
  // FastAPI returns 422 with `{detail: [{loc: [...], msg: "..."}, ...]}`.
  // The message field is the most useful bit; flatten to a single string.
  const e = err as { message?: string; detail?: unknown }
  if (typeof e.message === 'string') return e.message
  if (Array.isArray(e.detail)) {
    return e.detail
      .map((d: any) => (d && typeof d.msg === 'string' ? d.msg : JSON.stringify(d)))
      .join('; ')
  }
  if (typeof e.detail === 'string') return e.detail
  return fallback
}

export function FeedManager({ sources, onRefresh, onError }: Props) {
  const [tab, setTab] = useState<Tab>('mine')
  const [editingInterval, setEditingInterval] = useState<number | null>(null)

  return (
    <div className="space-y-2">
      <div className="flex gap-1" role="tablist" aria-label="feed manager tabs">
        <TabButton active={tab === 'mine'} onClick={() => setTab('mine')}>
          My feeds ({sources.length})
        </TabButton>
        <TabButton active={tab === 'recommended'} onClick={() => setTab('recommended')}>
          Recommended
        </TabButton>
        <TabButton active={tab === 'add'} onClick={() => setTab('add')}>
          Add custom
        </TabButton>
      </div>

      {tab === 'mine' && (
        <MyFeedsTab
          sources={sources}
          editingInterval={editingInterval}
          setEditingInterval={setEditingInterval}
          onRefresh={onRefresh}
          onError={onError}
        />
      )}
      {tab === 'recommended' && (
        <RecommendedTab
          existingNames={new Set(sources.map((s) => s.name))}
          onAdded={onRefresh}
          onError={onError}
        />
      )}
      {tab === 'add' && (
        <AddCustomTab onAdded={onRefresh} onError={onError} />
      )}
    </div>
  )
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`flex-1 rounded px-2 py-1 text-[10px] uppercase tracking-wide transition ${
        active
          ? 'bg-blue-700 text-white'
          : 'bg-slate-800 text-slate-300 [@media(hover:hover)]:hover:bg-slate-700'
      }`}
    >
      {children}
    </button>
  )
}

// ---------------------------------------------------------------------------
// My feeds — existing source list with per-row actions
// ---------------------------------------------------------------------------

function MyFeedsTab({
  sources,
  editingInterval,
  setEditingInterval,
  onRefresh,
  onError,
}: {
  sources: Source[]
  editingInterval: number | null
  setEditingInterval: (id: number | null) => void
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
}) {
  if (sources.length === 0) {
    return <p className="text-xs text-slate-500 italic px-1">no sources yet</p>
  }
  return (
    <ul className="space-y-1">
      {sources.map((s) => (
        <li key={s.id}>
          <SourceRow
            source={s}
            isEditingInterval={editingInterval === s.id}
            onStartEditInterval={() => setEditingInterval(s.id)}
            onCancelEditInterval={() => setEditingInterval(null)}
            onRefresh={onRefresh}
            onError={onError}
          />
        </li>
      ))}
    </ul>
  )
}

function SourceRow({
  source,
  isEditingInterval,
  onStartEditInterval,
  onCancelEditInterval,
  onRefresh,
  onError,
}: {
  source: Source
  isEditingInterval: boolean
  onStartEditInterval: () => void
  onCancelEditInterval: () => void
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
}) {
  const builtIn = isBuiltIn(source)
  const [busy, setBusy] = useState(false)

  const toggleActive = async () => {
    setBusy(true)
    try {
      await api.updateSource(source.id, { active: !source.active })
      await onRefresh()
    } catch (err) {
      onError(parseApiError(err, 'failed to update source'))
    } finally {
      setBusy(false)
    }
  }

  const setInterval = async (seconds: number) => {
    setBusy(true)
    try {
      await api.updateSource(source.id, { refresh_interval_seconds: seconds })
      await onRefresh()
      onCancelEditInterval()
    } catch (err) {
      onError(parseApiError(err, 'failed to update refresh interval'))
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    // Browser confirm is fine — the cost of a custom modal here is
    // not worth it for a once-per-feed action. The backend's 400
    // protects built-ins if the user has stale code.
    const ok = window.confirm(`Delete "${source.name}"? This stops fetching immediately.`)
    if (!ok) return
    setBusy(true)
    try {
      await api.deleteSource(source.id)
      await onRefresh()
    } catch (err) {
      onError(parseApiError(err, 'failed to delete source'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs ${busy ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-2 min-w-0">
        {source.favicon_path && (
          <img
            src={`/assets/${source.favicon_path}`}
            alt=""
            width={14}
            height={14}
            loading="lazy"
            className="shrink-0 w-3.5 h-3.5 rounded-sm bg-slate-800"
            onError={(e) => {
              ;(e.currentTarget as HTMLImageElement).style.display = 'none'
            }}
          />
        )}
        <span className={`truncate font-medium ${source.active ? 'text-slate-100' : 'text-slate-500 line-through'}`}>
          {source.name}
        </span>
        <span className="text-slate-500 shrink-0">{source.category}</span>
        {!source.active && (
          <span className="text-[10px] text-amber-400 shrink-0">paused</span>
        )}
        {source.last_error && (
          <span
            className="text-[10px] text-red-300 shrink-0 truncate"
            title={source.last_error}
          >
            ⚠
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 mt-1">
        <button
          onClick={toggleActive}
          disabled={busy}
          className="text-[10px] text-slate-400 hover:text-slate-100 disabled:opacity-50"
          aria-label={source.active ? 'pause source' : 'resume source'}
        >
          {source.active ? 'pause' : 'resume'}
        </button>
        {isEditingInterval ? (
          <select
            value={source.refresh_interval_seconds}
            onChange={(e) => setInterval(Number(e.target.value))}
            disabled={busy}
            className="text-[10px] rounded bg-slate-900 border border-slate-800 px-1 py-0.5 text-slate-100"
            aria-label="refresh interval"
          >
            {REFRESH_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                every {p.label}
              </option>
            ))}
          </select>
        ) : (
          <button
            onClick={onStartEditInterval}
            disabled={busy}
            className="text-[10px] text-slate-400 hover:text-slate-100 disabled:opacity-50"
            aria-label="edit refresh interval"
          >
            every {refreshLabel(source.refresh_interval_seconds)}
          </button>
        )}
        {isEditingInterval && (
          <button
            onClick={onCancelEditInterval}
            disabled={busy}
            className="text-[10px] text-slate-500 hover:text-slate-200 disabled:opacity-50"
          >
            cancel
          </button>
        )}
        {!builtIn && (
          <button
            onClick={onDelete}
            disabled={busy}
            className="ml-auto text-[10px] text-red-300 hover:text-red-200 disabled:opacity-50"
            aria-label={`delete ${source.name}`}
          >
            delete
          </button>
        )}
        {builtIn && (
          <span
            className="ml-auto text-[10px] text-slate-600"
            title="built-in source — managed by the scheduler at startup"
          >
            built-in
          </span>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Recommended — curated list, one-tap Add
// ---------------------------------------------------------------------------

type Recommendation = {
  name: string
  category: string
  url: string
  blurb: string
}

function RecommendedTab({
  existingNames,
  onAdded,
  onError,
}: {
  existingNames: Set<string>
  onAdded: () => Promise<void>
  onError: (msg: string) => void
}) {
  const [recs, setRecs] = useState<Recommendation[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [adding, setAdding] = useState<string | null>(null)

  // Fetch on mount. We don't poll — the user can navigate away and
  // back if they want a fresh list, and the backend is a static
  // module so the response is cheap.
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .feedRecommendations()
      .then((rows) => {
        if (cancelled) return
        // Backend already strips names that exist; the frontend
        // filter is a defensive belt-and-suspenders for a stale
        // cache that doesn't yet know about a freshly-added row.
        setRecs(rows.filter((r) => !existingNames.has(r.name)))
      })
      .catch((err) => onError(parseApiError(err, 'failed to load recommendations')))
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [existingNames, onError])

  const add = async (rec: Recommendation) => {
    setAdding(rec.name)
    try {
      await api.createSource({
        name: rec.name,
        type: 'rss',
        category: rec.category,
        url: rec.url,
        refresh_interval_seconds: 3600,
      })
      await onAdded()
      // Optimistically remove from the local list so the row
      // disappears immediately rather than after the next refresh.
      setRecs((prev) => (prev ? prev.filter((r) => r.name !== rec.name) : prev))
    } catch (err) {
      onError(parseApiError(err, `failed to add ${rec.name}`))
    } finally {
      setAdding(null)
    }
  }

  if (loading) {
    return <p className="text-xs text-slate-500 italic px-1">loading…</p>
  }
  if (!recs || recs.length === 0) {
    return (
      <p className="text-xs text-slate-500 italic px-1">
        you've added all the recommended feeds — try the Add custom tab
      </p>
    )
  }
  return (
    <ul className="space-y-1">
      {recs.map((r) => (
        <li
          key={r.name}
          className="rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs"
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="truncate font-medium text-slate-100">{r.name}</span>
            <span className="text-slate-500 shrink-0">{r.category}</span>
            <button
              onClick={() => add(r)}
              disabled={adding === r.name}
              className="ml-auto shrink-0 text-[10px] rounded bg-blue-800 hover:bg-blue-700 disabled:opacity-50 text-blue-100 px-2 py-0.5"
              aria-label={`add ${r.name}`}
            >
              {adding === r.name ? 'adding…' : 'Add'}
            </button>
          </div>
          <p className="text-[10px] text-slate-500 mt-0.5">{r.blurb}</p>
        </li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// Add custom — paste-a-URL form
// ---------------------------------------------------------------------------

function AddCustomTab({
  onAdded,
  onError,
}: {
  onAdded: () => Promise<void>
  onError: (msg: string) => void
}) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [category, setCategory] = useState('news')
  const [refresh, setRefresh] = useState<number>(3600)
  const [submitting, setSubmitting] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const trimmedName = name.trim().toLowerCase()
    const trimmedUrl = url.trim()
    if (!trimmedName) {
      onError('name is required')
      return
    }
    if (!/^[a-z0-9_]{1,120}$/.test(trimmedName)) {
      onError('name must be lowercase letters, digits, or underscore (1-120 chars)')
      return
    }
    if (!trimmedUrl) {
      onError('url is required')
      return
    }
    try {
      // Throws on parse failure — catches typos before the backend
      // sees them, but the backend still validates because the
      // client-side check isn't authoritative.
      new URL(trimmedUrl)
    } catch {
      onError('url is not a valid URL')
      return
    }
    setSubmitting(true)
    try {
      await api.createSource({
        name: trimmedName,
        type: 'rss',
        category,
        url: trimmedUrl,
        refresh_interval_seconds: refresh,
      })
      setName('')
      setUrl('')
      await onAdded()
    } catch (err) {
      onError(parseApiError(err, 'failed to add source'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-2 text-xs">
      <div>
        <label className="block text-slate-400 mb-1" htmlFor="fm-name">Name</label>
        <input
          id="fm-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my_blog"
          className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100 placeholder:text-slate-600"
        />
        <p className="text-[10px] text-slate-500 mt-0.5">
          lowercase letters, digits, underscore
        </p>
      </div>
      <div>
        <label className="block text-slate-400 mb-1" htmlFor="fm-url">RSS / Atom URL</label>
        <input
          id="fm-url"
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/feed.xml"
          className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100 placeholder:text-slate-600"
        />
      </div>
      <div className="flex gap-2">
        <div className="flex-1">
          <label className="block text-slate-400 mb-1" htmlFor="fm-category">Category</label>
          <select
            id="fm-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100"
          >
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-slate-400 mb-1" htmlFor="fm-refresh">Refresh</label>
          <select
            id="fm-refresh"
            value={refresh}
            onChange={(e) => setRefresh(Number(e.target.value))}
            className="w-full rounded bg-slate-900 border border-slate-800 px-2 py-1 text-slate-100"
          >
            {REFRESH_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>every {p.label}</option>
            ))}
          </select>
        </div>
      </div>
      <button
        type="submit"
        disabled={submitting}
        className="w-full rounded bg-blue-800 hover:bg-blue-700 disabled:opacity-50 text-blue-100 px-3 py-1.5"
      >
        {submitting ? 'adding…' : 'Add feed'}
      </button>
    </form>
  )
}