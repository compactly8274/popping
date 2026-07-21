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
  // in App — search results are cross-column, so there's no single
  // owning column to mark read against. Callers should scope reads
  // under a shared pseudo-column key (App uses ``'Search'``).
  categoriesBySourceId?: Map<number, string>
  // Entries the user has already read anywhere on the dashboard
  // (App's ``globalReadIds``). Drives the dimmed/unread ring.
  readIds?: Set<number>
  onMarkRead?: (entryId: number) => void
  onHide?: (entryId: number) => void
  onHideToggle?: (entryId: number) => void
  hiddenSet?: Set<number>
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
  readIds,
  onMarkRead,
  onHide,
  onHideToggle,
  hiddenSet,
  onStar,
  starredSet,
  onVote,
  votedMap,
  expandedSummaries,
  onToggleSummary,
}: Props) {
  return (
    <section className="flex flex-col h-full overflow-hidden">
      <header className="flex items-center justify-between px-1 pb-2 border-b border-hairline">
        <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
          Search results
        </h2>
        <span className="text-ios-caption text-label-secondary">
          {error
            ? 'failed'
            : searching
              ? 'searching…'
              : `${entries.length} for "${query}"`}
        </span>
      </header>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {error ? (
          <p className="text-ios-body text-red-400 px-1" role="alert">
            search failed — {error}
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
              category={categoriesBySourceId?.get(e.source_id)}
              unread={readIds ? !readIds.has(e.id) : undefined}
              expanded={expandedSummaries?.has(e.id) ?? false}
              onToggleSummary={onToggleSummary ? () => onToggleSummary(e.id) : undefined}
              onMarkRead={onMarkRead ? () => onMarkRead(e.id) : undefined}
              onHide={onHide ? () => onHide(e.id) : undefined}
              onHideToggle={onHideToggle ? () => onHideToggle(e.id) : undefined}
              hidden={hiddenSet?.has(e.id) ?? false}
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