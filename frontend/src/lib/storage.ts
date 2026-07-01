// Namespaced localStorage keys. The ``popping.`` prefix avoids
// collisions with anything else on the same origin (a private dashboard
// host may also be running other apps), and keeps DevTools storage
// filtering clean. The schema version (``v1``) lets us bump the
// namespace once without crashing older browsers — bump it whenever a
// stored shape changes in an incompatible way.
//
// Public constants:
//   - ``STORAGE_KEYS`` — record mapping logical name → fully-qualified key.
//   - ``safeGetItem`` / ``safeSetItem`` / ``safeRemoveItem`` — wrappers
//     that tolerate SSR / private-mode / quota errors. ``setItem``
//     returns ``true`` on success, ``false`` if the write was
//     rejected (callers log / ignore — the in-memory state is the
//     source of truth for the current session).

const SCHEMA = 'v1'
const NAMESPACE = 'popping'

export const STORAGE_KEYS = {
  // Per-column "I last saw this column at" timestamps. JSON map
  // ``{ [colName: string]: iso8601 }``. Used by App to compute the
  // "new since last visit" chip on each column header.
  lastViewed: `${NAMESPACE}.${SCHEMA}.col.lastViewed`,
  // Per-column sort/filter prefs (sort, minScore, maxAgeHours).
  // JSON map ``{ [colName: string]: ColumnPrefs }``.
  columnPrefs: `${NAMESPACE}.${SCHEMA}.col.prefs`,
  // The last mobile-view column index (0..N-1). Tracked so swiping
  // between columns on phone doesn't reset position on refresh.
  mobileColLast: `${NAMESPACE}.${SCHEMA}.mobileCol.last`,
  // Per-column manual read entries. App keeps a set of "I marked
  // this card read" ids per column so the dim state survives
  // reloads without persisting across the source's natural refresh
  // window. JSON map ``{ [colName: string]: number[] }``.
  readEntries: `${NAMESPACE}.${SCHEMA}.col.readEntries`,
  // Per-user "hide this entry" set. The user dismisses an entry
  // via the card's context menu (right-click / long-press). The
  // ids are flattened into a single number[] (rather than the
  // per-column shape ``readEntries`` uses) because "hide" is
  // entry-global: once an entry is hidden it shouldn't surface
  // in any column or in the For You row, regardless of which
  // column currently shows it. JSON array of numbers, trimmed
  // to ``MAX_HIDDEN`` to prevent unbounded growth.
  hiddenEntries: `${NAMESPACE}.${SCHEMA}.hidden.entries`,
  // BriefCard collapse preference. Boolean stored as '0' / '1' to
  // match the rest of the codebase's storage convention.
  briefCollapsed: `${NAMESPACE}.${SCHEMA}.brief.collapsed`,
} as const

// Same trim cap as ``readEntries``: keep the most-recent decisions
// (the oldest hides are also the ones the user is least likely to
// care about remembering). 1000 entries × ~6 bytes per id = ~6 KB,
// comfortably under the 5 MB localStorage quota.
export const MAX_HIDDEN = 1000

export function safeGetItem(key: string): string | null {
  try {
    return window.localStorage?.getItem(key) ?? null
  } catch {
    // Private mode / quota / disabled storage. Treat as missing.
    return null
  }
}

export function safeSetItem(key: string, value: string): boolean {
  try {
    window.localStorage?.setItem(key, value)
    return true
  } catch {
    // Quota exceeded / private-mode rejection. Caller decides whether
    // to log; the in-memory state is the source of truth for the
    // current session so the UI keeps working.
    return false
  }
}

export function safeRemoveItem(key: string): void {
  try {
    window.localStorage?.removeItem(key)
  } catch {
    // See safeGetItem — best effort.
  }
}
