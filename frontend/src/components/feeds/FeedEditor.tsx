// The feed editor, opened under a row in Settings → Feeds
// (REDESIGN_PLAN.md section 6). There is no Save button: each field
// saves on its own through lib/sourceSaves.ts — text fields when they
// lose focus or on Return, pickers when they change, headers when
// focus leaves them. Every save is one small PATCH, so a refused
// value says exactly which field and why.

import { useEffect, useId, useRef, useState } from 'react'
import { api, type Source } from '../../api'
import { CategoryPicker } from './CategoryPicker'
import { HeadersEditor } from './HeadersEditor'
import { FieldError, SaveStatus } from './SaveStatus'
import { useSaveState } from '../../lib/useSaveState'
import { usePreferences } from '../../lib/preferences'
import { REFRESH_PRESETS, isBuiltIn, normalizeFeedInput, refreshLabel } from '../../lib/feedRules'
import { BROWSER_USER_AGENT, type HeaderMap } from '../../lib/headers'
import { errorMessage, fieldSaveMessage, lastErrorMessage } from '../../lib/apiErrors'
import {
  clearError,
  commitField,
  currentSource,
  flushEditors,
  registerFlusher,
  retryField,
  whenIdle,
  type Field,
  type SaveState,
} from '../../lib/sourceSaves'
import { slugLabel, sourceLabel } from '../../lib/labels'
import { toast } from '../Toast'

// Types whose fetcher sends custom headers. Subreddits (and built-ins)
// ignore them, so the editor doesn't offer them there.
const HEADER_TYPES = ['rss', 'podcast', 'youtube_channel', 'generic_scrape']

const labelClass = 'text-ios-caption uppercase tracking-wide text-label-tertiary'
const fieldClass =
  'w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary read-only:opacity-70'
const actionClass =
  'min-h-[44px] px-3 rounded-ios border border-hairline text-ios-body active:opacity-60 disabled:opacity-40'

type Check = { ok: true; value: string | null } | { ok: false; message: string }

const busy = (s: SaveState) => s.status === 'saving' || s.status === 'slow'

function isWebAddress(v: string): boolean {
  try {
    const u = new URL(v)
    return u.protocol === 'http:' || u.protocol === 'https:'
  } catch {
    return false
  }
}

function LabelRow({ htmlFor, label, state }: { htmlFor?: string; label: string; state?: SaveState }) {
  return (
    <div className="flex items-center justify-between gap-2 mb-1">
      {htmlFor ? (
        <label htmlFor={htmlFor} className={labelClass}>
          {label}
        </label>
      ) : (
        <span className={labelClass}>{label}</span>
      )}
      {state && <SaveStatus state={state} />}
    </div>
  )
}

// One text field. The draft is local; it is committed on blur or
// Return, and reseeded from the saved value only while the field is
// not focused, not saving and not in error, so a poll never
// overwrites what is being typed.
function TextField({
  sourceId,
  field,
  label,
  saveLabel,
  saved,
  readOnly,
  check,
  hint,
  inputType = 'text',
  placeholder,
  autoCapitalize = 'none',
}: {
  sourceId: number
  field: Field
  label: string
  // How a failed-save toast names the field ("address").
  saveLabel: string
  saved: string | null
  readOnly?: boolean
  check: (draft: string) => Check
  hint?: (draft: string, savedAs: string | null) => string | null
  inputType?: 'text' | 'url'
  placeholder?: string
  autoCapitalize?: 'none' | 'words'
}) {
  const id = useId()
  const state = useSaveState(sourceId, field)
  const [draft, setDraft] = useState(() =>
    state.status === 'error' && typeof state.failedValue === 'string' ? state.failedValue : saved ?? '',
  )
  const [localError, setLocalError] = useState<string | null>(null)
  // What the server stored when it differs from what was sent (a
  // canonical subreddit, a resolved YouTube or Apple feed).
  const [savedAs, setSavedAs] = useState<string | null>(null)
  const focused = useRef(false)
  const sent = useRef<string | null>(null)
  const draftRef = useRef(draft)
  draftRef.current = draft
  const savedRef = useRef(saved)
  savedRef.current = saved

  useEffect(() => {
    if (focused.current || busy(state) || state.status === 'error') return
    if (state.status === 'saved' && sent.current != null && saved !== sent.current) setSavedAs(saved)
    setDraft(saved ?? '')
  }, [saved, state])

  const commit = (value: string) => {
    if (readOnly) return
    const r = check(value)
    if (!r.ok) {
      setLocalError(r.message)
      return
    }
    setLocalError(null)
    setSavedAs(null)
    // A cleaned-up value replaces the draft; null (clear) keeps what's
    // shown until the saved value comes back.
    if (r.value != null && r.value !== value) setDraft(r.value)
    sent.current = r.value
    commitField(sourceId, field, r.value, saveLabel)
  }

  // Commits a dirty, valid draft even if the field still has focus or
  // never blurs: Done, an app switch and Check now call it through
  // flushEditors, and closing the editor any other way (Back, another
  // Settings tab, the row's header) calls it on unmount.
  const flush = useRef(() => {})
  flush.current = () => {
    if (readOnly || draftRef.current === (savedRef.current ?? '')) return
    // The feed is gone (deleted here or elsewhere): nothing to save.
    if (!currentSource(sourceId)) return
    const r = check(draftRef.current)
    if (r.ok) {
      sent.current = r.value
      commitField(sourceId, field, r.value, saveLabel)
    }
  }
  useEffect(() => registerFlusher(() => flush.current()), [])
  useEffect(() => () => flush.current(), [])

  const undo = () => {
    setDraft(saved ?? '')
    setLocalError(null)
    clearError(sourceId, field)
  }
  // A hint about what the value will do is wrong once it was refused.
  const note = localError || state.status === 'error' ? null : hint?.(draft, savedAs)

  return (
    <div>
      <LabelRow htmlFor={id} label={label} state={state} />
      <input
        id={id}
        type={inputType}
        inputMode={inputType === 'url' ? 'url' : undefined}
        autoCapitalize={autoCapitalize}
        autoCorrect="off"
        spellCheck={false}
        enterKeyHint="done"
        readOnly={readOnly}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value)
          if (localError) setLocalError(null)
        }}
        onFocus={() => {
          focused.current = true
        }}
        onBlur={(e) => {
          focused.current = false
          commit(e.currentTarget.value)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            e.currentTarget.blur()
          }
        }}
        className={fieldClass}
      />
      {note && <p className="mt-1 text-ios-caption text-label-secondary break-words">{note}</p>}
      {localError && <FieldError message={localError} onUndo={undo} />}
      {state.status === 'error' && state.message && (
        <FieldError
          message={state.message}
          onUndo={undo}
          onRetry={state.retryable ? () => retryField(sourceId, field) : undefined}
        />
      )}
    </div>
  )
}

export function FeedEditor({
  source,
  sources,
  onDeleted,
  onRefresh,
}: {
  source: Source
  sources: Source[]
  onDeleted: (id: number) => void
  // A quiet refresh, so the row shows what Check now found.
  onRefresh: () => Promise<void>
}) {
  const id = source.id
  const builtIn = isBuiltIn(source)
  const type = source.type
  // The category picker lists categories in tab order.
  const tabOrder = usePreferences().state.categoryLayout?.order
  const takesHeaders = !builtIn && HEADER_TYPES.includes(type)
  const label = source.display_name || slugLabel(source.name)

  const categoryState = useSaveState(id, 'category')
  const refreshState = useSaveState(id, 'refresh_interval_seconds')
  const activeState = useSaveState(id, 'active')
  const headersState = useSaveState(id, 'custom_headers')
  const pending = <T,>(s: SaveState, saved: T): T => (busy(s) ? (s.pending as T) : saved)

  const [showRaw, setShowRaw] = useState(false)
  const [checking, setChecking] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [headersReseed, setHeadersReseed] = useState(0)

  // --- field rules (validated before any PATCH; nothing is sent on a
  // failure and the draft stays) ---------------------------------------
  // Any text the user likes; the internal slug never changes. Typing the
  // label the feed already shows saves nothing.
  const checkDisplayName = (v: string): Check => {
    const t = v.split(/\s+/).filter(Boolean).join(' ')
    if (!t) return { ok: false, message: 'Type a name.' }
    if (t.length > 120) return { ok: false, message: 'Keep it to 120 characters or fewer.' }
    if (!source.display_name && t === slugLabel(source.name)) return { ok: true, value: null }
    return { ok: true, value: t }
  }
  const checkAddress = (v: string): Check => {
    const t = v.trim()
    if (type === 'reddit') return t ? { ok: true, value: t } : { ok: false, message: 'Type a subreddit, like r/python.' }
    const n = normalizeFeedInput(type, t)
    if (!isWebAddress(n)) {
      return {
        ok: false,
        message:
          type === 'youtube_channel'
            ? 'Paste a channel link, video link, @handle or channel ID.'
            : 'Use a web address that starts with https://',
      }
    }
    return { ok: true, value: n }
  }
  const checkSitemap = (v: string): Check => {
    const t = v.trim()
    if (!t) return { ok: true, value: null }
    const n = normalizeFeedInput('generic_scrape', t)
    return isWebAddress(n) ? { ok: true, value: n } : { ok: false, message: 'Use a web address that starts with https://' }
  }
  const checkLinkPattern = (v: string): Check => {
    const t = v.trim()
    if (!t) return { ok: true, value: null }
    return t.startsWith('/') && !t.includes('://') ? { ok: true, value: t } : { ok: false, message: 'Start it with /' }
  }

  // --- Last problem --------------------------------------------------
  const lastError = source.last_error
  const hasUserAgent = Object.keys(source.custom_headers ?? {}).some((k) => k.toLowerCase() === 'user-agent')
  const offerIdentity =
    !!lastError && takesHeaders && !hasUserAgent && (/^\[blocked\]/.test(lastError) || /\b40[123]\b/.test(lastError))
  const useBrowserIdentity = () => {
    // On top of the newest headers, including a save still under way.
    const base = pending<HeaderMap | null>(headersState, source.custom_headers)
    const map: HeaderMap = { ...(base ?? {}), 'User-Agent': BROWSER_USER_AGENT }
    commitField(id, 'custom_headers', map, 'headers')
  }

  // --- actions -----------------------------------------------------------
  const active = pending(activeState, source.active)
  const checkNow = async () => {
    // Commit an edited field first, even if the tap didn't blur it, and
    // check with what was saved.
    ;(document.activeElement as HTMLElement | null)?.blur()
    flushEditors()
    setChecking(true)
    try {
      await whenIdle(id)
      const name = currentSource(id)?.name ?? source.name
      const r = await api.ingest(name)
      if (r.error) toast(`Couldn't check ${sourceLabel(name)}: ${lastErrorMessage(r.error)}`, 'error')
      else toast(`Checked ${sourceLabel(name)}: ${r.inserted} new`, 'info')
      await onRefresh()
    } catch (err) {
      toast(`Couldn't check ${label}: ${errorMessage(err)}`, 'error')
    } finally {
      setChecking(false)
    }
  }
  const deleteFeed = async () => {
    setDeleting(true)
    setDeleteError(null)
    try {
      await whenIdle(id)
      await api.deleteSource(id)
      toast(`Deleted ${label}`, 'info')
      onDeleted(id)
    } catch (err) {
      if ((err as { status?: number }).status === 404) {
        onDeleted(id)
        return
      }
      setDeleteError(fieldSaveMessage(err, 'name', source.name, sourceLabel))
      setDeleting(false)
    }
  }

  const refreshValue = pending(refreshState, source.refresh_interval_seconds)
  const isPreset = REFRESH_PRESETS.some((p) => p.value === refreshValue)

  return (
    <div className="pt-1 pb-4 space-y-4">
      {lastError && (
        <div className="rounded-ios border border-red-500/40 bg-red-500/10 p-3 space-y-2">
          <p className="text-ios-caption text-red-200 break-words">
            <span className="font-semibold">Last problem: </span>
            {lastErrorMessage(lastError)}
          </p>
          {source.auto_disabled && (
            <p className="text-ios-caption text-red-200 break-words">
              Popping stopped checking after {source.error_count} failed tries. Fix the address or headers, then tap
              Resume feed.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {offerIdentity && (
              <button type="button" onClick={useBrowserIdentity} className={`${actionClass} text-accent`}>
                Use a browser identity
              </button>
            )}
            <button
              type="button"
              aria-expanded={showRaw}
              onClick={() => setShowRaw((v) => !v)}
              className={`${actionClass} text-label-primary`}
            >
              {showRaw ? 'Less' : 'More'}
            </button>
          </div>
          {showRaw && <p className="text-ios-caption text-label-secondary break-all">{lastError}</p>}
        </div>
      )}

      <TextField
        sourceId={id}
        field="display_name"
        label="Name"
        saveLabel="name"
        saved={source.display_name ?? slugLabel(source.name)}
        check={checkDisplayName}
        autoCapitalize="words"
      />

      <TextField
        sourceId={id}
        field="url"
        label={type === 'reddit' ? 'Subreddit' : 'Address'}
        saveLabel="address"
        saved={source.url}
        readOnly={builtIn}
        inputType={type === 'reddit' ? 'text' : 'url'}
        check={checkAddress}
        hint={(draft, savedAs) =>
          savedAs
            ? type === 'youtube_channel'
              ? "Saved as the channel's feed link."
              : `Saved as ${savedAs}`
            : !builtIn && draft.trim() !== source.url
              ? 'Its icon refreshes on the next check.'
              : null
        }
      />

      <div>
        <LabelRow htmlFor={`fe-category-${id}`} label="Category" state={categoryState} />
        <CategoryPicker
          inputId={`fe-category-${id}`}
          value={pending(categoryState, source.category)}
          sources={sources}
          onChange={(v) => commitField(id, 'category', v, 'category')}
          order={tabOrder}
        />
        {categoryState.status === 'error' && categoryState.message && (
          <FieldError
            message={categoryState.message}
            onUndo={() => clearError(id, 'category')}
            onRetry={categoryState.retryable ? () => retryField(id, 'category') : undefined}
          />
        )}
      </div>

      <div>
        <LabelRow htmlFor={builtIn ? undefined : `fe-refresh-${id}`} label="Check for new items" state={refreshState} />
        {builtIn ? (
          // The scheduler runs built-ins on their plugin's own interval
          // and ignores this column, so a picker would save a no-op.
          <p className="text-ios-body text-label-secondary">
            Checked every {refreshLabel(source.refresh_interval_seconds)} (built-in feed)
          </p>
        ) : (
          <select
            id={`fe-refresh-${id}`}
            value={refreshValue}
            onChange={(e) => commitField(id, 'refresh_interval_seconds', Number(e.target.value), 'check interval')}
            className="select-ios w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline pl-3 pr-8 text-ios-body text-label-primary"
          >
            {!isPreset && <option value={refreshValue}>every {refreshLabel(refreshValue)}</option>}
            {REFRESH_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                every {p.label}
              </option>
            ))}
          </select>
        )}
        {refreshState.status === 'error' && refreshState.message && (
          <FieldError
            message={refreshState.message}
            onUndo={() => clearError(id, 'refresh_interval_seconds')}
            onRetry={refreshState.retryable ? () => retryField(id, 'refresh_interval_seconds') : undefined}
          />
        )}
      </div>

      {takesHeaders && (
        <div>
          <LabelRow label="Headers" state={headersState} />
          {type === 'generic_scrape' && (
            <p className="mb-2 text-ios-caption text-label-secondary">
              Used when Popping opens each article. The site&apos;s page list is still read without these.
            </p>
          )}
          <HeadersEditor
            mode="autosave"
            value={source.custom_headers}
            onCommit={(map) => commitField(id, 'custom_headers', map, 'headers')}
            busy={busy(headersState)}
            failed={headersState.status === 'error'}
            reseedToken={headersReseed}
          />
          {headersState.status === 'error' && headersState.message && (
            <FieldError
              message={headersState.message}
              onUndo={() => {
                clearError(id, 'custom_headers')
                setHeadersReseed((n) => n + 1)
              }}
              onRetry={headersState.retryable ? () => retryField(id, 'custom_headers') : undefined}
            />
          )}
        </div>
      )}

      {type === 'generic_scrape' && !builtIn && (
        <>
          <TextField
            sourceId={id}
            field="sitemap_url"
            label="Sitemap address"
            saveLabel="sitemap address"
            saved={source.sitemap_url}
            inputType="url"
            placeholder="Found automatically when empty"
            check={checkSitemap}
          />
          <TextField
            sourceId={id}
            field="link_pattern"
            label="Only links starting with"
            saveLabel="link filter"
            saved={source.link_pattern}
            placeholder="/news/"
            check={checkLinkPattern}
          />
        </>
      )}

      <div className="space-y-2">
        {activeState.status === 'error' && activeState.message && (
          <FieldError
            message={activeState.message}
            onUndo={() => clearError(id, 'active')}
            onRetry={activeState.retryable ? () => retryField(id, 'active') : undefined}
          />
        )}
        {confirmDelete ? (
          <div className="rounded-ios border border-hairline p-3 space-y-2">
            <p className="text-ios-body text-label-primary break-words">
              Delete {label}? Its stories go with it{builtIn ? ', and Popping won’t add it back' : ''}.
            </p>
            {deleteError && <FieldError message={deleteError} />}
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => void deleteFeed()}
                disabled={deleting}
                className="min-h-[44px] px-3 rounded-ios bg-red-500 text-white text-ios-body active:opacity-80 disabled:opacity-40"
              >
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
              {active && (
                <button
                  type="button"
                  onClick={() => {
                    commitField(id, 'active', false, 'pause setting')
                    setConfirmDelete(false)
                  }}
                  disabled={deleting}
                  className={`${actionClass} text-accent`}
                >
                  Pause instead
                </button>
              )}
              <button
                type="button"
                onClick={() => setConfirmDelete(false)}
                disabled={deleting}
                className={`${actionClass} text-label-primary`}
              >
                Keep
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => commitField(id, 'active', !active, 'pause setting')}
              className={`${actionClass} text-accent`}
            >
              {active ? 'Pause feed' : 'Resume feed'}
            </button>
            <button
              type="button"
              onClick={() => void checkNow()}
              disabled={checking}
              className={`${actionClass} text-accent`}
            >
              {checking ? 'Checking…' : 'Check now'}
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              className={`${actionClass} text-red-300`}
            >
              Delete feed…
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
