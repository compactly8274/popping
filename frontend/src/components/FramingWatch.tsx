// Framing Watch — surfaces same-underlying-story clusters detected by
// the backend's hourly embedding-similarity job (app.framing). Each
// cluster is 2+ outlets covering what the clustering job believes is
// the same story, stacked so their headline framing is directly
// comparable.
//
// Self-contained: fetches its own data on mount (same pattern as
// BriefCard / FeedManager's RecommendedTab) rather than threading
// cluster state through App's already-large state tree. Renders
// nothing when there are no clusters — this is a bonus surface, not
// core navigation, so an empty state doesn't need a message; it just
// doesn't take up space.

import { useEffect, useState } from 'react'
import { api, type FramingCluster } from '../api'
import { SourceIcon } from './SourceIcon'

// Same six-line duplicated formatter as FeedManager.tsx's timeAgo —
// kept local rather than extracted; see that file's comment on why
// (six lines, UI-local, lift to lib/format.ts if a third caller shows up).
function timeAgo(iso: string | null): string {
  if (!iso) return 'unknown'
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// Reuses the score-hot/warm/cold vocabulary already established for
// the composite-score gradient — alarmist reads as "hot", urgent as
// "warm", neutral stays unstyled (no badge color implies no concern).
const TONE_STYLES: Record<string, string> = {
  urgent: 'text-score-warm border-score-warm/40',
  alarmist: 'text-score-hot border-score-hot/40',
}

export function FramingWatch() {
  const [clusters, setClusters] = useState<FramingCluster[]>([])

  useEffect(() => {
    let cancelled = false
    api
      .framingClusters()
      .then((rows) => {
        if (!cancelled) setClusters(rows)
      })
      .catch(() => {
        // Silent — see module comment. A failed fetch just means the
        // section doesn't render this load; the next dashboard
        // refresh tries again.
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (clusters.length === 0) return null

  return (
    <section className="hidden md:block px-4 pt-4 pb-3 border-b border-hairline">
      <header className="flex items-center justify-between mb-2">
        <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
          Framing Watch
        </h2>
        <span className="text-ios-caption text-label-tertiary">
          {clusters.length} {clusters.length === 1 ? 'story' : 'stories'}
        </span>
      </header>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {clusters.map((c) => (
          <div key={c.cluster_id} className="rounded-ios border border-hairline bg-bg-elevated p-3">
            <div className="flex items-center gap-2 mb-2 min-w-0">
              <span className="text-ios-caption text-label-tertiary shrink-0">
                {c.articles.length} framings
              </span>
              {c.wire_source && (
                <span className="shrink-0 inline-flex items-center rounded-full bg-accent-soft px-1.5 text-[10px] uppercase tracking-wide text-accent">
                  {c.wire_source} wire
                </span>
              )}
              <span className="ml-auto shrink-0 text-ios-caption text-label-tertiary">
                {timeAgo(c.first_seen_at)}
              </span>
            </div>
            <ul className="space-y-2">
              {c.articles.map((a) => (
                <li key={a.entry_id}>
                  <a
                    href={a.url}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-start gap-2 group"
                  >
                    <span className="mt-0.5 shrink-0">
                      <SourceIcon src={a.favicon_path} name={a.source_name} size={16} />
                    </span>
                    <span className="min-w-0">
                      <span className="block text-ios-caption text-label-primary group-hover:underline">
                        {a.title}
                      </span>
                      <span className="flex items-center gap-1.5 mt-0.5">
                        <span className="text-[11px] text-label-tertiary truncate">
                          {a.source_name}
                        </span>
                        {a.framing_tone && a.framing_tone !== 'neutral' && (
                          <span
                            className={`shrink-0 text-[9px] uppercase tracking-wide border rounded-full px-1 ${TONE_STYLES[a.framing_tone] ?? ''}`}
                          >
                            {a.framing_tone}
                          </span>
                        )}
                      </span>
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  )
}
