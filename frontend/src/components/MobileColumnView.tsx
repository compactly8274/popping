/**
 * MobileColumnView — the mobile (md:hidden) tabbed column layout
 * extracted from App.tsx.
 *
 * Why this exists: App.tsx is the same function as the desktop
 * render. Every state change re-evaluates both render paths.
 * The mobile path is ~150 lines of inline JSX (tab bar +
 * conditional Brief / Column render) that fires the same
 * handlers as the desktop. With the ForYouSection and
 * ColumnGrid refactors in the previous passes, the desktop
 * surfaces are now memoized — but mobile was still inline, so
 * a health-poller or time-tick re-render rebuilt the entire
 * tab bar + its child Column.
 *
 * Wrapping the mobile path in this memoized component means
 * App-level state changes that don't affect mobile
 * (the desktop column-grid, the For You row) no longer rebuild
 * the tab bar or its child Column. The mobile view only
 * re-renders when its props change (the columns array, the
 * maps, the mobileCol state, or the handlers bundle).
 */
import { memo, useMemo } from 'react'
import type { Entry } from '../api'
import { BriefCard } from './BriefCard'
import { Column } from './Column'
import { DEFAULT_PREFS } from './Column'

/**
 * -1 means "show the Brief tab" rather than a column. This
 * is the value the parent's useState starts at (and the
 * default after the user taps the Brief tab).
 */
export const BRIEF_TAB_INDEX = -1

export interface MobileColumnEntry {
  id: number
  source_id: number
  [k: string]: unknown
}

export interface MobileColumnColumn {
  name: string
  totalCount?: number
  entries: MobileColumnEntry[]
}

export interface MobileColumnHandlers {
  // Tab state
  mobileCol: number
  setMobileCol: (i: number) => void
  // Column data
  columns: MobileColumnColumn[]
  newCountByColumn: Map<string, number>
  sectionsByColumn: Map<string, { new: MobileColumnEntry[]; history: MobileColumnEntry[] }>
  columnPrefs: Record<string, any>
  // Per-row state
  sourcesById: Map<number, string>
  categoriesBySourceId: Map<number, string | null>
  faviconBySourceId: Map<number, string | null>
  hiddenSet: Set<number>
  starredSet: Set<number>
  votedMap: Map<number, 'up' | 'down'>
  expandedSummaries: Set<number>
  // Per-section collapse
  sectionsCollapsedFor: (colName: string) => { new: boolean; history: boolean }
  // Selection
  selectedColumnIndex: number
  selectedCardId: number | undefined
  cardRefs: React.MutableRefObject<Map<number, HTMLElement | null>>
  // Brief card
  brief: any
  onBriefChange: (b: any) => void
  briefTone: 'terse' | 'narrative' | 'alert'
  onBriefToneChange: (t: 'terse' | 'narrative' | 'alert') => void
  triggerBriefGenerate: (tone: 'terse' | 'narrative' | 'alert', onError: (e: string) => void) => Promise<void>
  // Per-column actions
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
  // Toast wrappers — same as desktop
  toastEntryMarked: (colName: string, entryId: number) => void
  toastEntryHidden: (entryId: number) => void
  toastEntryStarred: (entryId: number, wasStarred: boolean) => void
}

function MobileColumnViewInner(props: MobileColumnHandlers) {
  const {
    mobileCol,
    setMobileCol,
    columns,
    newCountByColumn,
    sectionsByColumn,
    columnPrefs,
    sourcesById,
    categoriesBySourceId,
    faviconBySourceId,
    hiddenSet,
    starredSet,
    votedMap,
    expandedSummaries,
    sectionsCollapsedFor,
    selectedColumnIndex,
    selectedCardId,
    cardRefs,
    brief,
    onBriefChange,
    briefTone,
    onBriefToneChange,
    triggerBriefGenerate,
    refresh,
    markColumnRead,
    markEntryRead,
    toggleHideEntry,
    hideEntry,
    restoreHiddenEntry,
    toggleStarEntry,
    setEntryVote,
    toggleSummary,
    setColumnSection,
    setPrefsFor,
    unmarkEntryRead,
    toastEntryMarked,
    toastEntryHidden,
    toastEntryStarred,
  } = props

  const isBriefTab = mobileCol === BRIEF_TAB_INDEX
  const activeColumn = !isBriefTab ? columns[mobileCol] : undefined
  const activeColumnName = activeColumn?.name ?? ''

  return (
    <main className="md:hidden flex-1 min-h-0 flex flex-col p-3">
      {/* Tab bar. Replaces the old swipe-to-change-column
          gesture — that gesture had no direction lock (any
          60px-plus horizontal touch delta fired it, scroll
          wobble included) and it collided with the new
          per-card swipe actions in Card.tsx, which now own
          the horizontal-drag gesture on mobile. Tabs are the
          explicit, discoverable replacement; "Brief" is a tab
          here (not inline above the column, like on desktop)
          so it doesn't push the column below the fold on a
          small screen. Horizontally scrollable — a source
          list with 6+ categories won't fit every tab label on
          a phone-width screen. */}
      <div
        role="tablist"
        aria-label="dashboard sections"
        className="shrink-0 flex gap-1.5 overflow-x-auto pb-2 -mx-1 px-1"
      >
        <button
          type="button"
          role="tab"
          aria-selected={isBriefTab}
          onClick={() => setMobileCol(BRIEF_TAB_INDEX)}
          className={`shrink-0 rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
            isBriefTab
              ? 'bg-accent text-white'
              : 'bg-bg-surface text-label-secondary active:bg-bg-elevated'
          }`}
        >
          Brief
        </button>
        {columns.map((c, i) => {
          const newCount = newCountByColumn.get(c.name)
          return (
            // Navigation, not mark-read. Merely peeking at a
            // column shouldn't drop its "+N new" chip — that
            // violates the universal-inbox rule. The column
            // header (desktop) and the per-card ✓ button are
            // the explicit mark-read affordances.
            <button
              type="button"
              key={c.name}
              role="tab"
              aria-selected={i === mobileCol}
              onClick={() => setMobileCol(i)}
              className={`shrink-0 flex items-center gap-1 rounded-full px-3 py-1.5 text-ios-caption font-medium whitespace-nowrap transition ${
                i === mobileCol
                  ? 'bg-accent text-white'
                  : 'bg-bg-surface text-label-secondary active:bg-bg-elevated'
              }`}
            >
              {c.name}
              {!!newCount && (
                <span
                  className={`rounded-full px-1.5 text-[10px] leading-4 font-semibold ${
                    i === mobileCol ? 'bg-white/25 text-white' : 'bg-accent-soft text-accent'
                  }`}
                >
                  {newCount}
                </span>
              )}
            </button>
          )
        })}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {isBriefTab ? (
          <BriefCard
            brief={brief}
            onBriefChange={onBriefChange}
            tone={briefTone}
            onToneChange={onBriefToneChange}
            triggerGenerate={triggerBriefGenerate}
          />
        ) : (
          activeColumn && (
            <Column
              name={activeColumn.name}
              sections={sectionsByColumn.get(activeColumn.name) ?? { new: [], history: [] }}
              sourcesById={sourcesById}
              newCount={newCountByColumn.get(activeColumn.name)}
              onRefresh={refresh}
              selectedId={
                mobileCol === selectedColumnIndex ? selectedCardId ?? undefined : undefined
              }
              cardRefs={cardRefs}
              onMarkRead={() => markColumnRead(activeColumn.name)}
              onMarkEntryRead={(entryId) => {
                markEntryRead(activeColumn.name, entryId)
                toastEntryMarked(activeColumn.name, entryId)
              }}
              onHideEntry={(entryId) => {
                hideEntry(entryId)
                toastEntryHidden(entryId)
              }}
              onHideToggle={(entryId) => toggleHideEntry(activeColumn.name, entryId)}
              hiddenSet={hiddenSet}
              onStarEntry={(entryId) => {
                const wasStarred = starredSet.has(entryId)
                toggleStarEntry(entryId)
                toastEntryStarred(entryId, wasStarred)
              }}
              starredSet={starredSet}
              onVoteEntry={setEntryVote}
              votedMap={votedMap}
              prefs={columnPrefs[activeColumn.name] ?? DEFAULT_PREFS}
              onPrefsChange={(next) => setPrefsFor(activeColumn.name, next)}
              totalCount={activeColumn.totalCount}
              categoriesBySourceId={categoriesBySourceId}
              faviconBySourceId={faviconBySourceId}
              expandedSummaries={expandedSummaries}
              onToggleSummary={toggleSummary}
              sectionsCollapsed={sectionsCollapsedFor(activeColumn.name)}
              onToggleSection={(key) => setColumnSection(activeColumn.name, key)}
            />
          )
        )}
      </div>
    </main>
  )
}

// React.memo with a custom comparator. The mobile view only
// re-renders when the data it actually consumes changes
// (mobileCol + the column data + the handlers bundle). A
// desktop-side state update that doesn't affect mobile (e.g.
// the ColumnGrid re-rendering, the For You row updating)
// no longer cascades into the mobile tab bar.
const MobileColumnView = memo(
  MobileColumnViewInner,
  (prev, next) => {
    return (
      prev.mobileCol === next.mobileCol &&
      prev.setMobileCol === next.setMobileCol &&
      prev.columns === next.columns &&
      prev.newCountByColumn === next.newCountByColumn &&
      prev.sectionsByColumn === next.sectionsByColumn &&
      prev.columnPrefs === next.columnPrefs &&
      prev.sourcesById === next.sourcesById &&
      prev.categoriesBySourceId === next.categoriesBySourceId &&
      prev.faviconBySourceId === next.faviconBySourceId &&
      prev.hiddenSet === next.hiddenSet &&
      prev.starredSet === next.starredSet &&
      prev.votedMap === next.votedMap &&
      prev.expandedSummaries === next.expandedSummaries &&
      prev.sectionsCollapsedFor === next.sectionsCollapsedFor &&
      prev.selectedColumnIndex === next.selectedColumnIndex &&
      prev.selectedCardId === next.selectedCardId &&
      prev.cardRefs === next.cardRefs &&
      prev.brief === next.brief &&
      prev.onBriefChange === next.onBriefChange &&
      prev.briefTone === next.briefTone &&
      prev.onBriefToneChange === next.onBriefToneChange &&
      prev.triggerBriefGenerate === next.triggerBriefGenerate &&
      prev.handlers === next.handlers
    )
  },
)

export { MobileColumnView }
export default MobileColumnView
