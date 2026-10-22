// Settings › Feeds: adding, editing and removing feeds. Three tabs:
//
//   • My feeds — the feed list. Tapping a row opens its editor
//     (feeds/FeedEditor.tsx), which saves each field on its own:
//     name, address, category, how often to check, headers, pause,
//     check now and delete. Built-in feeds keep their name, address,
//     headers and interval; they can still be paused, recategorised
//     and deleted (the backend records the deletion so the plugin
//     doesn't recreate the row).
//
//   • Suggested — curated list from GET /api/feed-recommendations,
//     minus anything the user already has. Tap "Add" to fire
//     POST /api/sources.
//
//   • Add — the Auto feed box (paste any link) and the manual form
//     (name, address, type, category, interval, headers). The backend
//     is the source of truth on errors and returns 422 with
//     field-level details, shown under the field.
//
// Add has a "Test" button next to "Add feed" so the user can probe
// the URL with the same plugin Add would use, no DB write. The result
// is shown inline: "Looks good (42 items)" with sample titles, or the
// reason it failed with the raw error. Test doesn't change the form.
//
// Every add shows the feed at once (``onSourceAdded``) and its toast
// has an Edit action that opens the new feed's editor.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Source } from '../api'
import { SourceIcon } from './SourceIcon'
import { CategoryPicker } from './feeds/CategoryPicker'
import { FeedEditor } from './feeds/FeedEditor'
import { HeadersEditor } from './feeds/HeadersEditor'
import { categoryLabel, sourceLabel } from '../lib/labels'
import {
  PODCAST_DEFAULT_REFRESH,
  REFRESH_PRESETS,
  YOUTUBE_DEFAULT_REFRESH,
  classifyPaste,
  cleanDisplayName,
  isStale,
  normalizeFeedInput,
  normalizeName,
  refreshLabel,
  suggestName,
  timeAgo,
  uniqueSlug,
} from '../lib/feedRules'
import { CATEGORY_OPTIONS, resolveNewCategory } from '../lib/categories'
import { type HeaderMap } from '../lib/headers'
import { fieldSaveMessage, friendlyTestError, parseApiError } from '../lib/apiErrors'
import { toast } from './Toast'
import { usePreferences } from '../lib/preferences'

type Props = {
  sources: Source[]
  // A quiet refresh of App's sources and stories.
  onRefresh: () => Promise<void>
  onError: (msg: string) => void
  // Open this feed's row (the Menu's ›, a card's source name, a
  // ?source= link). A new ``focusNonce`` repeats the request for the
  // same feed.
  focusSourceId?: number | null
  focusNonce?: number
  // A feed was added here: App shows it at once, before the next poll.
  onSourceAdded?: (row: Source) => void
  // A feed was deleted from its editor.
  onSourceDeleted?: (id: number) => void
  // Open a feed's editor (the Edit action on an "Added …" toast).
  onEditSource?: (id: number) => void
}

type Tab = 'mine' | 'recommended' | 'add'

// After an add: the row (to show it at once) and the toast to show.
type OnCreated = (row: Source, message: string) => void

// The open row is part of the URL only until the user moves on: then
// ``source`` goes, keeping the history entry's state (App's Done reads
// it).
function clearSourceParam() {
  const u = new URL(window.location.href)
  if (!u.searchParams.has('source')) return
  u.searchParams.delete('source')
  window.history.replaceState(window.history.state, '', u.toString())
}

export function FeedManager({
  sources,
  onRefresh,
  onError,
  focusSourceId,
  focusNonce,
  onSourceAdded,
  onSourceDeleted,
  onEditSource,
}: Props) {
  const [tab, setTab] = useState<Tab>('mine')
  // One open row at a time, so the Menu's › can open a given feed.
  const [openRowId, setOpenRowId] = useState<number | null>(null)
  const pendingScroll = useRef<number | null>(null)

  // Asked to show a feed: My feeds, that row open, scrolled to the top
  // of the sheet once it has rendered.
  useEffect(() => {
    if (focusSourceId == null) return
    setTab('mine')
    setOpenRowId(focusSourceId)
    pendingScroll.current = focusSourceId
  }, [focusSourceId, focusNonce])
  useEffect(() => {
    const id = pendingScroll.current
    if (id == null) return
    const row = document.getElementById(`feed-${id}`)
    if (!row) return
    pendingScroll.current = null
    row.scrollIntoView({ block: 'start' })
  })

  const toggleRow = (id: number) => {
    setOpenRowId((cur) => (cur === id ? null : id))
    clearSourceParam()
  }
  const deleted = (id: number) => {
    setOpenRowId(null)
    clearSourceParam()
    if (onSourceDeleted) onSourceDeleted(id)
    else void onRefresh()
  }
  // Every add shows the feed at once and offers its editor.
  const created: OnCreated = (row, message) => {
    onSourceAdded?.(row)
    toast(message, onEditSource ? { kind: 'info', action: { label: 'Edit', onClick: () => onEditSource(row.id) } } : 'info')
  }
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
          My feeds
        </TabButton>
        <TabButton active={tab === 'recommended'} onClick={() => setTab('recommended')}>
          Suggested
        </TabButton>
        <TabButton active={tab === 'add'} onClick={() => setTab('add')}>
          Add
        </TabButton>
      </div>

      {tab === 'mine' && (
        <MyFeedsTab
          sources={sources}
          openRowId={openRowId}
          onToggleRow={toggleRow}
          onRefresh={onRefresh}
          onDeleted={deleted}
        />
      )}
      {tab === 'recommended' && (
        <RecommendedTab
          sources={sources}
          existingNames={existingNames}
          onAdded={onRefresh}
          onCreated={created}
          onError={onError}
        />
      )}
      {tab === 'add' && (
        <AddCustomTab sources={sources} onAdded={onRefresh} onCreated={created} />
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
      className={`flex-1 min-w-0 min-h-[44px] px-1 rounded-ios text-ios-body max-[359px]:text-[15px] whitespace-nowrap truncate transition ${
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
// My feeds — the source list; tapping a row opens its editor
// ---------------------------------------------------------------------------

function MyFeedsTab({
  sources,
  openRowId,
  onToggleRow,
  onRefresh,
  onDeleted,
}: {
  sources: Source[]
  openRowId: number | null
  onToggleRow: (id: number) => void
  onRefresh: () => Promise<void>
  onDeleted: (id: number) => void
}) {
  // While a row is open the list keeps the order it had when the row
  // opened (new feeds go at the end): the server sorts by category and
  // name, so a rename or a new category would otherwise move the row
  // being edited out from under the finger.
  const frozen = useRef<{ open: number; order: number[] } | null>(null)
  if (openRowId == null) frozen.current = null
  else if (frozen.current?.open !== openRowId) frozen.current = { open: openRowId, order: sources.map((s) => s.id) }
  const order = frozen.current?.order
  const shown = useMemo(() => {
    if (!order) return sources
    const byId = new Map(sources.map((s) => [s.id, s]))
    const kept = order.flatMap((id) => (byId.has(id) ? [byId.get(id)!] : []))
    return [...kept, ...sources.filter((s) => !order.includes(s.id))]
  }, [sources, order])

  if (sources.length === 0) {
    return <p className="text-ios-body text-label-secondary italic px-1">No feeds yet. Add one from Suggested or Add.</p>
  }
  return (
    <ul className="space-y-1">
      {shown.map((s) => (
        <li key={s.id} id={`feed-${s.id}`} className="scroll-mt-2">
          <SourceRow source={s} open={openRowId === s.id} onToggle={() => onToggleRow(s.id)} />
          {openRowId === s.id && (
            <FeedEditor source={s} sources={sources} onDeleted={onDeleted} onRefresh={onRefresh} />
          )}
        </li>
      ))}
    </ul>
  )
}

// One feed in the list: name, then a caption with its category, how
// fresh it is and any problem. The whole line is the button that opens
// the editor.
function SourceRow({
  source,
  open,
  onToggle,
}: {
  source: Source
  open: boolean
  onToggle: () => void
}) {
  const stale = isStale(source)
  return (
    <div className="text-ios-caption border-b border-hairline">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full min-h-[44px] flex items-center gap-2 min-w-0 text-left"
      >
        {/* SourceIcon — colored-letter fallback when the favicon
            hasn't landed yet. */}
        <SourceIcon src={source.favicon_path} name={source.name} size={14} />
        <span
          className={`min-w-0 truncate text-ios-body font-medium ${
            !source.active ? 'text-label-secondary line-through' : stale ? 'text-label-secondary italic' : 'text-label-primary'
          }`}
        >
          {sourceLabel(source.name)}
        </span>
        <span
          aria-hidden="true"
          className={`ml-auto shrink-0 text-label-tertiary transition-transform ${open ? 'rotate-90' : ''}`}
        >
          ›
        </span>
      </button>
      {/* Caption line, always shown: category, freshness, then status.
          It sits under the name rather than beside it, so the name
          keeps the width it needs on a phone; it wraps when long. */}
      <div className="-mt-2 pb-2 pl-[22px] flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className="max-w-full truncate text-label-secondary">{categoryLabel(source.category)}</span>
        <span className={stale ? 'text-amber-400' : 'text-label-tertiary'}>
          updated {timeAgo(source.last_fetch_at)} · every {refreshLabel(source.refresh_interval_seconds)}
        </span>
        {/* Stopped by the scheduler (after repeated failures) reads
            differently from paused by the user: the first needs a look
            at the problem before resuming. */}
        {source.auto_disabled ? (
          <span className="shrink-0 text-red-300">stopped after {source.error_count} failed checks</span>
        ) : (
          !source.active && <span className="shrink-0 text-amber-400">paused</span>
        )}
        {!source.auto_disabled && source.error_count > 0 && (
          <span className="shrink-0 text-red-300">⚠ {source.error_count > 1 ? source.error_count : ''}</span>
        )}
        {/* Net vote score across this feed's stories; only shown when
            it isn't 0. Negative is the one worth noticing. */}
        {source.net_vote_score !== 0 && (
          <span className={`shrink-0 font-semibold ${source.net_vote_score < 0 ? 'text-red-300' : 'text-emerald-400'}`}>
            {source.net_vote_score > 0 ? '+' : ''}
            {source.net_vote_score}
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
  sources,
  existingNames,
  onAdded,
  onCreated,
  onError,
}: {
  sources: Source[]
  existingNames: Set<string>
  onAdded: () => Promise<void>
  onCreated: OnCreated
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
      const where = categoryLabel(result.category)
      setDiscoverMsg(
        result.added > 0
          ? `Found ${result.added} new feed${result.added === 1 ? '' : 's'} for ${where}`
          : result.note
            ? `No new feeds for ${where}: ${result.note}`
            : `Nothing new for ${where} this time`,
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
    // An existing spelling of the category ("News") is reused, so the
    // feed joins that column instead of starting a second one.
    const category = resolveCategory(rec.category, sources)
    try {
      const row = await api.createSource({
        name: rec.name,
        type: rec.type ?? 'rss',
        category,
        url: rec.url,
        refresh_interval_seconds: isReddit ? 900 : 3600,
        // Recommendation-supplied header overrides (e.g. CBC's
        // browser UA). Falls through to the route's ``None``
        // branch when the recommendation doesn't ship one —
        // omitting the field leaves ``custom_headers`` at the
        // backend default (empty map → use source-plugin defaults).
        custom_headers: rec.default_headers ?? undefined,
      })
      onCreated(row, `Added ${sourceLabel(rec.name)} to ${categoryLabel(category)}`)
      await onAdded()
      // Optimistically remove from the local list so the row
      // disappears immediately rather than after the next refresh.
      setRecs((prev) => (prev ? prev.filter((r) => r.name !== rec.name) : prev))
    } catch (err) {
      toast(`Couldn't add ${sourceLabel(rec.name)}: ${fieldSaveMessage(err, 'name', rec.name, sourceLabel)}`, 'error')
    } finally {
      setAdding(null)
    }
  }

  // "Find more feeds" header — shown above the list regardless of
  // loading/empty state so the user can trigger discovery even when
  // the curated pool is fully added (the most likely time they'd
  // actually want it).
  const findMoreHeader = (
    <div className="pt-1 pb-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-hairline">
      <button
        onClick={findMore}
        disabled={discovering}
        className="shrink-0 min-h-[44px] rounded-ios border border-accent text-accent active:opacity-60 disabled:opacity-40 px-3 text-ios-caption"
      >
        {discovering ? 'Looking…' : '🔎 Find more feeds'}
      </button>
      {discoverMsg && (
        <span className="min-w-0 text-ios-caption text-label-tertiary break-words">{discoverMsg}</span>
      )}
    </div>
  )

  if (loading) {
    return (
      <>
        {findMoreHeader}
        <p className="text-ios-body text-label-secondary italic py-2">Loading…</p>
      </>
    )
  }
  if (!recs || recs.length === 0) {
    return (
      <>
        {findMoreHeader}
        <p className="text-ios-body text-label-secondary italic py-2">
          You've added every suggested feed. Try Find more feeds, or the Add tab.
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
            className="py-2 text-ios-caption border-b border-hairline last:border-b-0"
          >
            <div className="flex items-center gap-2 min-w-0">
              <span className="min-w-0 truncate text-ios-body font-medium text-label-primary">{sourceLabel(r.name)}</span>
              <span className="text-label-tertiary shrink-0 text-[11px]">{categoryLabel(r.category)}</span>
              {r.source === 'llm' && (
                <span className="shrink-0 inline-flex items-center rounded-full bg-accent-soft px-1.5 text-[11px] uppercase tracking-wide text-accent">
                  found for you
                </span>
              )}
              <button
                onClick={() => add(r)}
                disabled={adding === r.name}
                className="ml-auto shrink-0 min-h-[44px] min-w-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white px-3 text-ios-caption"
                aria-label={`add ${sourceLabel(r.name)}`}
              >
                {adding === r.name ? 'Adding…' : 'Add'}
              </button>
            </div>
            <p className="text-ios-caption text-label-secondary mt-0.5 break-words">{r.blurb}</p>
          </li>
        ))}
      </ul>
    </>
  )
}

// ---------------------------------------------------------------------------
// Auto feed — one link in, one source out. Sits above the manual form
// as a shortcut: paste any site's link (not necessarily a feed) and the
// backend finds its real feed, or falls back to checking the page for
// new articles if it has none. YouTube links, @handles and channel IDs
// go to the backend's YouTube branch; a subreddit or an Apple Podcasts
// page fills in the form below instead, since those need its type.
// The source's name comes from the link; rename it afterward from the
// row list like any other source.
// ---------------------------------------------------------------------------

type FeedType = 'rss' | 'reddit' | 'podcast' | 'youtube_channel' | 'generic_scrape'

// An existing spelling of a default category ("Video", "News") is
// reused, so a new feed doesn't start a second column with the same
// label (columns group by the stored string).
function resolveCategory(category: string, sources: Source[]): string {
  const r = resolveNewCategory(category, sources.map((s) => s.category), CATEGORY_OPTIONS, categoryLabel)
  return r.ok ? r.value : category
}

function AutoFeedBlock({
  sources,
  onAdded,
  onCreated,
  onPrefill,
}: {
  sources: Source[]
  onAdded: () => Promise<void>
  onCreated: OnCreated
  onPrefill: (type: 'reddit' | 'podcast', url: string) => void
}) {
  const [autoUrl, setAutoUrl] = useState('')
  const [autoSubmitting, setAutoSubmitting] = useState(false)
  // Track-anyway path. ``trackSubmitting`` is true while the
  // generic_scrape POST is in flight. Surfaced as a button next
  // to the "couldn't find a feed" error so the user has a path
  // forward when auto-discovery gives up.
  const [trackSubmitting, setTrackSubmitting] = useState(false)
  const [autoMessage, setAutoMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const trackAnyway = async () => {
    const trimmed = normalizeFeedInput('generic_scrape', autoUrl)
    if (!trimmed) return
    setTrackSubmitting(true)
    try {
      // Mirror the backend's ``_slugify_hostname`` so the name
      // matches what Auto-add would have produced for the same
      // link, then step past names already taken (POST answers 409
      // on a duplicate name).
      const host = (() => {
        try {
          return new URL(trimmed).hostname || 'site'
        } catch {
          return 'site'
        }
      })().toLowerCase().replace(/^www\./, '')
      const base = (host.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'site').slice(0, 100)
      const taken = new Set(sources.map((s) => s.name))
      let name = base
      for (let n = 2; taken.has(name); n++) name = `${base}_${n}`
      const category = resolveCategory('news', sources)
      const row = await api.createSource({
        name,
        type: 'generic_scrape',
        category,
        url: trimmed,
        refresh_interval_seconds: 7200,
      })
      onCreated(row, `Added ${sourceLabel(name)} to ${categoryLabel(category)}. Popping will check the page for new articles.`)
      setAutoUrl('')
      setAutoMessage(null)
      await onAdded()
    } catch (err) {
      setAutoMessage({ kind: 'err', text: fieldSaveMessage(err, 'url', trimmed, sourceLabel) })
    } finally {
      setTrackSubmitting(false)
    }
  }

  const autoAdd = async () => {
    const trimmed = autoUrl.trim()
    if (!trimmed) {
      setAutoMessage({ kind: 'err', text: 'Paste a link first.' })
      return
    }
    const kind = classifyPaste(trimmed)
    if (kind === 'reddit' || kind === 'podcast') {
      onPrefill(kind, trimmed)
      setAutoUrl('')
      setAutoMessage(null)
      return
    }
    setAutoSubmitting(true)
    setAutoMessage(null)
    try {
      const result =
        kind === 'youtube'
          ? await api.autoAddSource(trimmed, resolveCategory('video', sources))
          : await api.autoAddSource(normalizeFeedInput('rss', trimmed), resolveCategory('other', sources))
      const added = result.found ? result.source : null
      if (added) {
        const label = sourceLabel(added.name)
        if (sources.some((s) => s.id === added.id)) {
          onCreated(added, `Already added: ${label}`)
        } else {
          const note = result.kind === 'generic_scrape' ? '. It has no feed, so Popping will check the page for new articles.' : ''
          onCreated(added, `Added ${label} to ${categoryLabel(added.category)}${note}`)
        }
        setAutoUrl('')
        await onAdded()
      } else {
        setAutoMessage({
          kind: 'err',
          text: "Popping couldn't find a feed or a page list at that address.",
        })
      }
    } catch (err) {
      setAutoMessage({ kind: 'err', text: fieldSaveMessage(err, 'url', trimmed, sourceLabel) })
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
        Paste any site's link. Popping finds its feed, or checks the page for new articles if it has none. YouTube
        links, @handles and channel IDs work too.
      </p>
      <div className="flex items-center gap-2">
        <input
          type="url"
          inputMode="url"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="go"
          aria-label="link to add"
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
          className="flex-1 min-w-0 min-h-[44px] rounded-ios bg-bg-surface border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary disabled:opacity-50"
        />
        <button
          type="button"
          onClick={() => void autoAdd()}
          disabled={autoSubmitting}
          className="shrink-0 min-h-[44px] rounded-ios bg-accent text-white px-3 text-ios-caption font-medium active:opacity-70 disabled:opacity-40"
        >
          {autoSubmitting ? 'Looking…' : 'Auto-add'}
        </button>
      </div>
      {autoMessage && (
        <div className="space-y-2">
          <p className={`text-ios-caption break-words ${autoMessage.kind === 'err' ? 'text-red-300' : 'text-label-secondary'}`}>
            {autoMessage.text}
          </p>
          {autoMessage.kind === 'err' &&
            autoMessage.text.includes("couldn't find a feed") && (
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <button
                  type="button"
                  onClick={() => void trackAnyway()}
                  disabled={trackSubmitting || !autoUrl.trim()}
                  className="shrink-0 min-h-[44px] rounded-ios bg-bg-surface border border-hairline px-3 text-ios-caption font-medium text-label-primary active:opacity-70 disabled:opacity-40"
                >
                  {trackSubmitting ? 'Tracking…' : 'Track this URL anyway'}
                </button>
                <span className="text-ios-caption text-label-tertiary">
                  Popping checks the page for new articles instead
                </span>
              </div>
            )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add — the manual form
// ---------------------------------------------------------------------------

// Types whose fetcher sends custom headers; subreddits ignore them.
const HEADER_TYPES: FeedType[] = ['rss', 'podcast', 'youtube_channel', 'generic_scrape']

// A Test whose YouTube lookup worked but whose feed fetch didn't.
const YOUTUBE_FEED_DOWN_KINDS = ['not_found', 'timeout', 'network_error', 'unknown']

type TestOutcome = {
  kind: 'ok' | 'err'
  message: string
  itemCount?: number
  sampleTitles?: string[]
  // The feed the server resolved the link to (a YouTube channel or
  // playlist, an Apple Podcasts show). Add sends it, so the server
  // doesn't look the link up a second time.
  resolvedUrl?: string
}

const fieldClass =
  'w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline px-3 text-ios-body text-label-primary placeholder:text-label-tertiary'
const labelClass = 'block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1'
const radioLabelClass = 'flex items-center gap-2 min-h-[44px] text-ios-body text-label-primary'

function isWebAddress(v: string): boolean {
  try {
    const u = new URL(v)
    return u.protocol === 'http:' || u.protocol === 'https:'
  } catch {
    return false
  }
}

function AddCustomTab({
  sources,
  onAdded,
  onCreated,
}: {
  sources: Source[]
  onAdded: () => Promise<void>
  onCreated: OnCreated
}) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [category, setCategory] = useState('news')
  // The category picker lists categories in tab order.
  const tabOrder = usePreferences().state.categoryLayout?.order
  const [refresh, setRefresh] = useState<number>(3600)
  // Subtype selector — drives the address label, the validation and
  // the request. ``"rss"`` (default) takes a feed URL; ``"reddit"`` a
  // subreddit reference (``r/python`` or a reddit.com/r/python link);
  // ``"podcast"`` a podcast feed or an Apple Podcasts show page;
  // ``"youtube_channel"`` any YouTube link, @handle or channel ID,
  // which the backend resolves to the channel's video feed;
  // ``"generic_scrape"`` a page without a feed.
  const [sourceType, setSourceType] = useState<
    'rss' | 'reddit' | 'podcast' | 'youtube_channel' | 'generic_scrape'
  >('rss')
  // Slice 20: optional discovery hints for generic_scrape sources.
  // ``sitemapUrl`` is a direct sitemap URL (parsed by the plugin
  // instead of asking trafilatura to discover the sitemap from
  // the page). ``linkPattern`` is a path-prefix filter for the
  // page_links fallback (the plugin fetches self.url and extracts
  // <a href> URLs matching this prefix when sitemap discovery
  // returns nothing). Both empty by default; only meaningful when
  // sourceType='generic_scrape' is selected.
  const [sitemapUrl, setSitemapUrl] = useState('')
  const [linkPattern, setLinkPattern] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // Test step. ``testResult`` holds the last result so the user can
  // see it until they edit the form again; it clears on any change to
  // the name, address or type so a stale "Looks good" isn't trusted.
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestOutcome | null>(null)
  // Inline validation, shown under each field. The address error
  // waits until the field has been left once, so it doesn't nag
  // while the address is being typed.
  const [urlError, setUrlError] = useState<string | null>(null)
  const [nameError, setNameError] = useState<string | null>(null)
  const [urlTouched, setUrlTouched] = useState(false)
  // True while the New-category field holds text not yet added. Add
  // waits, or the feed would land in the previous category.
  const [categoryDraft, setCategoryDraft] = useState(false)
  const [headersOpen, setHeadersOpen] = useState(false)
  const [headers, setHeaders] = useState<HeaderMap | null>(null)
  const [headersOk, setHeadersOk] = useState(true)
  // Bumped to start the headers over (after Add, or on Subreddit).
  const [headersKey, setHeadersKey] = useState(0)
  const formRef = useRef<HTMLFormElement>(null)

  const existingNames = useMemo(() => new Set(sources.map((s) => s.name)), [sources])
  const takesHeaders = HEADER_TYPES.includes(sourceType)
  const headersBlocked = takesHeaders && !headersOk
  const normalized = normalizeFeedInput(sourceType, url)
  const suggested = name.trim() ? null : suggestName(sourceType, normalized, existingNames, testResult?.resolvedUrl)

  const chooseType = (t: FeedType) => {
    setSourceType(t)
    // Picking "Podcast" or "YouTube" bumps the refresh default to 6h
    // (episodes and videos are infrequent; see _PODCAST_DEFAULT_REFRESH
    // and _YOUTUBE_DEFAULT_REFRESH on the backend) unless the user has
    // already changed it from the plain-RSS default. 2h is the
    // backend default for generic_scrape: one poll can mean several
    // page fetches.
    if (t === 'rss') {
      if (refresh === PODCAST_DEFAULT_REFRESH) setRefresh(3600)
    } else if (t === 'reddit') {
      if (refresh === PODCAST_DEFAULT_REFRESH) setRefresh(900)
      setHeaders(null)
      setHeadersOk(true)
      setHeadersOpen(false)
      setHeadersKey((k) => k + 1)
    } else if (t === 'podcast') {
      if (refresh === 3600) setRefresh(PODCAST_DEFAULT_REFRESH)
      if (category === 'news') setCategory(resolveCategory('podcast', sources))
    } else if (t === 'youtube_channel') {
      if (refresh === 3600) setRefresh(YOUTUBE_DEFAULT_REFRESH)
      if (category === 'news') setCategory(resolveCategory('video', sources))
    } else if (t === 'generic_scrape') {
      if (refresh === 3600 || refresh === PODCAST_DEFAULT_REFRESH) setRefresh(7200)
    }
  }

  // Auto feed handed over a subreddit or an Apple Podcasts link.
  const prefill = (t: 'reddit' | 'podcast', link: string) => {
    chooseType(t)
    setUrl(link)
    requestAnimationFrame(() => formRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }

  // Shared validator for Test and Add. Returns ``null`` on success,
  // the message to show on failure.
  const validateForm = (): string | null => {
    const trimmedUrl = normalizeFeedInput(sourceType, url)
    if (cleanDisplayName(name).length > 120) return 'Keep the name to 120 characters or fewer.'
    if (!trimmedUrl) return sourceType === 'reddit' ? 'Type a subreddit, like r/python.' : 'Paste the address first.'
    if (
      sourceType === 'rss' ||
      sourceType === 'podcast' ||
      sourceType === 'youtube_channel' ||
      sourceType === 'generic_scrape'
    ) {
      if (!isWebAddress(trimmedUrl)) {
        return sourceType === 'youtube_channel'
          ? 'Paste a channel link, video link, @handle or channel ID.'
          : 'Use a web address that starts with https://'
      }
    } else if (sourceType === 'reddit') {
      if (trimmedUrl.length < 3) return 'Type a subreddit, like r/python.'
    }
    return null
  }

  // Re-validate on every form change so the Add / Test buttons
  // reflect whether the form is submittable. Errors show under the
  // field (not as a toast): toasts are for confirmations.
  useEffect(() => {
    setNameError(cleanDisplayName(name).length > 120 ? 'Keep it to 120 characters or fewer.' : null)
    const trimmedUrl = normalizeFeedInput(sourceType, url)
    if (!trimmedUrl) {
      setUrlError(null)
    } else if (
      sourceType === 'rss' ||
      sourceType === 'podcast' ||
      sourceType === 'youtube_channel' ||
      sourceType === 'generic_scrape'
    ) {
      try {
        const parsed = new URL(trimmedUrl)
        setUrlError(parsed.protocol === 'http:' || parsed.protocol === 'https:' ? null : 'Use a web address that starts with https://')
      } catch {
        setUrlError(
          sourceType === 'youtube_channel'
            ? 'Paste a channel link, video link, @handle or channel ID.'
            : 'Use a web address that starts with https://',
        )
      }
    } else if (sourceType === 'reddit') {
      setUrlError(trimmedUrl.length < 3 ? 'Type a subreddit, like r/python.' : null)
    } else {
      setUrlError(null)
    }
    // Clear stale test result on any form change so the user
    // doesn't trust the old "Looks good" status after editing.
    setTestResult(null)
  }, [name, url, sourceType])

  // Probe the address with the same plugin Add would use, no DB
  // write. Returns the outcome too, so Add can wait on it.
  const test = async (): Promise<TestOutcome | null> => {
    setUrlTouched(true)
    const err = validateForm()
    if (err) {
      setTestResult({ kind: 'err', message: err })
      return null
    }
    setTesting(true)
    setTestResult(null)
    let outcome: TestOutcome
    try {
      const typedSlug = normalizeName(cleanDisplayName(name))
      const result = await api.testSource({
        name: typedSlug || undefined,
        type: sourceType,
        category,
        url: normalizeFeedInput(sourceType, url),
        ...(takesHeaders && headers ? { custom_headers: headers } : {}),
      })
      const resolvedUrl = result.resolved_url ?? undefined
      if (result.ok) {
        const n = result.item_count ?? 0
        outcome = {
          kind: 'ok',
          message: `Looks good: found ${n} recent item${n === 1 ? '' : 's'}.`,
          itemCount: n,
          sampleTitles: result.sample_titles,
          resolvedUrl,
        }
      } else if (sourceType === 'youtube_channel' && resolvedUrl && YOUTUBE_FEED_DOWN_KINDS.includes(result.error_kind ?? 'unknown')) {
        outcome = {
          kind: 'err',
          message: "Found the channel, but YouTube's feed isn't answering right now. You can add it anyway; Popping will keep checking.",
          resolvedUrl,
        }
      } else {
        outcome = { kind: 'err', message: friendlyTestError(result.error_kind, result.error, sourceLabel), resolvedUrl }
      }
    } catch (e) {
      outcome = { kind: 'err', message: fieldSaveMessage(e, 'url', url, sourceLabel) }
    } finally {
      setTesting(false)
    }
    setTestResult(outcome)
    return outcome
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setUrlTouched(true)
    if (categoryDraft || headersBlocked) return
    const err = validateForm()
    if (err) {
      setTestResult({ kind: 'err', message: err })
      return
    }
    setSubmitting(true)
    let sendUrl = normalized
    try {
      let outcome = testResult
      // The typed name is shown as typed; the feed's internal id is
      // derived from it (or from the address) and never shown.
      const displayName = cleanDisplayName(name)
      const typedSlug = normalizeName(displayName).slice(0, 100)
      let feedName = typedSlug
        ? uniqueSlug(typedSlug, existingNames)
        : suggested ?? suggestName(sourceType, normalized, existingNames, testResult?.resolvedUrl)
      if (!feedName) {
        // A YouTube video link or a non-Latin handle: the name comes
        // from the channel, which Test looks up.
        outcome = await test()
        if (!outcome?.resolvedUrl) return
        feedName = suggestName(sourceType, normalized, existingNames, outcome.resolvedUrl)
        if (!feedName) {
          setTestResult({ kind: 'err', message: 'Type a name for this feed.' })
          return
        }
      }
      sendUrl = outcome?.resolvedUrl ?? normalized
      // Slice 20: only send the discovery hints when type='generic_scrape'
      // (other types ignore them server-side). Empty values are left out
      // so the backend leaves the columns NULL (= default discovery).
      const trimmedSitemapUrl = sitemapUrl.trim()
      const trimmedLinkPattern = linkPattern.trim()
      const row = await api.createSource({
        name: feedName,
        ...(displayName ? { display_name: displayName } : {}),
        type: sourceType,
        category,
        url: sendUrl,
        refresh_interval_seconds: refresh,
        ...(takesHeaders && headers ? { custom_headers: headers } : {}),
        ...(sourceType === 'generic_scrape' && trimmedSitemapUrl
          ? { sitemap_url: trimmedSitemapUrl }
          : {}),
        ...(sourceType === 'generic_scrape' && trimmedLinkPattern
          ? { link_pattern: trimmedLinkPattern }
          : {}),
      })
      onCreated(row, `Added ${displayName || sourceLabel(feedName)} to ${categoryLabel(category)}`)
      setName('')
      setUrl('')
      setUrlTouched(false)
      // Slice 20: also clear the discovery hints so the next Add
      // starts with empty inputs.
      setSitemapUrl('')
      setLinkPattern('')
      setHeaders(null)
      setHeadersOk(true)
      setHeadersOpen(false)
      setHeadersKey((k) => k + 1)
      setTestResult(null)
      await onAdded()
    } catch (e) {
      setTestResult({ kind: 'err', message: fieldSaveMessage(e, 'url', sendUrl, sourceLabel) })
    } finally {
      setSubmitting(false)
    }
  }

  const nameHint =
    nameError ??
    (name.trim()
      ? null
      : suggested
        ? `Leave it blank to call it “${sourceLabel(suggested)}”.`
        : sourceType === 'youtube_channel' && url.trim()
          ? 'Name is chosen after Test.'
          : 'Leave it blank to name the feed after its address.')
  const blocked = !url.trim() || !!(nameError || urlError) || headersBlocked

  return (
    <>
      <AutoFeedBlock sources={sources} onAdded={onAdded} onCreated={onCreated} onPrefill={prefill} />
      <form ref={formRef} onSubmit={submit} noValidate className="space-y-4 text-ios-body scroll-mt-4">
      {/* Type first: it decides what the address field takes. Radios
          rather than a dropdown so the choices are visible at once. */}
      <fieldset>
        <legend className={labelClass}>Type</legend>
        <div className="flex flex-wrap gap-x-4">
          <label className={radioLabelClass}>
            <input
              type="radio"
              name="fm-type"
              value="rss"
              checked={sourceType === 'rss'}
              onChange={() => chooseType('rss')}
              className="accent-accent w-5 h-5"
            />
            RSS / Atom
          </label>
          <label className={radioLabelClass}>
            <input
              type="radio"
              name="fm-type"
              value="reddit"
              checked={sourceType === 'reddit'}
              onChange={() => chooseType('reddit')}
              className="accent-accent w-5 h-5"
            />
            Subreddit
          </label>
          <label className={radioLabelClass}>
            <input
              type="radio"
              name="fm-type"
              value="podcast"
              checked={sourceType === 'podcast'}
              onChange={() => chooseType('podcast')}
              className="accent-accent w-5 h-5"
            />
            Podcast
          </label>
          <label className={radioLabelClass}>
            <input
              type="radio"
              name="fm-type"
              value="youtube_channel"
              checked={sourceType === 'youtube_channel'}
              onChange={() => chooseType('youtube_channel')}
              className="accent-accent w-5 h-5"
            />
            YouTube
          </label>
          <label
            className={radioLabelClass}
            title="For sites without an RSS feed: Popping checks the page for new articles."
          >
            <input
              type="radio"
              name="fm-type"
              value="generic_scrape"
              checked={sourceType === 'generic_scrape'}
              onChange={() => chooseType('generic_scrape')}
              className="accent-accent w-5 h-5"
            />
            Track page (no RSS)
          </label>
        </div>
      </fieldset>
      <div>
        <label className={labelClass} htmlFor="fm-url">
          {sourceType === 'reddit'
            ? 'Subreddit'
            : sourceType === 'podcast'
              ? 'Podcast RSS URL'
              : sourceType === 'youtube_channel'
                ? 'YouTube channel link or channel ID'
                : sourceType === 'generic_scrape'
                  ? 'Page URL (no RSS — will be scraped)'
                  : 'RSS / Atom URL'}
        </label>
        <input
          id="fm-url"
          type={sourceType === 'reddit' ? 'text' : 'url'}
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="next"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onBlur={() => setUrlTouched(true)}
          placeholder={
            sourceType === 'reddit'
              ? 'r/python or https://www.reddit.com/r/python'
              : sourceType === 'podcast'
                ? 'https://example.com/podcast/feed.xml'
                : sourceType === 'youtube_channel'
                  ? '@handle or https://www.youtube.com/@handle'
                  : sourceType === 'generic_scrape'
                    ? 'https://example.com'
                    : 'https://example.com/feed.xml'
          }
          className={fieldClass}
        />
        {sourceType === 'youtube_channel' && (
          <p className="text-ios-caption text-label-secondary mt-1">
            A channel link, video link, @handle or channel ID (starts with UC)
          </p>
        )}
        {urlTouched && urlError && <p className="text-ios-caption text-red-300 mt-1 break-words">{urlError}</p>}
      </div>
      <div>
        <label className={labelClass} htmlFor="fm-name">Name (optional)</label>
        <input
          id="fm-name"
          type="text"
          autoCapitalize="words"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="next"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. CPABC Newsroom"
          className={fieldClass}
        />
        {nameHint && (
          <p className={`text-ios-caption mt-1 break-words ${nameError ? 'text-red-300' : 'text-label-secondary'}`}>
            {nameHint}
          </p>
        )}
      </div>
      {/* Slice 20: discovery hints for ``type='generic_scrape'`` sources.
          Two optional inputs that let the user bypass the homepage-based
          sitemap discovery (sitemapUrl) or fall through to page_links
          (linkPattern) without having to PATCH the row after creating.
          Only shown for type=generic_scrape; other types ignore them
          server-side. */}
      {sourceType === 'generic_scrape' && (
        <div className="space-y-4">
          <div>
            <label className={labelClass} htmlFor="fm-sitemap-url">
              Sitemap address (optional)
            </label>
            <input
              id="fm-sitemap-url"
              type="url"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="next"
              value={sitemapUrl}
              onChange={(e) => setSitemapUrl(e.target.value)}
              placeholder="https://example.com/sitemap-news.xml"
              className={fieldClass}
            />
          </div>
          <div>
            <label className={labelClass} htmlFor="fm-link-pattern">
              Only links starting with (optional)
            </label>
            <input
              id="fm-link-pattern"
              type="text"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="next"
              value={linkPattern}
              onChange={(e) => setLinkPattern(e.target.value)}
              placeholder="/library/"
              className={fieldClass}
            />
          </div>
        </div>
      )}
      <div>
        <label className={labelClass} htmlFor="fm-category">Category</label>
        <CategoryPicker
          inputId="fm-category"
          value={category}
          sources={sources}
          onChange={setCategory}
          defaultInSuggestions
          onDraftChange={setCategoryDraft}
          order={tabOrder}
        />
      </div>
      <div>
        <label className={labelClass} htmlFor="fm-refresh">Check for new items</label>
        <select
          id="fm-refresh"
          value={refresh}
          onChange={(e) => setRefresh(Number(e.target.value))}
          className="select-ios w-full min-h-[44px] rounded-ios bg-bg-elevated border border-hairline pl-3 pr-8 text-ios-body text-label-primary"
        >
          {!REFRESH_PRESETS.some((p) => p.value === refresh) && (
            <option value={refresh}>every {refreshLabel(refresh)}</option>
          )}
          {REFRESH_PRESETS.map((p) => (
            <option key={p.value} value={p.value}>every {p.label}</option>
          ))}
        </select>
      </div>
      {takesHeaders && (
        <div className="space-y-3">
          <button
            type="button"
            aria-expanded={headersOpen}
            aria-controls="fm-headers"
            onClick={() => setHeadersOpen((open) => !open)}
            className="w-full min-h-[44px] flex items-center justify-between gap-2 rounded-ios border border-hairline px-3 py-2 text-left active:opacity-60"
          >
            <span className="min-w-0">
              <span className="block text-ios-body text-label-primary">Advanced</span>
              <span className="block text-ios-caption text-label-secondary">Headers for sites that block readers</span>
            </span>
            <span aria-hidden="true" className="shrink-0 text-label-tertiary">{headersOpen ? '▴' : '▾'}</span>
          </button>
          {/* Kept mounted while closed, so typed rows survive. */}
          <div id="fm-headers" hidden={!headersOpen}>
            <HeadersEditor
              key={headersKey}
              mode="local"
              value={null}
              onCommit={setHeaders}
              onValidChange={setHeadersOk}
              disabled={submitting}
            />
          </div>
          {headersBlocked && !headersOpen && (
            <p className="text-ios-caption text-red-300">Fix the headers under Advanced, or remove them.</p>
          )}
        </div>
      )}
      {/* Test result inline. ``data-test-result`` is the hook the
          flows in scripts/iphone-audit assert on. Add failures land
          here too, so they show inside the sheet. */}
      {testResult && (
        <div
          data-test-result={testResult.kind}
          role="status"
          className={`rounded-ios p-3 text-ios-body break-words ${
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
              Feed address: {testResult.resolvedUrl}
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
      {/* Test (outlined) and Add (filled, the default action). Both
          wait while a request is in flight. */}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void test()}
          disabled={testing || submitting || blocked}
          className="flex-1 min-h-[44px] rounded-ios border border-accent text-accent active:opacity-60 disabled:opacity-40"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
        <button
          type="submit"
          disabled={submitting || testing || blocked || categoryDraft}
          className="flex-1 min-h-[44px] rounded-ios bg-accent active:opacity-80 disabled:opacity-40 text-white"
        >
          {submitting ? 'Adding…' : 'Add feed'}
        </button>
      </div>
      {categoryDraft && (
        <p className="text-ios-caption text-label-secondary">Finish or cancel the new category.</p>
      )}
      </form>
    </>
  )
}
