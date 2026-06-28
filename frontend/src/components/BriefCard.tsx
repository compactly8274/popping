// The Brief card. Renders the most recent terse brief (or any tone
// the dashboard asks for) above For You. Falls back to a "generate
// today's brief" CTA when no brief exists yet.
//
// The brief content is plain text with a fixed shape:
//   TODAY IN ONE SENTENCE
//   <line>
//   HIGHLIGHTS
//   - ...
//   WATCH
//   - ...
// We parse those section headers out and render them as semantic HTML.
// Anything else goes in a small pre block.

import { useEffect, useId, useState } from 'react'
import { api, type Brief } from '../api'

type Props = {
  brief: Brief | null
  onBriefChange: (brief: Brief | null) => void
}

// localStorage key for the collapse preference. Lives alongside the
// other UI keys; no schema version because it's a bare boolean.
const COLLAPSED_KEY = 'brief.collapsed'

type Parsed = {
  oneSentence: string
  highlights: string[]
  watch: string[]
  remainder: string
}

function parse(content: string): Parsed {
  const out: Parsed = {
    oneSentence: '',
    highlights: [],
    watch: [],
    remainder: '',
  }
  let bucket: 'none' | 'one' | 'high' | 'watch' | 'rest' = 'none'
  for (const raw of content.split(/\r?\n/)) {
    const line = raw.trim()
    if (/^TODAY IN ONE SENTENCE/i.test(line)) {
      bucket = 'one'
      continue
    }
    if (/^HIGHLIGHTS/i.test(line)) {
      bucket = 'high'
      continue
    }
    if (/^WATCH/i.test(line)) {
      bucket = 'watch'
      continue
    }
    if (!line) {
      // Blank line — keep bucket, skip.
      continue
    }
    if (bucket === 'one') {
      out.oneSentence = (out.oneSentence ? out.oneSentence + ' ' : '') + line
      bucket = 'rest'
    } else if (bucket === 'high' && line.startsWith('-')) {
      out.highlights.push(line.slice(1).trim())
    } else if (bucket === 'watch' && line.startsWith('-')) {
      out.watch.push(line.slice(1).trim())
    } else if (bucket === 'high' || bucket === 'watch') {
      // Section header detected but no dash — keep accumulating into the
      // first matching list.
      if (bucket === 'high') out.highlights.push(line)
      else out.watch.push(line)
    } else {
      out.remainder = out.remainder ? out.remainder + '\n' + line : line
    }
  }
  return out
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export function BriefCard({ brief, onBriefChange }: Props) {
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Collapse state. Default open; persists per-browser via localStorage
  // so the user doesn't have to re-collapse on every refresh. SSR-safe
  // — the fallback covers the case where window/localStorage isn't
  // available (e.g. an early mount in a future SSR build).
  const [collapsed, setCollapsed] = useState<boolean>(
    () =>
      typeof window !== 'undefined' &&
      window.localStorage?.getItem(COLLAPSED_KEY) === '1',
  )
  const bodyId = useId()

  // Toggle the collapse. Functional setState avoids a stale-closure
  // hazard when two clicks land in the same React batch: each
  // invocation reads the latest state via the updater's argument,
  // not the value captured when this callback was last rendered.
  //
  // The localStorage write lives inside the updater. StrictMode
  // invokes updaters twice in dev; that's harmless here because
  // the writes are idempotent (``'1'`` twice == ``'1'`` once).
  const toggleCollapsed = () => {
    setCollapsed((prev) => {
      const next = !prev
      try {
        window.localStorage?.setItem(COLLAPSED_KEY, next ? '1' : '0')
      } catch {
        // Quota / private-mode failures don't matter — the in-memory
        // state is the source of truth for the current session.
      }
      return next
    })
  }

  // Header-level click handler. Used instead of an absolutely-
  // positioned overlay button so the toggle target is the natural
  // header element, not a duplicate focusable sibling. Clicks that
  // originated from a child interactive element (Regenerate /
  // Generate / the chevron button) carry a sentinel marker; the
  // marker is what lets us avoid the regenerate-then-collapse
  // double-action that ``stopPropagation`` would cause across
  // delegated vs captured handlers.
  const onHeaderClick = (e: React.MouseEvent<HTMLElement>) => {
    const target = e.target as HTMLElement
    // Anything tagged ``data-brief-action`` is an independent
    // control inside the header. Let it act on its own.
    if (target.closest('[data-brief-action]')) return
    toggleCollapsed()
  }

  // Pull latest brief on mount. The parent passes the current value in
  // as a prop so other UI surfaces can refresh it; we only fetch when
  // the prop is null.
  useEffect(() => {
    if (brief) return
    let cancelled = false
    api
      .briefLatest({ tone: 'terse' })
      .then((rows) => {
        if (cancelled) return
        if (rows.length > 0) onBriefChange(rows[0])
      })
      .catch(() => {
        // Tolerate 503 etc. — the card just stays in the "no brief" state.
      })
    return () => {
      cancelled = true
    }
  }, [brief, onBriefChange])

  const onGenerate = async () => {
    setError(null)
    setGenerating(true)
    try {
      const next = await api.briefGenerate('terse')
      onBriefChange(next)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  if (!brief) {
    return (
      <section className="border-b border-slate-800 bg-gradient-to-r from-slate-900/60 to-slate-900/30">
        <div
          role="button"
          tabIndex={0}
          aria-expanded={!collapsed}
          aria-controls={bodyId}
          onClick={onHeaderClick}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              toggleCollapsed()
            }
          }}
          className="px-4 py-3 flex items-center gap-3 cursor-pointer select-none"
        >
          {/* Chevron — finger-sized. Hover/active only on devices that
              actually have a hover capability; touch devices that
              leave :hover stuck on after a tap won't get a lingering
              background. */}
          <span
            aria-hidden="true"
            className="shrink-0 -ml-2 flex items-center justify-center w-11 h-11 rounded text-slate-400 active:bg-slate-800/60 [@media(hover:hover)]:hover:bg-slate-800/60"
          >
            <span className="text-base leading-none">
              {collapsed ? '▸' : '▾'}
            </span>
          </span>
          <div className="flex-1 min-w-0">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
              The Brief
            </h2>
            {!collapsed && (
              <p className="text-xs text-slate-500 mt-0.5">
                No brief generated yet for today.
              </p>
            )}
          </div>
          {/* Generate button — independent action, marked so the
              header click handler ignores taps on it. min-h-[44px]
              keeps the touch target large enough for a thumb. */}
          <button
            data-brief-action="generate"
            onClick={onGenerate}
            disabled={generating}
            className="shrink-0 min-h-[44px] rounded px-3 py-1.5 text-sm bg-blue-700 active:bg-blue-800 disabled:opacity-50 text-white [@media(hover:hover)]:hover:bg-blue-600"
          >
            {generating ? 'Generating…' : "Generate today's brief"}
          </button>
        </div>
        {!collapsed && error && (
          <p className="px-4 pb-3 text-xs text-red-300">{error}</p>
        )}
      </section>
    )
  }

  const parsed = parse(brief.content)

  return (
    <section className="border-b border-slate-800 bg-gradient-to-r from-slate-900/60 to-slate-900/30">
      {/* Header row. The whole element is the toggle target — chevron,
          title, tone badge, timestamp — but Regenerate is a child
          marked ``data-brief-action`` so it bypasses the toggle. */}
      <header
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        aria-controls={bodyId}
        onClick={onHeaderClick}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggleCollapsed()
          }
        }}
        className="flex items-center justify-between px-4 pt-3 pb-2 cursor-pointer select-none min-h-[44px]"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            aria-hidden="true"
            className="shrink-0 -ml-2 flex items-center justify-center w-11 h-11 rounded text-slate-400 active:bg-slate-800/60 [@media(hover:hover)]:hover:bg-slate-800/60"
          >
            <span className="text-base leading-none">
              {collapsed ? '▸' : '▾'}
            </span>
          </span>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
            The Brief
          </h2>
          <span className="text-[10px] uppercase tracking-wide text-slate-500 bg-slate-800 rounded px-1.5 py-0.5">
            {brief.tone}
          </span>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500 shrink-0">
          <span title={brief.generated_at}>
            {/* "generated " prefix only on sm+; saves ~70px on mobile
                so the Regenerate button isn't pushed off-screen. */}
            <span className="hidden sm:inline">generated </span>
            {timeAgo(brief.generated_at)}
          </span>
          <button
            data-brief-action="regenerate"
            onClick={onGenerate}
            disabled={generating}
            className="min-h-[44px] rounded px-3 py-1.5 text-xs bg-slate-800 active:bg-slate-700 disabled:opacity-50 text-slate-200 [@media(hover:hover)]:hover:bg-slate-700"
          >
            {generating ? '…' : 'Regenerate'}
          </button>
        </div>
      </header>
      {!collapsed && (
        <div id={bodyId} className="px-4 pb-3 space-y-2">
          {parsed.oneSentence && (
            <p className="text-base font-medium text-slate-100 leading-snug">
              {parsed.oneSentence}
            </p>
          )}
          {parsed.highlights.length > 0 && (
            <ul className="space-y-1 text-sm text-slate-200 list-none">
              {parsed.highlights.map((h, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-slate-500 select-none">•</span>
                  <span>{h}</span>
                </li>
              ))}
            </ul>
          )}
          {parsed.watch.length > 0 && (
            <div className="pt-1">
              <h3 className="text-[10px] font-semibold uppercase tracking-wide text-slate-500 mb-1">
                Watch
              </h3>
              <ul className="space-y-0.5 text-xs text-slate-400 list-none">
                {parsed.watch.map((w, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-slate-600 select-none">›</span>
                    <span>{w}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {parsed.remainder && (
            <pre className="text-xs text-slate-500 whitespace-pre-wrap font-sans">
              {parsed.remainder}
            </pre>
          )}
          {error && <p className="text-xs text-red-300">{error}</p>}
        </div>
      )}
    </section>
  )
}