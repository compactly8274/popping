// Namespaced localStorage keys for the small set of device-local
// UI-state values that aren't worth syncing to the server.
//
// What lives here now
// -------------------
//
// Only two keys remain in localStorage as of the server-side
// preferences migration (2026-07-02):
//
//   - ``mobileColLast`` -- the column index the user was last
//     viewing on the mobile swipe layout. Per-device because
//     "the column the user is currently looking at" has no
//     meaning when shared across two devices.
//
//   - ``briefCollapsed`` -- whether the BriefCard is collapsed
//     in the drawer. Same argument: a screen position, not
//     data.
//
// What moved off localStorage
// ---------------------------
//
// Eight per-user, per-device keys used to live here. They all
// moved to the server-backed ``user_preferences`` store
// (``lib/preferences.tsx``). The migration is one-way: the
// first launch after the deploy POSTs the old localStorage
// values to the server, then deletes the localStorage entries
// (gated by a ``popping.v1.preferences.seeded`` flag so the
// seed runs at most once per browser). The keys that moved:
//
//   - lastViewed      -> per-column "I last saw this column at"
//   - columnPrefs     -> per-column sort/min-score/max-age
//   - readEntries     -> per-column "marked read" entry ids
//   - columnSections  -> per-column Fresh/History collapse
//   - hiddenEntries   -> per-user "hide this entry"
//   - starredEntries  -> per-user "saved for later"
//   - filterPresets   -> user-saved dashboard views
//   - historyGroupBy  -> History tab group-by mode
//
// ``STORAGE_KEYS`` is kept as a frozen record so the namespacing
// is still centralised. The two remaining entries are device-
// only; new server-backed keys should NOT be added here.

const SCHEMA = 'v1'
const NAMESPACE = 'popping'

export const STORAGE_KEYS = {
  // The last mobile-view column index (0..N-1). Tracked so swiping
  // between columns on phone doesn't reset position on refresh.
  // Pure screen position; per-device is correct (the user looking
  // at column 3 on their phone shouldn't shift their laptop to
  // column 3 too).
  mobileColLast: `${NAMESPACE}.${SCHEMA}.mobileCol.last`,
  // BriefCard collapse preference. Boolean stored as '0' / '1' to
  // match the codebase's storage convention.
  briefCollapsed: `${NAMESPACE}.${SCHEMA}.brief.collapsed`,
} as const

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
