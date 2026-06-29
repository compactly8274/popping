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
}

export function SearchResults({ query, entries, sourcesById }: Props) {
  return (
    <section className="flex flex-col h-full overflow-hidden">
      <header className="flex items-center justify-between px-1 pb-2 border-b border-hairline">
        <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
          Search results
        </h2>
        <span className="text-ios-caption text-label-secondary">
          {entries.length} for "{query}"
        </span>
      </header>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {entries.length === 0 ? (
          <p className="text-ios-body text-label-secondary italic px-1">
            no matches — try a different keyword
          </p>
        ) : (
          entries.map((e) => (
            <Card key={e.id} entry={e} sourceName={sourcesById.get(e.source_id)} />
          ))
        )}
      </div>
    </section>
  )
}