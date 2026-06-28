import { useEffect, useRef, useState } from 'react'
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
  prefs,
  onPrefsChange,
  totalCount,
  categoriesBySourceId,
}: Props) {
  const [popoverOpen, setPopoverOpen] = useState(false)
  const popoverRef = useRef<HTMLDivElement | null>(null)

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
        className="relative flex items-center justify-between px-1 pb-2 cursor-pointer select-none"
        title="tap to mark this column read"
      >
        {/* Thin gradient underline. Sits below the title row and
            gives each column a soft separator from its body without
            a hard border. The transparent-to-transparent endpoint
            keeps the line from "starting" — it reads as a continuous
            gradient flow rather than a divider stuck across the top. */}
        <div
          aria-hidden="true"
          className="absolute left-0 right-0 bottom-0 h-px bg-gradient-to-r from-transparent via-slate-700 to-transparent"
        />
        <div className="flex items-center gap-2 min-w-0">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300 truncate">
            {name}
          </h2>
          {/* New-entry chip. Only renders when newCount > 0 — when
              it hits 0 the user has acknowledged the column and the
              chip just disappears. Pulsing slightly is overkill; the
              blue bg against the dark header is enough signal. */}
          {newCount != null && newCount > 0 && (
            <span
              data-new-chip
              className="shrink-0 rounded-full bg-blue-600 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white"
              title={`${newCount} new since last visit`}
            >
              +{newCount} new
            </span>
          )}
          {/* Entry count. "X of Y" when filters are hiding some,
              plain "X" otherwise. text-slate-500 to keep it
              secondary to the new chip. */}
          <span className="text-xs text-slate-500">
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
            className="shrink-0 rounded px-2 py-0.5 text-xs text-slate-400 active:bg-slate-800 [@media(hover:hover)]:hover:bg-slate-800 [@media(hover:hover)]:hover:text-slate-100"
            aria-label="column preferences"
            aria-expanded={popoverOpen}
          >
            ⋯
          </button>
        )}

        {popoverOpen && prefs && onPrefsChange && (
          <div
            ref={popoverRef}
            data-column-popover
            // Position relative to the header so it lands just below
            // the ⋯ button on the right edge. min-w-[220px] keeps
            // the slider readable. z-20 puts it above cards but
            // below the Drawer (z-30+).
            className="absolute right-0 top-full mt-1 z-20 min-w-[220px] rounded border border-slate-700 bg-slate-900 shadow-xl p-3 text-xs space-y-3"
            onClick={(e) => e.stopPropagation()}
          >
            <div>
              <label className="block text-slate-400 mb-1">Sort</label>
              <select
                value={prefs.sort}
                onChange={(e) =>
                  onPrefsChange({ ...prefs, sort: e.target.value as ColumnPrefs['sort'] })
                }
                className="w-full rounded bg-slate-950 border border-slate-800 px-2 py-1 text-slate-100"
              >
                <option value="top">Top (composite score)</option>
                <option value="newest">Newest first</option>
                <option value="oldest">Oldest first</option>
              </select>
            </div>
            <div>
              <label className="block text-slate-400 mb-1">
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
                className="w-full"
              />
            </div>
            <div>
              <label className="block text-slate-400 mb-1">Hide if older than</label>
              <select
                value={prefs.maxAgeHours == null ? 'all' : String(prefs.maxAgeHours)}
                onChange={(e) => {
                  const v = e.target.value
                  onPrefsChange({
                    ...prefs,
                    maxAgeHours: v === 'all' ? null : Number(v),
                  })
                }}
                className="w-full rounded bg-slate-950 border border-slate-800 px-2 py-1 text-slate-100"
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
          <p className="text-sm text-slate-500 italic px-1">nothing yet</p>
        ) : (
          entries.map((e) => {
            // Build the ref-callback only when a Map is provided.
            // Card uses the ref to expose its DOM node for F6
            // keyboard nav. We don't allocate a new callback per
            // render — a stable callback would still re-attach but
            // this is cheap enough at column sizes.
            const refCb =
              cardRefs != null
                ? (el: HTMLElement | null) => {
                    // cardRefs can be either a Map or a ref-of-Map.
                    // The ref-of-Map shape is what App uses (a single
                    // mutable Map that all columns write into) so
                    // keyboard nav can find any card by id regardless
                    // of which column is currently mounted.
                    if ('current' in cardRefs) {
                      if (el) cardRefs.current.set(e.id, el)
                      else cardRefs.current.delete(e.id)
                    } else {
                      if (el) cardRefs.set(e.id, el)
                      else cardRefs.delete(e.id)
                    }
                  }
                : undefined
            return (
              <Card
                key={e.id}
                entry={e}
                sourceName={sourcesById.get(e.source_id)}
                unread={unreadIds == null ? false : unreadIds.has(e.id)}
                selected={selectedId === e.id}
                cardRef={refCb}
                category={categoriesBySourceId?.get(e.source_id)}
              />
            )
          })
        )}
      </div>
    </section>
  )
}