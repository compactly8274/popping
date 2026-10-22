// A tab's window (PR4d, D5): how far back it shows stories, by the time
// they arrived (``fetched_at``). No imports (see feedRules.ts), so the
// unit tests run it under strip-types.
//
// Every tab shows the last 2 days unless its stored ``column_prefs``
// row says otherwise. 1 day is a cut of the 2-day data the tabs load;
// 7 days and All are fetched for the tab on their own (App's
// ``extraByTab``). Saved has no window: it lists every save.

/** The four windows a tab offers. */
export type Window = '1d' | '2d' | '7d' | 'all'

export const DEFAULT_WINDOW: Window = '2d'

/** The windows in the order the ⋯ control shows them. */
export const WINDOWS: ReadonlyArray<{ value: Window; label: string }> = [
  { value: '1d', label: '1 day' },
  { value: '2d', label: '2 days' },
  { value: '7d', label: '7 days' },
  { value: 'all', label: 'All' },
]

// Hours per window: what ``max_age_hours`` is sent, and what the device
// cuts at. All is 8760 (a year), the most the server takes, so the
// per-feed cap is never sent alone (it needs ``max_age_hours``).
const HOURS: Record<Window, number> = { '1d': 24, '2d': 48, '7d': 168, all: 8760 }

// What an older build's "Hide if older than" wrote; any of them reads
// as 1 day, the closest window.
const LEGACY_HOURS: readonly number[] = [1, 4, 24]

export function isWindow(x: unknown): x is Window {
  return x === '1d' || x === '2d' || x === '7d' || x === 'all'
}

/** The hours a window covers. */
export function windowHours(w: Window): number {
  return HOURS[w]
}

/** Wider than the 2 days every tab loads: the tab fetches its own rows. */
export function isWider(w: Window): boolean {
  return HOURS[w] > HOURS[DEFAULT_WINDOW]
}

/** The window a stored ``column_prefs`` row means (the same shape
 *  reaches here through a saved view's ``filter_presets``). An explicit
 *  ``window`` wins. Without one, the legacy ``maxAgeHours`` of 1, 4 or
 *  24 (the old control's choices) maps to 1 day; null, missing or
 *  anything else is the default. Null meant "all" to the old control,
 *  but every row an older build wrote has it, so reading it as anything
 *  but the default would change every tab. */
export function windowOf(row: unknown): Window {
  if (!row || typeof row !== 'object') return DEFAULT_WINDOW
  const r = row as Record<string, unknown>
  if (isWindow(r.window)) return r.window
  if (typeof r.maxAgeHours === 'number' && LEGACY_HOURS.includes(r.maxAgeHours)) return '1d'
  return DEFAULT_WINDOW
}

/** Whether a story that arrived at ``fetched_at`` is inside the window
 *  at ``now``. A story with no arrival time (or one that doesn't parse)
 *  is kept, as the age filter always kept it. */
export function inWindow(fetched_at: string | null | undefined, w: Window, now: number): boolean {
  if (w === 'all' || !fetched_at) return true
  const t = Date.parse(fetched_at)
  if (!Number.isFinite(t)) return true
  return t >= now - HOURS[w] * 60 * 60 * 1000
}

/** The window an empty tab offers next: 7 days from 1 or 2 days, All
 *  from 7 days, nothing from All (the end is the end). */
export function widerWindow(w: Window): Window | null {
  if (w === '1d' || w === '2d') return '7d'
  if (w === '7d') return 'all'
  return null
}
