// Search results view. Renders the matching entries as a single
// scrollable list with a "Search results" header. Same shape as a
// Column so the visual rhythm matches the rest of the dashboard.
//
// Deliberately doesn't take search state itself — the parent owns the
// query and the debounced fetch. This component is purely presentational.

import type { Entry } from '../api'
import { Card } from './Card'

type Props = {
  query: string
  entries: Entry[]
  sourcesById: Map<number, string>
  // Network/5xx error from the underlying fetch. Distinct from
  // "no matches" — the panel renders the error in a red block so
  // the user can tell the search itself failed, vs. there simply
  // being no rows for their query.
  error?: string | null
  searching?: boolean
  // Per-card engagement, wired the same way as the "For You" row
  // in App.
  categoriesBySourceId?: Map<number, string>
  // Same shape, favicon path — Card's thumbnail fallback for
  // entries with no photo of their own.
  faviconBySourceId?: Map<number, string | null>
  // A story was opened: App marks it seen, under its category's
  // ``seen:`` key, as from a tab (PR3b). Results are never dimmed: they
  // are what you searched for.
  onOpenEntry?: (entry: Entry) => void
  // Hide (App's ``hideStory``): the card's ⋯ row and a swipe left.
  onHide?: (entryId: number) => void
  onStar?: (entryId: number) => void
  starredSet?: Set<number>
  onVote?: (entryId: number, direction: 'up' | 'down' | null) => void
  votedMap?: Map<number, 'up' | 'down'>
  expandedSummaries?: Set<number>
  onToggleSummary?: (entryId: number) => void
}

export function SearchResults({
  query,
  entries,
  sourcesById,
  error,
  searching,
  categoriesBySourceId,
  faviconBySourceId,
  onOpenEntry,
  onHide,
  onStar,
  starredSet,
  onVote,
  votedMap,
  expandedSummaries,
  onToggleSummary,
}: Props) {
  return (
    <section className="flex flex-col h-full overflow-hidden">
      <header className="flex items-center justify-between gap-2 px-1 pb-2 border-b border-hairline">
        <h2 className="shrink-0 whitespace-nowrap text-ios-caption uppercase tracking-wide text-label-tertiary">
          Search results
        </h2>
        <span className="min-w-0 truncate text-ios-caption text-label-secondary">
          {error
            ? 'failed'
            : searching
              ? 'searching…'
              : `${entries.length} for "${query}"`}
        </span>
      </header>
      <div className="flex-1 overflow-x-clip overflow-y-auto overscroll-contain space-y-2 pr-1">
        {error ? (
          <p className="text-ios-body text-red-400 px-1" role="alert">
            Search failed. {error}
          </p>
        ) : entries.length === 0 ? (
          <p className="text-ios-body text-label-secondary italic px-1">
            no matches — try a different keyword
          </p>
        ) : (
          entries.map((e) => (
            <Card
              key={e.id}
              entry={e}
              sourceName={sourcesById.get(e.source_id)}
              sourceFaviconPath={faviconBySourceId?.get(e.source_id)}
              category={categoriesBySourceId?.get(e.source_id)}
              expanded={expandedSummaries?.has(e.id) ?? false}
              onToggleSummary={onToggleSummary ? () => onToggleSummary(e.id) : undefined}
              onActivate={onOpenEntry ? () => onOpenEntry(e) : undefined}
              onHide={onHide ? () => onHide(e.id) : undefined}
              onStar={onStar ? () => onStar(e.id) : undefined}
              starred={starredSet?.has(e.id) ?? false}
              onVote={onVote ? (dir) => onVote(e.id, dir) : undefined}
              vote={votedMap?.get(e.id) ?? null}
            />
          ))
        )}
      </div>
    </section>
  )
}