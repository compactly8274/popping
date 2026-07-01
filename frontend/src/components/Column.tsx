import { useCallback, useEffect, useRef, useState } from 'react'
import type { Entry } from '../api'
import { Card } from './Card'

// Per-column sort/filter preferences. Owned by App (so it persists
// across columns and across renders) and passed in here for the
// popover to read/write. ``maxAgeHours === null`` means "no max-age
// filter" — the popover's "all" option. Same shape is persisted to
// localStorage in App.
export type ColumnPrefs = {
  sort: 'top' | 'newest' | 'oldest'
  minScore: number
  maxAgeHours: number | null
}

export const DEFAULT_PREFS: ColumnPrefs = {
  sort: 'top',
  minScore: 0,
  maxAgeHours: null,
}

type Props = {
  name: string
  entries: Entry[]
  sourcesById: Map<number, string>
  // Number of entries in this column that are "new since the user's
  // last visit to this column". Rendered as a chip in the header so
  // the user knows to come look. ``undefined`` means we haven't
  // computed it yet (initial mount) and we show nothing.
  newCount?: number
  // Set of entry IDs that are unread. The Card dims itself when its
  // id is not in this set. App derives this from per-column
  // lastViewedAt timestamps so it survives reloads.
  unreadIds?: Set<number>
  // The id of the currently keyboard-selected entry in this column.
  // Only the matching Card renders the blue focus ring. ``-1``
  // (or undefined) means no selection in this column.
  selectedId?: number
  // Ref callback so App can focus + scroll-into-view on keyboard nav.
  cardRefs?: Map<number, HTMLElement | null> | React.MutableRefObject<Map<number, HTMLElement | null>>
  // Triggered when the user taps the column header. App uses this
  // to mark the column read in localStorage.
  onMarkRead?: () => void
  // Per-card mark-read. App flips the entry's read state via this —
  // see ``markEntryRead`` in App.tsx. The Column just forwards the
  // clicked entry id up so the keyboard ``m`` shortcut and the
  // button on each card share one code path.
  onMarkEntryRead?: (entryId: number) => void
  // Prefs + setter for the ⋯ popover. For You column passes
  // ``undefined`` — no popover, just the chip + count.
  prefs?: ColumnPrefs
  onPrefsChange?: (next: ColumnPrefs) => void
  // The total number of entries before prefs filtering. We render
  // "X of Y" so the user can see when their filters are hiding
  // content. App passes this; falls back to entries.length when
  // prefs are not in play.
  totalCount?: number
  // Optional map sourceId → category. Passed through to each Card so
  // it can render the colored left stripe. Without it the cards just
  // skip the stripe.
  categoriesBySourceId?: Map<number, string>
  // Per-card summary expansion set + toggle callback. App owns both
  // so the keyboard ``s`` shortcut and the per-card chevron stay in
  // sync. Both optional — older callers (none today) just omit
  // them and the cards render with summaries permanently collapsed.
  expandedSummaries?: Set<number>
  onToggleSummary?: (entryId: number) => void
  // Per-card "hide this entry" action. When present, each card's
  // context menu gets a "Hide this entry" item. App wires this
  // to the localStorage-backed hidden set; the entry disappears
  // from every column + the For You row immediately.
  onHideEntry?: (entryId: number) => void
}

export function Column({
  name,
  entries,
  sourcesById,
  newCount,
  unreadIds,
  selectedId,
  cardRefs,
  onMarkRead,
  onMarkEntryRead,
  prefs,
  onPrefsChange,
  totalCount,
  categoriesBySourceId,
  expandedSummaries,
  onToggleSummary,
  onHideEntry,
}: Props) {
  const [popoverOpen, setPopoverOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement | null>(null)

  // Keep the latest ``cardRefs`` value in a ref so we can ship a
  // stable ref callback to each ``Card``. Without this, the inline
  // lambda in the ``entries.map`` below would defeat the
  // ``Card.memo`` — every Column re-render allocates new closures
  // and React would re-mount every card's DOM ref. The cardRefs
  // shape (Map vs ref-of-Map) can change between renders too, so we
  // also stash the active setter in the ref to keep the dispatcher
  // single-sourced.
  const cardRefsLatest = useRef(cardRefs)
  cardRefsLatest.current = cardRefs

  // Stable ref callback. Same function reference across renders;
  // reads the latest ``cardRefs`` and ``entry.id`` from refs so
  // each card lands in the right slot regardless of when it
  // mounted.
  const setCardRef = useCallback((entryId: number) => (el: HTMLElement | null) => {
    const refs = cardRefsLatest.current
    if (!refs) return
    if ('current' in refs) {
      if (el) refs.current.set(entryId, el)
      else refs.current.delete(entryId)
    } else {
      if (el) refs.set(entryId, el)
      else refs.delete(entryId)
    }
  }, [])

  // Click-outside dismissal. The popover is small and short-lived;
  // a backdrop div would be overkill. ``mousedown`` (not ``click``)
  // so the act of opening the popover doesn't immediately close it
  // — the click that opened it lands before the listener is
  // attached, but if the user re-clicks on the ⋯ button to dismiss
  // it, that's already handled by the button's own onClick.
  useEffect(() => {
    if (!popoverOpen) return
    const onDocMouseDown = (e: MouseEvent) => {
      const target = e.target as Node
      if (popoverRef.current && !popoverRef.current.contains(target)) {
        setPopoverOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [popoverOpen])

  // Esc closes the popover. Same effect slot as click-outside.
  useEffect(() => {
    if (!popoverOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPopoverOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [popoverOpen])

  // Header tap on desktop marks the column read. On mobile the same
  // element exists (the column header is visible on touch layouts
  // too — the swipe changes the visible column but the header is
  // always rendered). The handler stays the same on both.
  const onHeaderClick = () => {
    onMarkRead?.()
  }
  // Keyboard support — the header is interactive (it triggers an
  // action), so it should be a button. role="button" + tabIndex={0}
  // matches the pattern BriefCard uses for its collapse header.
  const onHeaderKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onMarkRead?.()
    }
  }

  const visible = totalCount ?? entries.length
  const showTotal = totalCount != null && totalCount !== entries.length

  return (
    <section className="flex flex-col h-full overflow-visible">
      <header
        role="button"
        tabIndex={0}
        onClick={onHeaderClick}
        onKeyDown={onHeaderKey}
        className="relative flex items-center justify-between px-1 pb-2 min-h-[44px] cursor-pointer select-none"
        title="tap to mark this column read"
      >
        {/* Thin hairline underline. iOS-style list-header separator —
          a single 1px line right at the bottom of the title row,
          rendered slightly inset so the line reads as belonging to
          the column rather than the page. */}
        <div
          aria-hidden="true"
          className="absolute left-1 right-1 bottom-0 h-px bg-hairline"
        />
        <div className="flex items-center gap-2 min-w-0">
          <h2 className="text-ios-caption uppercase tracking-wide text-label-tertiary truncate">
            {name}
          </h2>
          {/* New-entry chip. Only renders when newCount > 0 — when
              it hits 0 the user has acknowledged the column and the
              chip just disappears. Pulses slightly via the accent
              color rather than an animation, matching the iOS
              "badge" pattern. */}
          {newCount != null && newCount > 0 && (
            <span
              data-new-chip
              className="shrink-0 rounded-full bg-accent px-2 py-0.5 text-ios-caption font-semibold uppercase tracking-wide text-white"
              title={`${newCount} new since last visit`}
            >
              +{newCount} new
            </span>
          )}
          {/* Entry count. "X of Y" when filters are hiding some,
              plain "X" otherwise. text-label-secondary to keep it
              secondary to the new chip. */}
          <span className="text-ios-caption text-label-secondary">
            {showTotal ? `${entries.length} of ${visible}` : visible}
          </span>
        </div>

        {/* Sort/filter popover trigger. Hidden for For You because
            the backend already pre-sorts by composite_score. We
            identify For You by name rather than a prop because the
            column already gets the name anyway and we'd be adding
            ceremony for a single special case. */}
        {onPrefsChange && name !== 'For You' && (
          <button
            data-column-action="prefs"
            onClick={(e) => {
              // Don't let the click bubble up to the header — it
              // would also fire onMarkRead. The header click is a
              // useful action but not when the user is just opening
              // preferences.
              e.stopPropagation()
              setPopoverOpen((p) => !p)
            }}
            className="shrink-0 w-8 h-8 flex items-center justify-center rounded-full text-label-secondary active:bg-bg-elevated"
            aria-label="column preferences"
            aria-expanded={popoverOpen}
          >
            <MoreIcon className="w-4 h-4" />
          </button>
        )}

        {popoverOpen && prefs && onPrefsChange && (
          <div
            ref={popoverRef}
            data-column-popover
            // Position relative to the header so it lands just below
            // the more-button on the right edge. min-w-[240px] keeps
            // the slider readable. z-20 puts it above cards but
            // below the Drawer (z-30+). The popover is the only
            // place we still render a popover-shaped surface — the
            // rest of the dashboard uses sheets / grouped lists.
            className="absolute right-0 top-full mt-2 z-20 min-w-[240px] rounded-ios-lg bg-bg-elevated shadow-2xl p-3 text-ios-body space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div>
              <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
                Sort
              </label>
              <select
                value={prefs.sort}
                onChange={(e) =>
                  onPrefsChange({ ...prefs, sort: e.target.value as ColumnPrefs['sort'] })
                }
                className="w-full min-h-[36px] rounded-ios bg-bg-surface border border-hairline px-2 text-label-primary"
              >
                <option value="top">Top (composite score)</option>
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
              </select>
            </div>
            <div>
              <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
                Min score: {prefs.minScore}
              </label>
              <input
                type="range"
                min={0}
                max={100}
                step={5}
                value={prefs.minScore}
                onChange={(e) =>
                  onPrefsChange({ ...prefs, minScore: Number(e.target.value) })
                }
                className="w-full accent-accent"
              />
            </div>
            <div>
              <label className="block text-ios-caption uppercase tracking-wide text-label-tertiary mb-1">
                Hide if older than
              </label>
              <select
                value={prefs.maxAgeHours == null ? 'all' : String(prefs.maxAgeHours)}
                onChange={(e) => {
                  const v = e.target.value
                  onPrefsChange({
                    ...prefs,
                    maxAgeHours: v === 'all' ? null : Number(v),
                  })
                }}
                className="w-full min-h-[36px] rounded-ios bg-bg-surface border border-hairline px-2 text-label-primary"
              >
                <option value="all">All</option>
                <option value="1">1 hour</option>
                <option value="4">4 hours</option>
                <option value="24">24 hours</option>
              </select>
            </div>
          </div>
        )}
      </header>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {entries.length === 0 ? (
          <p className="text-ios-body text-label-secondary italic px-1">nothing yet</p>
        ) : (
          entries.map((e) => {
            // Stable ref callback. The previous version allocated a
            // new closure per card per Column render, defeating the
            // ``memo`` on ``Card``. Instead, ``Card`` writes its own
            // DOM node into ``cardRefs`` keyed by entry id — the
            // Column doesn't need to bind anything, the Card knows
            // its own id and looks itself up.
            //
            // The custom-equal in ``Card.memo`` ignores the
            // ``cardRef`` prop entirely (it's a sentinel ``null`` /
            // ``undefined`` from Column), so any callback churn
            // here is free.
            return (
              <Card
                key={e.id}
                entry={e}
                sourceName={sourcesById.get(e.source_id)}
                unread={unreadIds == null ? false : unreadIds.has(e.id)}
                selected={selectedId === e.id}
                cardRef={
                  cardRefs
                    ? (el: HTMLElement | null) => {
                        // cardRefs can be either a Map or a
                        // ref-of-Map. The ref-of-Map shape is what
                        // App uses (a single mutable Map that all
                        // columns write into) so keyboard nav can
                        // find any card by id regardless of which
                        // column is currently mounted.
                        if ('current' in cardRefs) {
                          if (el) cardRefs.current.set(e.id, el)
                          else cardRefs.current.delete(e.id)
                        } else {
                          if (el) cardRefs.set(e.id, el)
                          else cardRefs.delete(e.id)
                        }
                      }
                    : undefined
                }
                category={categoriesBySourceId?.get(e.source_id)}
                onMarkRead={onMarkEntryRead ? () => onMarkEntryRead(e.id) : undefined}
                expanded={expandedSummaries?.has(e.id) ?? false}
                onToggleSummary={onToggleSummary ? () => onToggleSummary(e.id) : undefined}
                onHide={onHideEntry ? () => onHideEntry(e.id) : undefined}
              />
            )
          })
        )}
      </div>
    </section>
  )
}

// iOS-style "more" glyph — three horizontal dots. Used for the
// column-preferences affordance; matches the SF Symbols
// "ellipsis" pictogram at small sizes.
function MoreIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <circle cx="6"  cy="12" r="1.75" />
      <circle cx="12" cy="12" r="1.75" />
      <circle cx="18" cy="12" r="1.75" />
    </svg>
  )
}
