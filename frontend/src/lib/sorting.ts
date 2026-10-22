// How a tab orders its stories: Newest or Best. Pure, so the unit
// tests run it under strip-types (only a type import and columns.ts).
//
// Best means the server's ranking. ``/api/entries`` already orders a
// category by ``composite_score``, so a category (or the filter's
// column) re-sorts by it. For You's ranking multiplies the score by a
// convergence boost it doesn't send (routes/foryou.py), so For You's
// Best is the order the server sent, never a client re-sort. Saved is
// never re-sorted here: App lists saves newest save first
// (``lib/saved``), whatever the stored sort says.

import type { ColKey } from './columns.ts'
import { FORYOU_KEY, SAVED_KEY } from './columns.ts'
import { DEFAULT_WINDOW, windowOf, type Window } from './window.ts'

/** The two orders a tab offers. */
export type Sort = 'newest' | 'best'

/** A tab's prefs as ``prefsFor`` reads them and Column writes them.
 *  ``top`` and ``oldest`` come from older builds; ``minScore`` is kept
 *  so the stored shape doesn't change, but nothing filters on it any
 *  more. ``window`` is the tab's window (lib/window, PR4d); a row an
 *  older build wrote has a numeric ``maxAgeHours`` instead, which
 *  ``prefsFor`` reads through ``windowOf``. ``maxAgeHours`` is always
 *  written as null next to ``window``: an older build (a desktop tab
 *  left open) keeps a row as it is only when its ``maxAgeHours`` is
 *  null, so its writes then spread the row, ``window`` included; a row
 *  without the field would be rebuilt there without ``window``, and
 *  its next change of sort would put the tab back to 2 days. */
export type TabPrefs = {
  sort: 'top' | 'newest' | 'oldest' | 'best'
  minScore: number
  window: Window
  maxAgeHours: null
}

// Module constants, not fresh objects: Column's memo compares prefs by
// reference, so a tab with no stored row must get the same object on
// every render.
export const NEWEST_PREFS: TabPrefs = { sort: 'newest', minScore: 0, window: DEFAULT_WINDOW, maxAgeHours: null }
export const BEST_PREFS: TabPrefs = { sort: 'best', minScore: 0, window: DEFAULT_WINDOW, maxAgeHours: null }

/** The fields a story is ordered by. */
export type Sortable = {
  id: number
  published_at: string | null
  fetched_at: string | null
  composite_score: number
}

/** When a story is from: published, else fetched. Null when it has
 *  neither (or neither parses). */
export function entryTime(e: Sortable): number | null {
  const iso = e.published_at ?? e.fetched_at
  if (!iso) return null
  const t = new Date(iso).getTime()
  return Number.isFinite(t) ? t : null
}

/** Newest first; dated stories before undated ones; then the higher id
 *  first, so equal times keep one order across renders. */
export function byNewest(a: Sortable, b: Sortable): number {
  const ta = entryTime(a)
  const tb = entryTime(b)
  if (ta != null && tb != null && ta !== tb) return tb - ta
  if (ta == null && tb != null) return 1
  if (tb == null && ta != null) return -1
  return b.id - a.id
}

/** Highest ``composite_score`` first, then ``byNewest``. */
export function byBest(a: Sortable, b: Sortable): number {
  const sa = Number.isFinite(a.composite_score) ? a.composite_score : 0
  const sb = Number.isFinite(b.composite_score) ? b.composite_score : 0
  if (sa !== sb) return sb - sa
  return byNewest(a, b)
}

/** The sort a tab has before anything is stored for it. */
export function defaultSort(key: ColKey): Sort {
  return key === FORYOU_KEY ? 'best' : 'newest'
}

/** A stored sort, read for one tab. ``top``, ``oldest``, a missing or
 *  an unknown value become the tab's default: Best on For You (it never
 *  honoured a stored sort, so whatever is stored there was never seen),
 *  Newest elsewhere (``top`` had no visible effect: both sections were
 *  re-sorted newest-first). */
export function normalizeSort(sort: unknown, key: ColKey): Sort {
  if (sort === 'newest' || sort === 'best') return sort
  return defaultSort(key)
}

/** A tab's stories in its order. A new array, except on Saved and on
 *  For You's Best, which keep the order they came in. */
export function sortEntries<T extends Sortable>(entries: T[], sort: Sort, key: ColKey): T[] {
  if (key === SAVED_KEY) return entries
  if (sort === 'newest') return [...entries].sort(byNewest)
  if (key === FORYOU_KEY) return entries
  return [...entries].sort(byBest)
}

// One merged object per stored row and default, so a row that needed
// filling in still reaches Column as the same object on every render.
// Rows are replaced, never changed, on write, so the cache can't go
// stale.
const merged = new WeakMap<object, Partial<Record<Sort, TabPrefs>>>()

/** A tab's prefs: the stored row (``columnPrefs[tab.name]``; rows stay
 *  keyed by the column's name) over the tab's defaults, with the sort
 *  read by ``normalizeSort`` and the window by ``windowOf``. A spread of
 *  the result is what Column writes back, so a write can never store
 *  ``top`` or ``oldest``, and a row that still holds an older build's
 *  numeric ``maxAgeHours`` is written back with ``window`` and
 *  ``maxAgeHours: null`` (``TabPrefs``). */
export function prefsFor(
  columnPrefs: Readonly<Record<string, unknown>>,
  tab: { key: ColKey; name: string },
): TabPrefs {
  const fallback = defaultSort(tab.key)
  const base = fallback === 'best' ? BEST_PREFS : NEWEST_PREFS
  const row = Object.prototype.hasOwnProperty.call(columnPrefs, tab.name) ? columnPrefs[tab.name] : undefined
  if (!row || typeof row !== 'object') return base
  const r = row as Record<string, unknown>
  const sort = normalizeSort(r.sort, tab.key)
  const minScore = typeof r.minScore === 'number' && Number.isFinite(r.minScore) ? r.minScore : 0
  const window = windowOf(row)
  if (r.sort === sort && r.minScore === minScore && r.window === window && r.maxAgeHours === null) return row as TabPrefs
  const cached = merged.get(row)?.[fallback]
  if (cached) return cached
  const out: TabPrefs = { sort, minScore, window, maxAgeHours: null }
  merged.set(row, { ...merged.get(row), [fallback]: out })
  return out
}
