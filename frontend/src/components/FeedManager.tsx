// Phase 5 FeedManager — Drawer section for adding, editing, and
// removing dynamic RSS feeds. Three tabs:
//
//   • My feeds — the existing source list, with per-row actions:
//     toggle active, edit metadata, edit refresh interval, delete
//     (works for both dynamic and built-in rows). Plugin-backed
//     rows have url/name fields locked (the plugin owns them) but
//     can still be deleted — the row goes away and the scheduler
//     job is removed; the plugin re-registers itself on the next
//     backend restart.
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
// Errors flow up to App's red banner via `onError` so the
// visual treatment matches refresh failures — single, consistent
// error surface across the dashboard.
//
// Add Custom has a "Test" button next to "Add feed" so the user
// can probe the URL with the same plugin Add would use, no DB
// write. The result is shown inline: green "Looks good (42
// items)" with sample titles, or red "Site blocks automated
// access" with the raw error. The Add flow always succeeds
// against URLs that pass Test (modulo name conflicts that show
// up at Add time). The Test step is non-destructive and
// reversible — tapping it doesn't change the form state.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type Source } from '../api'
import { SourceIcon } from './SourceIcon'
import { toast } from './Toast'

type Props = {
  sources: Source[]
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
  // Bubbles up from ``SourceRow`` when the user renames a source via
  // the inline edit form. See ``App.tsx`` — the parent remaps
  // ``activeSources`` synchronously so the chip bar stays consistent
  // through the rename.
  onSourceRenamed?: (oldName: string, newName: string) => void
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

// Mirrors the backend's _PODCAST_DEFAULT_REFRESH (routes/sources.py)
// — episodes publish far less often than news, so the "Podcast"
// radio bumps the refresh dropdown to this instead of the 1h
// plain-RSS default.
const PODCAST_DEFAULT_REFRESH = 21600

// Mirrors the backend's _YOUTUBE_DEFAULT_REFRESH — same rationale as
// podcasts, most channels post far less than hourly.
const YOUTUBE_DEFAULT_REFRESH = 21600

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
  'podcast',
  'video',
  // Leisure categories (0019_leisure_feed_recommendations) — kept
  // after the original work-adjacent set so the dropdown reads as
  // "the usual suspects, then the fun stuff" rather than an
  // alphabetical shuffle that'd bury either group.
  'sports',
  'entertainment',
  'gaming',
  'food',
  'music',
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

// Same shape as Card.tsx's ``timeAgo`` — kept duplicated rather than
// extracted to a shared module because the function is six lines
// and the call sites are UI-local. If a third caller appears, lift
// it to ``frontend/src/lib/format.ts``.
function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// True when ``last_fetch_at`` is older than 2× the configured
// refresh interval — heuristic for "the scheduler should have run by
// now but hasn't (or just ran and didn't update last_fetch_at
// because of a fetch error)." The 2× grace is generous because
// scheduler jobs queue up if multiple sources hit at once.
function isStale(source: Source): boolean {
  if (!source.last_fetch_at) return true
  const ageMs = Date.now() - new Date(source.last_fetch_at).getTime()
  return ageMs > source.refresh_interval_seconds * 2 * 1000
}

// Friendly message lookup for the Test step. Maps the backend's
// ``error_kind`` enum (see ``SourceTestResult`` in api.ts and
// ``SourceTestResult`` in backend/app/schemas.py) to user-facing
// copy. The ``name`` slot in name_conflict / invalid_url messages
// is interpolated by the caller via the raw ``error`` string when
// it adds info beyond the kind.
function friendlyTestError(
  kind: string | null,
  raw: string | null,
): string {
  switch (kind) {
    case 'not_found':
      return "Couldn't find a feed at that URL. Double-check the address."
    case 'forbidden':
      return 'This site blocks automated access. Try adding a User-Agent header (Advanced → Custom headers).'
    case 'timeout':
      return 'Feed took too long to respond. The site may be slow or down.'
    case 'parse_error':
      return "Got a response but it didn't look like a feed. Is this a valid RSS/Atom URL?"
    case 'name_conflict':
      return raw ? `Name conflict — ${raw}` : 'A source with this name already exists.'
    case 'invalid_url':
      return raw ? `Invalid URL — ${raw}` : "That URL isn't valid."
    case 'unsupported_type':
      return raw ? `Unsupported type — ${raw}` : "This feed type isn't supported yet."
    case 'network_error':
      return "Couldn't reach the URL. Check your network or the site's status."
    case 'unknown':
    default:
      return raw ? `Something went wrong — ${raw}` : 'Something went wrong. Try again.'
  }
}

function parseApiError(err: unknown, fallback: string): string {
  // FastAPI returns 422 with `{detail: [{loc: [...], msg: "..."}, ...]}`
  // (validation errors) or `{detail: "..."}` (a plain HTTPException).
  // ``jsonFetch`` (api.ts) attaches that parsed `detail` to the thrown
  // ``ApiError``, so check it before falling back to the generic
  // "<status> <statusText>" message — otherwise the specific,
  // useful reason never gets a chance to show.
  const e = err as { message?: string; detail?: unknown }
  if (Array.isArray(e.detail)) {
    return e.detail
      .map((d: unknown) =>
        d && typeof d === 'object' && typeof (d as { msg?: unknown }).msg === 'string'
          ? (d as { msg: string }).msg
          : JSON.stringify(d),
      )
      .join('; ')
  }
  if (typeof e.detail === 'string') return e.detail
  if (typeof e.message === 'string') return e.message
  return fallback
}

export function FeedManager({ sources, onRefresh, onError, onSourceRenamed }: Props) {
  const [tab, setTab] = useState<Tab>('mine')
  const [editingInterval, setEditingInterval] = useState<number | null>(null)
  const [editingRowId, setEditingRowId] = useState<number | null>(null)
  // Stable Set of source names. ``RecommendedTab``'s useEffect
  // depends on this and would otherwise refire on every parent
  // render (parent constructs ``new Set(...)`` inline today).
  const existingNames = useMemo(
    () => new Set(sources.map((s) => s.name)),
    [sources],
  )

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
          editingRowId={editingRowId}
          setEditingRowId={setEditingRowId}
          onRefresh={onRefresh}
          onError={onError}
          onSourceRenamed={onSourceRenamed}
        />
      )}
      {tab === 'recommended' && (
        <RecommendedTab
          existingNames={existingNames}
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
      className={`flex-1 min-h-[36px] rounded-ios text-ios-body transition ${
        active
          ? 'bg-bg-elevated text-accent'
          : 'bg-bg-surface text-label-primary active:bg-bg-elevated'
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
  editingRowId,
  setEditingRowId,
  onRefresh,
  onError,
  onSourceRenamed,
}: {
  sources: Source[]
  editingInterval: number | null
  setEditingInterval: (id: number | null) => void
  // ID of the row currently in inline edit mode. Mutually exclusive
  // with ``editingInterval`` for that row — editing the metadata
  // closes any open interval picker on the same row.
  editingRowId: number | null
  setEditingRowId: (id: number | null) => void
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
  onSourceRenamed?: (oldName: string, newName: string) => void
}) {
  if (sources.length === 0) {
    return <p className="text-ios-body text-label-secondary italic px-1">no sources yet</p>
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
            isEditing={editingRowId === s.id}
            onStartEdit={() => {
              setEditingInterval(null)
              setEditingRowId(s.id)
            }}
            onCancelEdit={() => setEditingRowId(null)}
            onRefresh={onRefresh}
            onError={onError}
            onRenamed={(oldName, newName) => {
              setEditingRowId(null)
              onSourceRenamed?.(oldName, newName)
            }}
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
  isEditing,
  onStartEdit,
  onCancelEdit,
  onRefresh,
  onError,
  onRenamed,
}: {
  source: Source
  isEditingInterval: boolean
  onStartEditInterval: () => void
  onCancelEditInterval: () => void
  isEditing: boolean
  onStartEdit: () => void
  onCancelEdit: () => void
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
  onRenamed?: (oldName: string, newName: string) => void
}) {
  const builtIn = isBuiltIn(source)
  const [busy, setBusy] = useState(false)
  // Inline two-tap delete confirm. ``confirmingDelete`` swaps the
  // single ``delete`` button into a ``confirm delete`` + ``cancel``
  // pair, scoped to this row. Auto-cancels after
  // ``CONFIRM_TIMEOUT_MS`` so an abandoned confirm doesn't linger
  // forever (user opened delete, looked away, came back). The native
  // ``window.confirm`` modal was jarring inside a drawer — competing
  // iOS sheet for the user's attention. Inline keep the gesture in
  // the drawer's frame.
  const CONFIRM_TIMEOUT_MS = 4000
  const [confirmingDelete, setConfirmingDelete] = useState(false)

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
    // Inline two-tap — the first tap set ``confirmingDelete`` to
    // true (button became "confirm delete"). This is the second tap.
    // Backend protects built-ins with a 400; we also hide the
    // button for built-ins so this path is dynamic-only.
    setBusy(true)
    try {
      await api.deleteSource(source.id)
      setConfirmingDelete(false)
      await onRefresh()
    } catch (err) {
      onError(parseApiError(err, 'failed to delete source'))
      setConfirmingDelete(false)
    } finally {
      setBusy(false)
    }
  }

  // Auto-cancel the confirm after a few seconds. Reset the timer on
  // each new confirmation so a user who taps, waits, taps again gets
  // a fresh window. ``useRef`` avoids re-firing the effect when only
  // the timeout id changes.
  useEffect(() => {
    if (!confirmingDelete) return
    const t = window.setTimeout(() => setConfirmingDelete(false), CONFIRM_TIMEOUT_MS)
    return () => window.clearTimeout(t)
  }, [confirmingDelete])

  // Edit form local state — initialized from props so the user
  // always sees the current row values, not stale defaults. Reset
  // on each edit-start so cancelling a rename and starting again
  // doesn't show the previously-typed (and rejected) values.
  const [editName, setEditName] = useState(source.name)
  const [editUrl, setEditUrl] = useState(source.url)
  const [editCategory, setEditCategory] = useState(source.category)
  // ``editHeaders`` is a JSON-string textarea. Empty string means
  // "no override" (custom_headers column will be cleared on save);
  // any non-empty value must parse to a ``str → str`` map. We keep
  // it as a string rather than a parsed object because the user
  // expects to see exactly what they typed, and showing parsed keys
  // back to them in a different order would feel broken.
  const [editHeaders, setEditHeaders] = useState(
    source.custom_headers ? JSON.stringify(source.custom_headers) : '',
  )
  // Parse error from the last editHeaders change. Surfaced inline
  // so the user sees why Save is disabled rather than getting a
  // generic 422 on submit.
  const [headersError, setHeadersError] = useState<string | null>(null)

  // When edit mode opens, seed the inputs from the current row. Use
  // an effect keyed on ``isEditing`` rather than re-deriving in the
  // ``useState`` initializer because React only runs the initializer
  // once — closing + reopening edit mode would otherwise show stale
  // text from the previous edit.
  useEffect(() => {
    if (isEditing) {
      setEditName(source.name)
      setEditUrl(source.url)
      setEditCategory(source.category)
      setEditHeaders(
        source.custom_headers ? JSON.stringify(source.custom_headers) : '',
      )
      setHeadersError(null)
    }
  }, [isEditing, source.name, source.url, source.category, source.custom_headers])

  // Live-validate the headers textarea. Treat empty as "no override";
  // for non-empty, try-parse and confirm it's a flat string→string map.
  // We do this on every keystroke so the Save button can flip between
  // enabled and disabled without a click round-trip.
  const parsedHeaders = (() => {
    if (!editHeaders.trim()) return { ok: true as const, value: null as Record<string, string> | null }
    try {
      const parsed = JSON.parse(editHeaders)
      if (
        typeof parsed !== 'object' ||
        parsed === null ||
        Array.isArray(parsed) ||
        Object.entries(parsed).some(
          ([k, v]) => typeof k !== 'string' || typeof v !== 'string',
        )
      ) {
        return { ok: false as const, error: 'must be a flat {"Header": "value"} map' }
      }
      return { ok: true as const, value: parsed as Record<string, string> }
    } catch {
      return { ok: false as const, error: 'invalid JSON' }
    }
  })()
  // Push the error string into state so the inline error renders,
  // but only if it changed (avoids re-renders on every keystroke
  // when the message is the same).
  useEffect(() => {
    const next = parsedHeaders.ok ? null : parsedHeaders.error
    if (next !== headersError) setHeadersError(next)
  }, [parsedHeaders.ok, parsedHeaders.error]) // eslint-disable-line react-hooks/exhaustive-deps

  const saveEdit = async () => {
    const nameChanged = editName.trim() !== source.name
    const urlChanged = editUrl.trim() !== source.url
    const categoryChanged = editCategory.trim() !== source.category
    // Compare parsed headers against the source's current map. We
    // parse the textarea first so trailing-whitespace differences
    // don't accidentally trigger a no-op PATCH.
    const headersChanged =
      JSON.stringify(parsedHeaders.value ?? null) !==
      JSON.stringify(source.custom_headers ?? null)
    if (
      !nameChanged &&
      !urlChanged &&
      !categoryChanged &&
      !headersChanged
    ) {
      // Nothing to save — close the form so the user doesn't think
      // the click was eaten by a bug.
      onCancelEdit()
      return
    }
    if (!parsedHeaders.ok) {
      // Guard against the rare race where the user clicks Save with
      // an invalid textarea. The button is disabled when not
      // ``parsedHeaders.ok`` but we re-check here as a safety net.
      return
    }
    setBusy(true)
    try {
      const body: {
        name?: string
        url?: string
        category?: string
        // ``null`` clears the column; an object sets/replaces it.
        custom_headers?: Record<string, string> | null
      } = {}
      if (nameChanged) body.name = editName.trim()
      if (urlChanged) body.url = editUrl.trim()
      if (categoryChanged) body.category = editCategory.trim()
      if (headersChanged) body.custom_headers = parsedHeaders.value
      const updated = await api.updateSource(source.id, body)
      const oldName = source.name
      const newName = updated.name
      await onRefresh()
      if (nameChanged && oldName !== newName) {
        // Bubble up so App can remap the active filter chip in the
        // same render cycle as the refresh.
        onRenamed?.(oldName, newName)
      } else {
        // No rename — close edit mode without triggering the remap.
        onCancelEdit()
      }
    } catch (err) {
      onError(parseApiError(err, 'failed to save changes'))
    } finally {
      setBusy(false)
    }
  }

  if (isEditing) {
    // Inline edit form — replaces the read-only row chrome while
    // open. The fields mirror what's editable via PATCH (name +
    // url + category). ``url`` is locked for built-ins because the
    // backend rejects it with a 400 anyway; locking client-side
    // avoids the user typing something that would just bounce.
    return (
      <div className={`px-3 py-2 text-ios-caption border-b border-hairline last:border-b-0 space-y-2 ${busy ? 'opacity-60' : ''}`}>
        <label className="block">
          <span className="text-label-tertiary text-[11px] uppercase tracking-wide">name</span>
          <input
            type="text"
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            disabled={busy || builtIn}
            className="mt-0.5 w-full bg-bg-elevated border border-hairline rounded-ios px-2 py-1 text-ios-body text-label-primary disabled:opacity-50"
            autoFocus
            spellCheck={false}
          />
        </label>
        <label className="block">
          <span className="text-label-tertiary text-[11px] uppercase tracking-wide">url</span>
          <input
            type="url"
            value={editUrl}
            onChange={(e) => setEditUrl(e.target.value)}
            disabled={busy || builtIn}
            className="mt-0.5 w-full bg-bg-elevated border border-hairline rounded-ios px-2 py-1 text-ios-body text-label-primary disabled:opacity-50"
            spellCheck={false}
          />
          {builtIn && (
            <span className="block text-label-tertiary text-[10px] mt-0.5">
              built-in — url is bound to the plugin
            </span>
          )}
        </label>
        <label className="block">
          <span className="text-label-tertiary text-[11px] uppercase tracking-wide">category</span>
          <input
            type="text"
            value={editCategory}
            onChange={(e) => setEditCategory(e.target.value)}
            disabled={busy}
            className="mt-0.5 w-full bg-bg-elevated border border-hairline rounded-ios px-2 py-1 text-ios-body text-label-primary disabled:opacity-50"
            list="feed-category-options"
            spellCheck={false}
          />
          <datalist id="feed-category-options">
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </label>
        {/*
          Custom HTTP headers. Hidden for built-ins — the URL is
          locked too, and overriding headers on a built-in would
          defeat the point of the static plugin contract. Empty
          textarea means "use defaults"; non-empty must parse as a
          flat str→str map. ``User-Agent`` is the only override
          anyone realistically needs (CBC blocks our default UA),
          but the textarea lets power users add e.g.
          ``Accept-Language`` for region-gated feeds.
        */}
        {!builtIn && (
          <label className="block">
            <span className="text-label-tertiary text-[11px] uppercase tracking-wide">
              custom headers (JSON)
            </span>
            <textarea
              value={editHeaders}
              onChange={(e) => setEditHeaders(e.target.value)}
              disabled={busy}
              rows={3}
              spellCheck={false}
              placeholder='{"User-Agent": "Mozilla/5.0 ..."}'
              className="mt-0.5 w-full bg-bg-elevated border border-hairline rounded-ios px-2 py-1 text-ios-caption text-label-primary font-mono disabled:opacity-50"
            />
            {headersError && (
              <span className="block text-red-400 text-[10px] mt-0.5">
                {headersError}
              </span>
            )}
            {!headersError && editHeaders.trim() && (
              <span className="block text-label-tertiary text-[10px] mt-0.5">
                sent on every fetch; clear to use defaults
              </span>
            )}
          </label>
        )}
        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={saveEdit}
            disabled={busy || !parsedHeaders.ok}
            className="text-ios-caption text-accent active:opacity-60 disabled:opacity-40"
          >
            save
          </button>
          <button
            onClick={onCancelEdit}
            disabled={busy}
            className="text-ios-caption text-label-secondary active:opacity-60 disabled:opacity-40"
          >
            cancel
          </button>
          {!builtIn && source.url !== editUrl && (
            <span
              className="text-ios-caption text-amber-400 ml-auto"
              title="changing the url clears the cached favicon; the next ingest re-downloads"
            >
              ⚠ favicon resets
            </span>
          )}
        </div>
      </div>
    )
  }

  return (
    <div className={`px-3 py-2 text-ios-caption border-b border-hairline last:border-b-0 ${busy ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-2 min-w-0">
        {/* SourceIcon — colored-letter fallback when the favicon
            hasn't landed yet. ``size={14}`` matches the original
            w-3.5 h-3.5 img (14px). */}
        <SourceIcon src={source.favicon_path} name={source.name} size={14} />
        <span
          className={`truncate font-medium ${
            !source.active
              ? 'text-label-secondary line-through'
              : isStale(source)
                ? 'text-label-secondary italic'
                : 'text-label-primary'
          }`}
          title={
            isStale(source)
              ? `last fetched ${timeAgo(source.last_fetch_at)} — refresh interval is ${refreshLabel(source.refresh_interval_seconds)}`
              : undefined
          }
        >
          {source.name}
        </span>
        <span className="text-label-tertiary shrink-0 text-[11px]">{source.category}</span>
        {/* Auto-disabled chips take precedence over the plain
            "paused" chip — the distinction matters: a paused source
            is a user choice (no action needed); an auto-disabled
            source has hit the scheduler's consecutive-failure
            threshold and needs ``last_error`` review before a manual
            re-enable is worthwhile. ``auto_disabled`` is computed on
            the backend (``Source.auto_disabled``) so the frontend
            doesn't have to know the threshold value. */}
        {source.auto_disabled ? (
          <span
            className="text-ios-caption text-red-400 shrink-0"
            title={
              source.last_error
                ? `auto-disabled after ${source.error_count} consecutive failures — last: ${source.last_error}`
                : `auto-disabled after ${source.error_count} consecutive failures`
            }
          >
            auto-disabled
          </span>
        ) : (
          !source.active && (
            <span className="text-ios-caption text-amber-400 shrink-0">paused</span>
          )
        )}
        {/* Quantitative error chip. ``error_count > 1`` shows the
            number so the user can distinguish a chronic failure
            (``⚠ 144``) from a one-off (``⚠`` alone is fine for
            ``error_count === 1``). Gated on the count rather than
            ``last_error`` truthiness so a source that recovered
            between fetches but still has a nonzero counter doesn't
            silently flash a warning. */}
        {source.error_count > 0 && (
          <span
            className="text-ios-caption text-red-400 shrink-0"
            title={
              source.last_error
                ? `${source.error_count} consecutive failure${source.error_count === 1 ? '' : 's'} — last: ${source.last_error}`
                : `${source.error_count} consecutive failure${source.error_count === 1 ? '' : 's'}`
            }
          >
            ⚠ {source.error_count > 1 ? source.error_count : ''}
          </span>
        )}
        {/* Net vote score — sum(thumb_up) - sum(thumb_down) across
            every entry from this source. Omitted at exactly 0 (never
            voted on, or an even split — neither is actionable) so
            most rows stay quiet; only sources the user has a real
            opinion on get a badge. Negative (a source they keep
            downvoting) is the one worth surfacing loudly — red, with
            the minus sign JS already gives negative numbers.
            Positive gets a quieter green; there's no "clean this up"
            action implied by a source the user likes. */}
        {source.net_vote_score !== 0 && (
          <span
            className={`shrink-0 text-ios-caption font-semibold ${
              source.net_vote_score < 0 ? 'text-red-400' : 'text-emerald-400'
            }`}
            title={`net vote score: ${source.net_vote_score > 0 ? '+' : ''}${source.net_vote_score} (thumbs up minus thumbs down across this source's entries)`}
          >
            {source.net_vote_score > 0 ? '+' : ''}
            {source.net_vote_score}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 mt-1">
        <button
          onClick={toggleActive}
          disabled={busy}
          className="text-ios-caption text-accent active:opacity-60 disabled:opacity-40"
          aria-label={source.active ? 'pause source' : 'resume source'}
        >
          {source.active ? 'pause' : 'resume'}
        </button>
        <button
          onClick={onStartEdit}
          disabled={busy}
          className="text-ios-caption text-accent active:opacity-60 disabled:opacity-40"
          aria-label={`edit ${source.name}`}
        >
          edit
        </button>
        {/* Last-fetched caption. Renders inline with the action
            buttons so the user can read at-a-glance freshness
            without hovering for a tooltip. ``text-label-tertiary``
            keeps it secondary to the action affordances. Stale
            sources get ``text-amber-400`` to flag the row without
            using up another glyph in the title row. */}
        <span
          className={`text-ios-caption text-label-tertiary ${
            isStale(source) ? 'text-amber-400' : ''
          }`}
          title={
            source.last_fetch_at
              ? `last fetch: ${new Date(source.last_fetch_at).toLocaleString()}`
              : 'never fetched'
          }
        >
          · fetched {timeAgo(source.last_fetch_at)}
        </span>
        {isEditingInterval ? (
          <select
            value={source.refresh_interval_seconds}
            onChange={(e) => setInterval(Number(e.target.value))}
            disabled={busy}
            className="text-ios-caption rounded-ios bg-bg-elevated border border-hairline px-1 py-0.5 text-label-primary"
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
            className="text-ios-caption text-accent active:opacity-60 disabled:opacity-40"
            aria-label="edit refresh interval"
          >
            every {refreshLabel(source.refresh_interval_seconds)}
          </button>
        )}
        {isEditingInterval && (
          <button
            onClick={onCancelEditInterval}
            disabled={busy}
            className="text-ios-caption text-label-secondary active:opacity-60 disabled:opacity-40"
          >
            cancel
          </button>
        )}
        {/* Fetch-now button. Power-user affordance: force an
            immediate ingest without waiting for the next scheduler
            tick. The backend's POST /api/ingest/{name} endpoint
            returns the same ``IngestResult`` shape, so we surface
            the row + inserted counts in a toast. We sit the button
            to the LEFT of the delete button (which is ``ml-auto``)
            so the destructive action stays at the far right. */}
        {!confirmingDelete && (
          <button
            onClick={async () => {
              setBusy(true)
              try {
                const r = await api.ingest(source.name)
                if (r.error) {
                  toast(`${source.name}: ${r.error}`, 'error')
                } else {
                  toast(
                    `Fetched ${r.fetched} from ${r.source}` +
                      (r.inserted > 0 ? ` (${r.inserted} new)` : ''),
                    'info',
                  )
                }
                await onRefresh()
              } catch (err) {
                onError(parseApiError(err, 'fetch failed'))
              } finally {
                setBusy(false)
              }
            }}
            disabled={busy}
            className="text-ios-caption text-accent active:opacity-60 disabled:opacity-40"
            aria-label={`fetch ${source.name} now`}
          >
            fetch
          </button>
        )}
        {!confirmingDelete && (
          <button
            onClick={() => setConfirmingDelete(true)}
            disabled={busy}
            className="ml-auto text-ios-caption text-red-400 active:opacity-60 disabled:opacity-40"
            aria-label={`delete ${source.name}`}
          >
            delete
          </button>
        )}
        {confirmingDelete && (
          <>
            <button
              onClick={onDelete}
              disabled={busy}
              className="ml-auto text-ios-caption text-red-400 active:opacity-60 disabled:opacity-40 font-semibold"
              aria-label={`confirm delete ${source.name}`}
            >
              confirm delete
            </button>
            <button
              onClick={() => setConfirmingDelete(false)}
              disabled={busy}
              className="text-ios-caption text-label-secondary active:opacity-60 disabled:opacity-40"
              aria-label={`cancel delete ${source.name}`}
            >
              cancel
            </button>
          </>
        )}
        {builtIn && !confirmingDelete && (
          <span
            className="text-ios-caption text-label-tertiary"
            title="built-in source — managed by the scheduler at startup. Will re-appear after a backend restart unless the plugin is also removed."
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
  // ``Source.type`` — ``"rss"`` (default) or ``"reddit"``. Forwarded
  // verbatim to ``POST /api/sources`` so the backend dispatches to
  // the right plugin. Recommendations without the field fall through
  // to the default ("rss") for forward compat with the editor rows
  // that predate the Reddit rollout.
  type?: string
  // Optional HTTP header overrides pre-applied at Add time. Set
  // on entries whose CDN blocks our default ``Popping/0.2`` UA
  // (CBC). The frontend passes them through to ``POST /api/sources``
  // as ``custom_headers`` — no extra click needed.
  default_headers?: Record<string, string> | null
  // "editorial" (hand-picked) or "llm" (found via the "Find more
  // feeds" button / auto-discovery). Drives the small badge next to
  // the name so a freshly-discovered row reads differently from the
  // curated set.
  source?: string
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
  // "Find more feeds" button state. ``discovering`` disables the
  // button while the LLM call + validation fetches are in flight
  // (a few seconds); ``discoverMsg`` is a one-line status the user
  // sees after it resolves ("found 2 new feeds" / "nothing new
  // found this time" — added=0 is a normal outcome, not an error).
  const [discovering, setDiscovering] = useState(false)
  const [discoverMsg, setDiscoverMsg] = useState<string | null>(null)

  // Shared loader — used for the mount fetch and for refetching after
  // "Find more feeds" adds new rows to the pool.
  const fetchRecs = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await api.feedRecommendations()
      // Backend already strips names that exist; the frontend filter
      // is a defensive belt-and-suspenders for a stale cache that
      // doesn't yet know about a freshly-added row.
      setRecs(rows.filter((r) => !existingNames.has(r.name)))
    } catch (err) {
      onError(parseApiError(err, 'failed to load recommendations'))
    } finally {
      setLoading(false)
    }
  }, [existingNames, onError])

  // Fetch on mount. We don't otherwise poll — the user can navigate
  // away and back for a fresh list, and "Find more feeds" (below)
  // covers the case where the pool itself changes.
  useEffect(() => {
    fetchRecs()
  }, [fetchRecs])

  const findMore = async () => {
    setDiscovering(true)
    setDiscoverMsg(null)
    try {
      const result = await api.discoverFeeds()
      setDiscoverMsg(
        result.added > 0
          ? `found ${result.added} new feed${result.added === 1 ? '' : 's'} in ${result.category}`
          : result.note
            ? `couldn't find feeds for ${result.category}: ${result.note}`
            : `nothing new found for ${result.category} this time`,
      )
      if (result.added > 0) await fetchRecs()
    } catch (err) {
      onError(parseApiError(err, 'failed to find more feeds'))
    } finally {
      setDiscovering(false)
    }
  }

  const add = async (rec: Recommendation) => {
    setAdding(rec.name)
    // Per-type sensible default refresh when the recommendation
    // doesn't ship one (none do today; the backend fills in the
    // same default from ``routes/sources._REDDIT_DEFAULT_REFRESH``
    // and ``_REFRESH_MIN * 60``). Mirroring the backend defaults
    // client-side means the Source row renders with the right
    // interval on first list refresh, before the backend's
    // response comes back.
    const isReddit = rec.type === 'reddit'
    try {
      await api.createSource({
        name: rec.name,
        type: rec.type ?? 'rss',
        category: rec.category,
        url: rec.url,
        refresh_interval_seconds: isReddit ? 900 : 3600,
        // Recommendation-supplied header overrides (e.g. CBC's
        // browser UA). Falls through to the route's ``None``
        // branch when the recommendation doesn't ship one —
        // omitting the field leaves ``custom_headers`` at the
        // backend default (empty map → use source-plugin defaults).
        custom_headers: rec.default_headers ?? undefined,
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

  // "Find more feeds" header — shown above the list regardless of
  // loading/empty state so the user can trigger discovery even when
  // the curated pool is fully added (the most likely time they'd
  // actually want it).
  const findMoreHeader = (
    <div className="px-3 pt-1 pb-2 flex items-center gap-2 border-b border-hairline">
      <button
        onClick={findMore}
        disabled={discovering}
        className="shrink-0 min-h-[28px] rounded-ios border border-accent text-accent active:opacity-60 disabled:opacity-40 px-2 py-0.5 text-ios-caption"
      >
        {discovering ? 'looking…' : '🔎 find more feeds'}
      </button>
      {discoverMsg && (
        <span className="text-ios-caption text-label-tertiary truncate">{discoverMsg}</span>
      )}
    </div>
  )

  if (loading) {
    return (
      <>
        {findMoreHeader}
        <p className="text-ios-body text-label-secondary italic px-3 py-2">loading…</p>
      </>
    )
  }
  if (!recs || recs.length === 0) {
    return (
      <>
        {findMoreHeader}
        <p className="text-ios-body text-label-secondary italic px-3 py-2">
          you've added all the recommended feeds — try Find more feeds or the Add custom tab
        </p>
      </>
    )
  }
  return (
    <>
      {findMoreHeader}
      <ul>
        {recs.map((r) => (
          <li
            key={r.name}
            className="px-3 py-2 text-ios-caption border-b border-hairline last:border-b-0"
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="truncate font-medium text-label-primary">{r.name}</span>
              <span className="text-label-tertiary shrink-0 text-[11px]">{r.category}</span>
              {r.source === 'llm' && (
                <span
                  className="shrink-0 inline-flex items-center rounded-full bg-accent-soft px-1.5 text-[10px] uppercase tracking-wide text-accent"
                  title="found via AI feed discovery"
                >
                  found for you
                </span>
              )}
              <button
                onClick={() => add(r)}
                disabled={adding === r.name}
                className="ml-auto shrink-0 min-h-[28px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white px-2 py-0.5 text-ios-caption"
                aria-label={`add ${r.name}`}
              >
                {adding === r.name ? 'adding…' : 'Add'}
              </button>
            </div>
            <p className="text-ios-caption text-label-secondary mt-0.5">{r.blurb}</p>
          </li>
        ))}
      </ul>
    </>
  )
}

// ---------------------------------------------------------------------------
// Auto feed — one URL in, one source out. Sits above the manual
// "Add custom" form as a shortcut: paste any site's URL (not
// necessarily a feed URL) and the backend finds its real feed, or
// falls back to periodic sitemap-based scraping if it doesn't have
// one. Deliberately its own tiny form with its own state — a true
// one-click flow, not a "prefill the manual form and let the user
// review" step, matching what was asked for ("I put in a url, and it
// goes and finds the RSS feed automatically then adds it"). The
// resulting source's name is auto-derived from the feed's hostname;
// rename it afterward from the row list like any other source.
// ---------------------------------------------------------------------------

function AutoFeedBlock({
  onAdded,
  onError,
}: {
  onAdded: () => Promise<void>
  onError: (msg: string) => void
}) {
  const [autoUrl, setAutoUrl] = useState('')
  const [autoSubmitting, setAutoSubmitting] = useState(false)
  const [autoMessage, setAutoMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const autoAdd = async () => {
    const trimmed = autoUrl.trim()
    if (!trimmed) {
      setAutoMessage({ kind: 'err', text: 'paste a URL first' })
      return
    }
    setAutoSubmitting(true)
    setAutoMessage(null)
    try {
      const result = await api.autoAddSource(trimmed)
      if (result.found && result.source) {
        const note = result.kind === 'generic_scrape'
          ? ' — no native feed found, tracking via periodic scraping instead'
          : ''
        toast(`Feed added: ${result.source.name}${note}`, 'info')
        setAutoUrl('')
        await onAdded()
      } else {
        setAutoMessage({
          kind: 'err',
          text: "couldn't find a feed (or anything scrapeable) at that URL",
        })
      }
    } catch (err) {
      onError(parseApiError(err, 'auto feed failed'))
    } finally {
      setAutoSubmitting(false)
    }
  }

  return (
    <div className="rounded-ios border border-hairline bg-bg-elevated p-3 space-y-2 mb-4">
      <div className="flex items-center gap-1.5">
        <span aria-hidden="true">✨</span>
        <span className="text-ios-caption uppercase tracking-wide text-label-tertiary">Auto feed</span>
      </div>
      <p className="text-ios-caption text-label-secondary">
        Paste any site's URL — we'll find its RSS feed automatically, or track it via
        periodic scraping if it doesn't have one.
      </p>
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={autoUrl}
          onChange={(e) => setAutoUrl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              void autoAdd()
            }
          }}
          placeholder="https://example.com"
          disabled={autoSubmitting}
          className="flex-1 min-h-[36px] rounded-ios bg-bg-surface border border-hairline px-2 text-label-primary placeholder:text-label-tertiary disabled:opacity-50"
        />
        <button
          type="button"
          onClick={() => void autoAdd()}
          disabled={autoSubmitting}
          className="shrink-0 min-h-[36px] rounded-ios bg-accent text-white px-3 text-ios-caption font-medium active:opacity-70 disabled:opacity-40"
        >
          {autoSubmitting ? 'Looking…' : 'Auto-add'}
        </button>
      </div>
      {autoMessage && (
        <p className={`text-ios-caption ${autoMessage.kind === 'err' ? 'text-red-400' : 'text-label-secondary'}`}>
          {autoMessage.text}
        </p>
      )}
    </div>
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
  // Subtype selector — drives both the URL placeholder/label and the
  // client-side validation. ``"rss"`` (default) takes a feed URL;
  // ``"reddit"`` takes a subreddit reference (``r/python`` or full
  // ``https://www.reddit.com/r/python``); ``"podcast"`` also takes a
  // feed URL (podcast feeds are RSS-shaped — the backend extracts
  // the episode audio + duration from the same fetch); ``"youtube_channel"``
  // takes any shape of YouTube link (channel, handle, custom, or
  // video URL) — the backend resolves it to the channel's video RSS
  // feed the same way it resolves Apple Podcasts show pages. The
  // backend applies the same dispatch in
  // ``routes/sources.create_source_endpoint`` so the field reaches
  // ``POST /api/sources`` as ``type``.
  const [sourceType, setSourceType] = useState<'rss' | 'reddit' | 'podcast' | 'youtube_channel'>('rss')
  const [submitting, setSubmitting] = useState(false)
  // Test step. ``testing`` is true while the request is in flight;
  // ``testResult`` holds the last result so the user can see
  // "Looks good (42 items)" until they edit the form again. The
  // result auto-clears on any form change so the user doesn't
  // accidentally trust a stale result after editing the URL.
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{
    kind: 'ok' | 'err'
    message: string
    itemCount?: number
    sampleTitles?: string[]
    // Set when the tested URL was an Apple Podcasts show page the
    // backend resolved to its real feed URL. We don't rewrite the
    // ``url`` field's own state to match — doing so would trip the
    // "clear stale test result on any form change" effect below and
    // wipe this very result out from under itself. Add already
    // resolves the same way server-side, so the row that gets
    // created uses the real feed URL regardless of what's still
    // showing in the input.
    resolvedUrl?: string
  } | null>(null)
  // Inline validation state mirrors the submit validator. ``urlError``
  // and ``nameError`` show why Test / Add are disabled; they clear
  // as soon as the form is fixed.
  const [urlError, setUrlError] = useState<string | null>(null)
  const [nameError, setNameError] = useState<string | null>(null)

  // Shared validator. Returns ``null`` on success, an error
  // string on failure. ``validateForm`` is called by both
  // ``submit`` and ``test`` so the form's "Add" and "Test" buttons
  // share the same input rules.
  const validateForm = (): string | null => {
    const trimmedName = name.trim().toLowerCase()
    const trimmedUrl = url.trim()
    if (!trimmedName) return 'name is required'
    if (!/^[a-z0-9_]{1,120}$/.test(trimmedName)) {
      return 'name must be lowercase letters, digits, or underscore (1-120 chars)'
    }
    if (!trimmedUrl) return 'url is required'
    if (sourceType === 'rss' || sourceType === 'podcast' || sourceType === 'youtube_channel') {
      try {
        new URL(trimmedUrl)
      } catch {
        return 'url is not a valid URL'
      }
    } else if (sourceType === 'reddit') {
      if (trimmedUrl.length < 3) {
        return 'enter a subreddit like r/python or a full reddit.com/r/python URL'
      }
    }
    return null
  }

  // Re-validate on every form change so the Add / Test buttons
  // reflect whether the form is submittable. Errors are surfaced
  // inline below the field (not via toast) — toasts are for
  // action confirmations, not "this field is empty."
  useEffect(() => {
    setNameError(name.trim() ? null : 'name is required')
    const trimmedUrl = url.trim()
    if (!trimmedUrl) {
      setUrlError('url is required')
    } else if (sourceType === 'rss' || sourceType === 'podcast' || sourceType === 'youtube_channel') {
      try {
        new URL(trimmedUrl)
        setUrlError(null)
      } catch {
        setUrlError('url is not a valid URL')
      }
    } else if (sourceType === 'reddit') {
      setUrlError(trimmedUrl.length < 3 ? 'enter a subreddit like r/python' : null)
    } else {
      setUrlError(null)
    }
    // Clear stale test result on any form change so the user
    // doesn't trust the old "Looks good" status after editing.
    setTestResult(null)
  }, [name, url, sourceType])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const err = validateForm()
    if (err) {
      onError(err)
      return
    }
    setSubmitting(true)
    try {
      const trimmedName = name.trim().toLowerCase()
      const trimmedUrl = url.trim()
      await api.createSource({
        name: trimmedName,
        type: sourceType,
        category,
        url: trimmedUrl,
        refresh_interval_seconds: refresh,
      })
      // Success — celebrate with a toast. The toast stays for the
      // standard 1.5s but the user can keep typing / closing the
      // Settings overlay; the toast is purely confirmatory.
      toast(`Feed Added: ${trimmedName}`, 'info')
      setName('')
      setUrl('')
      setTestResult(null)
      await onAdded()
    } catch (err) {
      onError(parseApiError(err, 'failed to add source'))
    } finally {
      setSubmitting(false)
    }
  }

  // Test the URL via the new ``POST /api/sources/test`` endpoint.
  // Reuses ``validateForm`` for client-side checks; the backend
  // does the real fetch + parse. Result is shown inline; no DB
  // write happens regardless of outcome.
  const test = async () => {
    const err = validateForm()
    if (err) {
      setTestResult({ kind: 'err', message: err })
      return
    }
    setTesting(true)
    setTestResult(null)
    try {
      const result = await api.testSource({
        name: name.trim().toLowerCase() || undefined,
        type: sourceType,
        category,
        url: url.trim(),
      })
      if (result.ok) {
        setTestResult({
          kind: 'ok',
          message: `Looks good — ${result.item_count ?? 0} items found`,
          itemCount: result.item_count ?? 0,
          sampleTitles: result.sample_titles,
          resolvedUrl: result.resolved_url ?? undefined,
        })
      } else {
        setTestResult({
          kind: 'err',
          message: friendlyTestError(result.error_kind, result.error),
          resolvedUrl: result.resolved_url ?? undefined,
        })
      }
    } catch (err) {
      // The endpoint returned 4xx/5xx (auth, network) — the
      // jsonFetch helper throws. Map to a friendly message.
      const raw = (err as Error).message
      setTestResult({
        kind: 'err',
        message: `Couldn't test the feed — ${raw}`,
      })
    } finally {
      setTesting(false)
    }
  }

  return (
    <>
      <AutoFeedBlock onAdded={onAdded} onError={onError} />
      <form onSubmit={submit} className="space-y-3 text-ios-body">
      <div>
        <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1" htmlFor="fm-name">Name</label>
        <input
          id="fm-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my_blog"
          className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary placeholder:text-label-tertiary"
        />
        <p className="text-ios-caption text-label-secondary mt-1">
          lowercase letters, digits, underscore
        </p>
      </div>
      {/* Subtype selector. Drives the URL label + placeholder + the
          validator branch in ``submit``. Radios sit next to the URL
          field so the user sees the choice immediately — a dropdown
          would work but adds a click and hides the most common
          choice (RSS). Picking "Podcast" or "YouTube" also bumps the
          refresh default to 6h (episodes/videos are infrequent;
          polling hourly just wastes requests — see
          _PODCAST_DEFAULT_REFRESH / _YOUTUBE_DEFAULT_REFRESH on the
          backend) unless the user has already customized it away
          from the plain-RSS default. */}
      <fieldset className="flex items-center gap-3 flex-wrap">
        <legend className="sr-only">Source type</legend>
        <span className="text-ios-caption uppercase tracking-wide text-label-tertiary">Type</span>
        <label className="flex items-center gap-1 text-ios-caption text-label-primary">
          <input
            type="radio"
            name="fm-type"
            value="rss"
            checked={sourceType === 'rss'}
            onChange={() => {
              setSourceType('rss')
              if (refresh === PODCAST_DEFAULT_REFRESH) setRefresh(3600)
            }}
            className="accent-accent"
          />
          RSS / Atom
        </label>
        <label className="flex items-center gap-1 text-ios-caption text-label-primary">
          <input
            type="radio"
            name="fm-type"
            value="reddit"
            checked={sourceType === 'reddit'}
            onChange={() => {
              setSourceType('reddit')
              if (refresh === PODCAST_DEFAULT_REFRESH) setRefresh(900)
            }}
            className="accent-accent"
          />
          Subreddit
        </label>
        <label className="flex items-center gap-1 text-ios-caption text-label-primary">
          <input
            type="radio"
            name="fm-type"
            value="podcast"
            checked={sourceType === 'podcast'}
            onChange={() => {
              setSourceType('podcast')
              if (refresh === 3600) setRefresh(PODCAST_DEFAULT_REFRESH)
              if (category === 'news') setCategory('podcast')
            }}
            className="accent-accent"
          />
          Podcast
        </label>
        <label className="flex items-center gap-1 text-ios-caption text-label-primary">
          <input
            type="radio"
            name="fm-type"
            value="youtube_channel"
            checked={sourceType === 'youtube_channel'}
            onChange={() => {
              setSourceType('youtube_channel')
              if (refresh === 3600) setRefresh(YOUTUBE_DEFAULT_REFRESH)
              if (category === 'news') setCategory('video')
            }}
            className="accent-accent"
          />
          YouTube
        </label>
      </fieldset>
      <div>
        <label
          className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1"
          htmlFor="fm-url"
        >
          {sourceType === 'reddit'
            ? 'Subreddit'
            : sourceType === 'podcast'
              ? 'Podcast RSS URL'
              : sourceType === 'youtube_channel'
                ? 'YouTube channel URL'
                : 'RSS / Atom URL'}
        </label>
        <input
          id="fm-url"
          type={sourceType === 'reddit' ? 'text' : 'url'}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={
            sourceType === 'reddit'
              ? 'r/python or https://www.reddit.com/r/python'
              : sourceType === 'podcast'
                ? 'https://example.com/podcast/feed.xml'
                : sourceType === 'youtube_channel'
                  ? 'https://www.youtube.com/@handle'
                  : 'https://example.com/feed.xml'
          }
          className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary placeholder:text-label-tertiary"
        />
      </div>
      <div className="flex gap-2">
        <div className="flex-1">
          <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1" htmlFor="fm-category">Category</label>
          <select
            id="fm-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
          >
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
        <div className="flex-1">
          <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1" htmlFor="fm-refresh">Refresh</label>
          <select
            id="fm-refresh"
            value={refresh}
            onChange={(e) => setRefresh(Number(e.target.value))}
            className="w-full min-h-[36px] rounded-ios bg-bg-elevated border border-hairline px-2 text-label-primary"
          >
            {REFRESH_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>every {p.label}</option>
            ))}
          </select>
        </div>
      </div>
      {/* Test result inline. ``data-test-result`` is the
          hook a future test could use to assert on the UI; not
          load-bearing for production. The block is conditionally
          rendered — no visual change when there's no result. The
          44px min-height keeps the layout from jumping when a
          result appears (3 lines of sample titles + a single
          status line still fits in 44px at body text size). */}
      {testResult && (
        <div
          data-test-result={testResult.kind}
          className={`rounded-ios p-3 text-ios-body ${
            testResult.kind === 'ok'
              ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-200'
              : 'bg-red-500/15 border border-red-500/40 text-red-200'
          }`}
        >
          <div className="font-medium">
            {testResult.kind === 'ok' ? '✓ ' : '✗ '}
            {testResult.message}
          </div>
          {testResult.resolvedUrl && (
            <div className="mt-1 text-ios-caption opacity-80 break-all">
              Resolved to the real feed: {testResult.resolvedUrl}
              {testResult.kind === 'ok' && ' — Add will use this URL.'}
            </div>
          )}
          {testResult.kind === 'ok' && testResult.sampleTitles && testResult.sampleTitles.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-ios-caption text-emerald-200/80">
              {testResult.sampleTitles.map((t, i) => (
                <li key={i} className="truncate">· {t}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {/* Two-button row: Test (left) + Add (right). Test is the
          ``secondary`` visual — outlined instead of filled — so
          the primary "Add" still reads as the default action.
          Both disabled while a request is in flight; the disabled
          state keeps the user from firing parallel requests. */}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void test()}
          disabled={testing || submitting || !!(nameError || urlError)}
          className="flex-1 min-h-[44px] rounded-ios border border-accent text-accent active:opacity-60 disabled:opacity-40"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
        <button
          type="submit"
          disabled={submitting || testing || !!(nameError || urlError)}
          className="flex-1 min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white"
        >
          {submitting ? 'adding…' : 'Add feed'}
        </button>
      </div>
      </form>
    </>
  )
}

