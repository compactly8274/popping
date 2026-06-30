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
import { SourceIcon } from './SourceIcon'

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

export function FeedManager({ sources, onRefresh, onError, onSourceRenamed }: Props) {
  const [tab, setTab] = useState<Tab>('mine')
  const [editingInterval, setEditingInterval] = useState<number | null>(null)
  const [editingRowId, setEditingRowId] = useState<number | null>(null)

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
    } catch (e) {
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
        {!builtIn && !confirmingDelete && (
          <button
            onClick={() => setConfirmingDelete(true)}
            disabled={busy}
            className="ml-auto text-ios-caption text-red-400 active:opacity-60 disabled:opacity-40"
            aria-label={`delete ${source.name}`}
          >
            delete
          </button>
        )}
        {!builtIn && confirmingDelete && (
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
        {builtIn && (
          <span
            className="ml-auto text-ios-caption text-label-tertiary"
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

  if (loading) {
    return <p className="text-ios-body text-label-secondary italic px-1">loading…</p>
  }
  if (!recs || recs.length === 0) {
    return (
      <p className="text-ios-body text-label-secondary italic px-1">
        you've added all the recommended feeds — try the Add custom tab
      </p>
    )
  }
  return (
    <ul>
      {recs.map((r) => (
        <li
          key={r.name}
          className="px-3 py-2 text-ios-caption border-b border-hairline last:border-b-0"
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="truncate font-medium text-label-primary">{r.name}</span>
            <span className="text-label-tertiary shrink-0 text-[11px]">{r.category}</span>
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
  // ``https://www.reddit.com/r/python``). The backend applies the
  // same dispatch in ``routes/sources.create_source_endpoint`` so
  // the field reaches ``POST /api/sources`` as ``type``.
  const [sourceType, setSourceType] = useState<'rss' | 'reddit'>('rss')
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
    if (sourceType === 'rss') {
      // Only run the URL constructor when the subtype expects an
      // http(s) URL. Reddit accepts ``r/python`` shorthand which
      // isn't a parseable URL, so we let the backend
      // ``normalize_subreddit`` handle it instead.
      try {
        new URL(trimmedUrl)
      } catch {
        onError('url is not a valid URL')
        return
      }
    } else if (sourceType === 'reddit') {
      // Light client-side sanity: reject obviously-empty / wrong-shape
      // inputs so the user gets immediate feedback. Full validation
      // (subreddit regex, lowercasing) happens backend-side; we just
      // guard against the common "I typed `reddit.com/r/python`
      // without a scheme" footgun by accepting it (the backend
      // handles scheme-less inputs via ``normalize_subreddit``).
      if (trimmedUrl.length < 3) {
        onError('enter a subreddit like r/python or a full reddit.com/r/python URL')
        return
      }
    }
    setSubmitting(true)
    try {
      await api.createSource({
        name: trimmedName,
        type: sourceType,
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
          validator branch in ``submit``. Two radios sit next to the
          URL field so the user sees the choice immediately — a
          dropdown would work but adds a click and hides the most
          common choice (RSS). ``ml-auto`` on the radios block pushes
          the group to the right edge so the label + radios share a
          single visual row on mobile. */}
      <fieldset className="flex items-center gap-3">
        <legend className="sr-only">Source type</legend>
        <span className="text-ios-caption uppercase tracking-wide text-label-tertiary">Type</span>
        <label className="flex items-center gap-1 text-ios-caption text-label-primary">
          <input
            type="radio"
            name="fm-type"
            value="rss"
            checked={sourceType === 'rss'}
            onChange={() => setSourceType('rss')}
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
            onChange={() => setSourceType('reddit')}
            className="accent-accent"
          />
          Subreddit
        </label>
      </fieldset>
      <div>
        <label
          className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1"
          htmlFor="fm-url"
        >
          {sourceType === 'rss' ? 'RSS / Atom URL' : 'Subreddit'}
        </label>
        <input
          id="fm-url"
          type={sourceType === 'rss' ? 'url' : 'text'}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder={
            sourceType === 'rss'
              ? 'https://example.com/feed.xml'
              : 'r/python or https://www.reddit.com/r/python'
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
      <button
        type="submit"
        disabled={submitting}
        className="w-full min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white"
      >
        {submitting ? 'adding…' : 'Add feed'}
      </button>
    </form>
  )
}