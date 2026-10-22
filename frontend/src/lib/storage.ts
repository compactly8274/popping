// Namespaced localStorage keys for the small set of device-local
// UI-state values that aren't worth syncing to the server.
//
// What lives here now
// -------------------
//
// Five keys live in localStorage (two kept through the server-side
// preferences migration of 2026-07-02, three added later):
//
//   - ``mobileTab`` -- the key of the tab the phone shows
//     (``lib/columns.ts``: ``meta:foryou``, ``cat:news`` ...).
//     Per-device because "the tab the user is currently looking
//     at" has no meaning when shared across two devices.
//
//   - ``mobileColLast`` -- the index ``mobileTab`` replaced. Read
//     once, to migrate, and removed when ``mobileTab`` is first
//     written.
//
//   - ``briefCollapsed`` -- whether the BriefCard is collapsed
//     in the drawer. Same argument: a screen position, not
//     data.
//
//   - ``briefTone`` -- the Brief tone picked on this device
//     (Short, Story or Alerts), so a reload keeps it.
//
//   - ``firstRunAt`` -- when this device first loaded the read model
//     (PR3b, D33): stories fetched more than 48 hours before it count
//     as seen here, so the first deploy doesn't open every tab at
//     99+. Per-device on purpose: nothing is written to the server.
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
// is still centralised. The entries are device-only; new
// server-backed keys should NOT be added here.

const SCHEMA = 'v1'
const NAMESPACE = 'popping'

export const STORAGE_KEYS = {
  // The phone's tab, by key, so a reload (or a tab that comes or
  // goes around it) keeps the phone where it was. Pure screen
  // position; per-device is correct (the user looking at News on
  // their phone shouldn't move their laptop to News too).
  mobileTab: `${NAMESPACE}.${SCHEMA}.mobileTab`,
  // The old phone column index (-1 = Brief, 0..N-1), from before
  // tab keys. Only read to migrate it into ``mobileTab``.
  mobileColLast: `${NAMESPACE}.${SCHEMA}.mobileCol.last`,
  // BriefCard collapse preference. Boolean stored as '0' / '1' to
  // match the codebase's storage convention.
  briefCollapsed: `${NAMESPACE}.${SCHEMA}.brief.collapsed`,
  // The Brief tone ('terse' | 'narrative' | 'alert'), checked on read.
  briefTone: `${NAMESPACE}.${SCHEMA}.brief.tone`,
  // When this device first loaded the read model (ms since the epoch).
  firstRunAt: `${NAMESPACE}.${SCHEMA}.firstRunAt`,
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
