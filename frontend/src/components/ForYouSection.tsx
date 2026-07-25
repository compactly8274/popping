/**
 * ForYouSection — the desktop "For You" card grid (extracted from
 * App.tsx).
 *
 * The reason this exists as a separate component: App.tsx is one
 * large function with ~30 useState hooks. Every state change
 * re-evaluates the whole tree's render path. ``setHealth`` fires
 * every 30s, ``setTimeTick`` every 30s, the toast library mutates
 * its own state on every interaction — none of these affect the
 * For You data, but they all trigger a re-render of the For You
 * card grid. For a heavy user with 25+ For You cards that's a
 * noticeable ~30% of the dashboard's render cost paid on every
 * poller tick.
 *
 * Wrapping the For You section in this memoized component means
 * App-level state changes that don't affect the For You data
 * (health poller, time tick, search query, hidden/starred edits
 * in OTHER columns) no longer re-render this section. The For You
 * section only re-renders when its props change.
 */
import { memo } from 'react'
import { Card } from './Card'

export interface ForYouEntry {
  id: number
  source_id: number
  // Anything else the Card needs; we don't type-check it here
  // because the full Entry type is large and the contract is "we
  // pass whatever Entry the API returns". Keeping it loose avoids
  // a long index signature in the test.
  [k: string]: unknown
}

export interface ForYouHandlers {
  markEntryRead: (colName: string, entryId: number) => void
  unmarkEntryRead: (colName: string, entryId: number) => void
  hideEntry: (entryId: number) => void
  restoreHiddenEntry: (entryId: number) => void
  toggleHideEntry: (colName: string, entryId: number) => void
  toggleStarEntry: (entryId: number) => void
  setEntryVote: (entryId: number, dir: 'up' | 'down' | null) => void
  toggleSummary: (entryId: number) => void
  // Toast wrappers. App owns the toast library; the section
  // calls into the wrapper for the per-card Undo actions.
  toastEntryMarked: (colName: string, entryId: number) => void
  toastEntryHidden: (entryId: number) => void
  toastEntryStarred: (entryId: number, wasStarred: boolean) => void
}

export interface ForYouSectionProps {
  visibleForYou: ForYouEntry[]
  sourcesById: Map<number, string>
  categoriesBySourceId: Map<number, string | null>
  faviconBySourceId: Map<number, string | null>
  globalReadIds: Set<number>
  hiddenSet: Set<number>
  starredSet: Set<number>
  votedMap: Map<number, 'up' | 'down'>
  expandedSummaries: Set<number>
  handlers: ForYouHandlers
}

const COLUMN_NAME = 'For You'

function ForYouSectionInner(props: ForYouSectionProps) {
  const {
    visibleForYou,
    sourcesById,
    categoriesBySourceId,
    faviconBySourceId,
    globalReadIds,
    hiddenSet,
    starredSet,
    votedMap,
    expandedSummaries,
    handlers,
  } = props

  // Per-card handlers. Wrap each one in a callback that fixes
  // the entry id, so the column-level event wiring doesn't have
  // to repeat the per-entry dispatch logic.
  //
  // We can't useCallback here because the per-entry id is the
  // variable. The closure over ``handlers`` is fine because
  // ``handlers`` is built with useMemo upstream — its identity
  // is stable, so the inline functions are also stable.
  const onMarkRead = (entryId: number) => {
    handlers.markEntryRead(COLUMN_NAME, entryId)
    handlers.toastEntryMarked(COLUMN_NAME, entryId)
  }
  const onHide = (entryId: number) => {
    handlers.hideEntry(entryId)
    handlers.toastEntryHidden(entryId)
  }
  const onHideToggle = (entryId: number) => {
    handlers.toggleHideEntry(COLUMN_NAME, entryId)
  }
  const onStar = (entryId: number) => {
    const wasStarred = starredSet.has(entryId)
    handlers.toggleStarEntry(entryId)
    handlers.toastEntryStarred(entryId, wasStarred)
  }
  const onUnstar = (entryId: number) => {
    handlers.toggleStarEntry(entryId)
  }

  return (
    <section className="hidden md:block px-4 pt-4 pb-3 border-b border-hairline">
      <header className="flex items-center justify-between mb-2">
        <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary">
          For You
        </h2>
        <span className="text-ios-caption text-label-tertiary">
          {visibleForYou.length} {visibleForYou.length === 1 ? 'entry' : 'entries'}
        </span>
      </header>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {visibleForYou.map((e) => {
          // Per-card engagement props. The For You row was
          // previously read-only — no mark-read, no hide, no
          // star. The user could see the personal feed but
          // couldn't engage with it. Now every card has the
          // full set of per-card actions wired to App-level
          // callbacks so the user can mark-read, hide, or star
          // a For You card the same way they would in a
          // category column.
          //
          // ``unread`` reflects ``globalReadIds``, the union
          // of every column's manual read-set — so marking
          // this card read here also dims it in its category
          // column, and marking it read there dims it here too.
          // The per-column lastViewed isn't used here (the For
          // You row has no "mark all read" button).
          //
          // ``selected`` / ``onActivate`` / ``cardRef`` are
          // not passed because keyboard nav is per-column,
          // not per-row, and the For You row is not a
          // "column" the user can scroll into.
          const isRead = globalReadIds.has(e.id)
          return (
            <Card
              key={e.id}
              entry={e as any}
              sourceName={sourcesById.get(e.source_id)}
              sourceFaviconPath={faviconBySourceId.get(e.source_id)}
              category={categoriesBySourceId.get(e.source_id) ?? undefined}
              unread={!isRead}
              expanded={expandedSummaries.has(e.id)}
              onToggleSummary={() => handlers.toggleSummary(e.id)}
              onMarkRead={() => onMarkRead(e.id)}
              onHide={() => onHide(e.id)}
              onHideToggle={() => onHideToggle(e.id)}
              hidden={hiddenSet.has(e.id)}
              onStar={() => onStar(e.id)}
              onUnstar={() => onUnstar(e.id)}
              starred={starredSet.has(e.id)}
              onVote={(dir) => handlers.setEntryVote(e.id, dir)}
              vote={votedMap.get(e.id) ?? null}
            />
          )
        })}
      </div>
    </section>
  )
}

// React.memo with a custom comparator. Re-render only when the
// data this section actually consumes changes. App's other
// state changes (health poller, time tick, search query, edits
// in OTHER columns) no longer cascade into a 25-card re-render.
const ForYouSection = memo(
  ForYouSectionInner,
  (prev, next) => {
    return (
      prev.visibleForYou === next.visibleForYou &&
        prev.sourcesById === next.sourcesById &&
        prev.categoriesBySourceId === next.categoriesBySourceId &&
        prev.faviconBySourceId === next.faviconBySourceId &&
        prev.globalReadIds === next.globalReadIds &&
        prev.hiddenSet === next.hiddenSet &&
        prev.starredSet === next.starredSet &&
        prev.votedMap === next.votedMap &&
        prev.expandedSummaries === next.expandedSummaries &&
        prev.handlers === next.handlers
    )
  },
)

export { ForYouSection }
export default ForYouSection
