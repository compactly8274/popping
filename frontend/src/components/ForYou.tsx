import type { Entry } from '../api'
import { Card } from './Card'

type Props = {
  entries: Entry[]
  sourcesById: Map<number, string>
}

/**
 * For You — a single horizontal row of the user's top-N entries,
 * pinned above the category grid. On mobile it stacks below the
 * header and scrolls horizontally. When empty (cold start) we show a
 * single muted placeholder rather than collapsing the layout.
 */
export function ForYou({ entries, sourcesById }: Props) {
  return (
    <section className="border-b border-slate-800 bg-slate-900/40">
      <header className="flex items-center justify-between px-4 pt-3 pb-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
          For You
        </h2>
        <span className="text-xs text-slate-500">
          {entries.length > 0 ? `top ${entries.length}` : 'cold start'}
        </span>
      </header>
      <div
        className="flex gap-3 overflow-x-auto px-4 pb-3"
        // Snap-scroll on mobile so swipes feel intentional; desktop
        // just scrolls naturally with the wheel.
        style={{ scrollSnapType: 'x mandatory' }}
      >
        {entries.length === 0 ? (
          <p className="text-sm text-slate-500 italic">
            personal feed is empty — interact with a few cards or check back after the next ingest.
          </p>
        ) : (
          entries.map((e) => (
            <div
              key={e.id}
              className="min-w-[18rem] max-w-[20rem] flex-shrink-0"
              style={{ scrollSnapAlign: 'start' }}
            >
              <Card entry={e} sourceName={sourcesById.get(e.source_id)} />
            </div>
          ))
        )}
      </div>
    </section>
  )
}