/**
 * ColumnGrid — the desktop (md+) column layout extracted from App.tsx.
 *
 * Why this exists: App.tsx is one 2905-line function with ~30
 * useState hooks. Every state change re-evaluates the whole tree.
 * The column grid is the heaviest single surface (8+ Column
 * components, each with their own cards, sections, and per-card
 * mark-read/star/hide state). Health poller, time tick, and
 * search-query changes all triggered a full re-render of every
 * Column before this refactor.
 *
 * Wrapping the grid in a React.memo'd component means App-level
 * state changes that don't affect the column data no longer
 * cascade into 8+ Column re-renders. The grid only re-renders
 * when its props (the ``columns`` array, the maps, or the
 * ``handlers`` bundle) change. The mobile version (md:hidden)
 * stays inline in App.tsx because it's a different layout with
 * tabs — splitting it would duplicate the Column component's
 * already-substantial prop surface.
 */
import { memo, useMemo } from 'react'
import type { Entry } from '../api'
import { Column } from './Column'

/**
 * Per-column shape. ``entries`` is the full Entry[] — the
 * Column component (and the Card component it forwards to)
 * already know how to render any field on the row.
 */
export interface ColumnGridColumn {
  name: string
  totalCount?: number
  entries: Entry[]
}

/**
 * Stable callback bundle. App constructs this with useMemo so
 * its identity is stable across renders. Same pattern as
 * ForYouSection. The two share enough that they could one day
 * be unified, but keeping them separate is clearer at the call
 * site (the column grid's handlers carry a column name; the
 * For You section's don't).
 */
export interface ColumnGridHandlers {
  refresh: () => void
  markColumnRead: (colName: string) => void
  markEntryRead: (colName: string, entryId: number) => void
  toggleHideEntry: (colName: string, entryId: number) => void
  hideEntry: (entryId: number) => void
  restoreHiddenEntry: (entryId: number) => void
  toggleStarEntry: (entryId: number) => void
  setEntryVote: (entryId: number, dir: 'up' | 'down' | null) => void
  toggleSummary: (entryId: number) => void
  setColumnSection: (colName: string, key: 'new' | 'history') => void
  setPrefsFor: (colName: string, prefs: any) => void
  unmarkEntryRead: (colName: string, entryId: number) => void
  // Toast wrappers. The column forwards per-card events to these
  // so the "Marked as read" / "Entry hidden" / "Saved for later"
  // toasts fire from one place. App owns the toast library.
  toastEntryMarked: (colName: string, entryId: number) => void
  toastEntryHidden: (entryId: number) => void
  toastEntryStarred: (entryId: number, wasStarred: boolean) => void
}

export interface ColumnGridProps {
  columns: ColumnGridColumn[]
  viewKind: string
  // Sources / prefs maps — built with useMemo upstream so the
  // reference is stable.
  sourcesById: Map<number, string>
  categoriesBySourceId: Map<number, string | null>
  faviconBySourceId: Map<number, string | null>
  // Per-column state.
  sectionsByColumn: Map<string, { new: Entry[]; history: Entry[] }>
  newCountByColumn: Map<string, number>
  columnPrefs: Record<string, any>
  defaultPrefs: any
  // Per-row state used by every column.
  hiddenSet: Set<number>
  starredSet: Set<number>
  votedMap: Map<number, 'up' | 'down'>
  expandedSummaries: Set<number>
  // Keyboard nav. selectedColumnIndex === -1 means no focus.
  selectedColumnIndex: number
  selectedCardId: number | undefined
  // Refs forwarded to each column for scroll-into-view.
  cardRefs: React.MutableRefObject<Map<number, HTMLElement | null>>
  setColumnRef: (colName: string) => (el: HTMLElement | null) => void
  sectionsCollapsedFor: (colName: string) => { new: boolean; history: boolean }
  handlers: ColumnGridHandlers
}

function ColumnGridInner(props: ColumnGridProps) {
  const {
    columns,
    viewKind,
    sourcesById,
    categoriesBySourceId,
    faviconBySourceId,
    sectionsByColumn,
    newCountByColumn,
    columnPrefs,
    defaultPrefs,
    hiddenSet,
    starredSet,
    votedMap,
    expandedSummaries,
    selectedColumnIndex,
    selectedCardId,
    cardRefs,
    setColumnRef,
    sectionsCollapsedFor,
    handlers,
  } = props

  // Filter the columns array based on viewKind. Memoized so a
  // re-render with the same columns + viewKind doesn't
  // re-allocate. The actual <Column /> elements are stable
  // because their props come from this single useMemo.
  const visibleColumns = useMemo(
    () => columns.filter((col) => viewKind === 'multisub' || col.name !== 'For You'),
    [columns, viewKind],
  )

  return (
    <main className="hidden md:grid md:grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 p-4 flex-1 overflow-y-auto">
      {visibleColumns.map((col, ci) => (
        <div key={col.name} ref={setColumnRef(col.name)} className="contents">
          <Column
            name={col.name}
            sections={sectionsByColumn.get(col.name) ?? { new: [], history: [] }}
            sourcesById={sourcesById}
            newCount={newCountByColumn.get(col.name)}
            onRefresh={handlers.refresh}
            selectedId={ci === selectedColumnIndex ? selectedCardId ?? undefined : undefined}
            cardRefs={cardRefs}
            onMarkRead={() => handlers.markColumnRead(col.name)}
            onMarkEntryRead={(entryId) => {
              handlers.markEntryRead(col.name, entryId)
              handlers.toastEntryMarked(col.name, entryId)
            }}
            onHideEntry={(entryId) => {
              handlers.hideEntry(entryId)
              handlers.toastEntryHidden(entryId)
            }}
            onHideToggle={(entryId) => handlers.toggleHideEntry(col.name, entryId)}
            hiddenSet={hiddenSet}
            onStarEntry={(entryId) => {
              const wasStarred = starredSet.has(entryId)
              handlers.toggleStarEntry(entryId)
              handlers.toastEntryStarred(entryId, wasStarred)
            }}
            starredSet={starredSet}
            onVoteEntry={handlers.setEntryVote}
            votedMap={votedMap}
            prefs={columnPrefs[col.name] ?? defaultPrefs}
            onPrefsChange={(next) => handlers.setPrefsFor(col.name, next)}
            totalCount={col.totalCount}
            categoriesBySourceId={categoriesBySourceId}
            faviconBySourceId={faviconBySourceId}
            expandedSummaries={expandedSummaries}
            onToggleSummary={handlers.toggleSummary}
            sectionsCollapsed={sectionsCollapsedFor(col.name)}
            onToggleSection={(key) => handlers.setColumnSection(col.name, key)}
          />
        </div>
      ))}
    </main>
  )
}

// React.memo with a custom comparator. Default shallow-compare
// would re-render on every prop reference change; the
// comparator below only re-renders when the data the grid
// actually consumes changes. All maps/sets/records are
// expected to be built with useMemo upstream — the per-render
// identity of a Map created inline would defeat this.
const ColumnGrid = memo(
  ColumnGridInner,
  (prev, next) => {
    return (
      prev.columns === next.columns &&
      prev.viewKind === next.viewKind &&
      prev.sourcesById === next.sourcesById &&
      prev.categoriesBySourceId === next.categoriesBySourceId &&
      prev.faviconBySourceId === next.faviconBySourceId &&
      prev.sectionsByColumn === next.sectionsByColumn &&
      prev.newCountByColumn === next.newCountByColumn &&
      prev.columnPrefs === next.columnPrefs &&
      prev.defaultPrefs === next.defaultPrefs &&
      prev.hiddenSet === next.hiddenSet &&
      prev.starredSet === next.starredSet &&
      prev.votedMap === next.votedMap &&
      prev.expandedSummaries === next.expandedSummaries &&
      prev.selectedColumnIndex === next.selectedColumnIndex &&
      prev.selectedCardId === next.selectedCardId &&
      prev.cardRefs === next.cardRefs &&
      prev.setColumnRef === next.setColumnRef &&
      prev.sectionsCollapsedFor === next.sectionsCollapsedFor &&
      prev.handlers === next.handlers
    )
  },
)

export { ColumnGrid }
export default ColumnGrid
