// Tab keys, and which tab the phone shows. No imports (see feedRules.ts),
// so the unit tests run it under strip-types.
//
// A tab is named by a key, never by its place in the strip: tabs can be
// moved and hidden (``category_layout``, lib/categories.ts), and categories
// come and go with their feeds, so an index stored on one render can point
// at another tab on the next.

export const BRIEF_KEY = 'meta:brief'
export const SAVED_KEY = 'meta:saved'
export const FORYOU_KEY = 'meta:foryou'
export const SEARCH_KEY = 'meta:search'
// The source filter's one column. PR8b removes it with the filter.
export const FILTER_KEY = 'meta:filter'

export type MetaKey =
  | typeof BRIEF_KEY
  | typeof SAVED_KEY
  | typeof FORYOU_KEY
  | typeof SEARCH_KEY
  | typeof FILTER_KEY
export type ColKey = MetaKey | `cat:${string}`

const META_KEYS: readonly string[] = [BRIEF_KEY, SAVED_KEY, FORYOU_KEY, SEARCH_KEY, FILTER_KEY]

// Longer than any key the app makes (categories are at most 40
// characters); anything longer in storage isn't ours.
const MAX_KEY_LENGTH = 200

/** The key of a category's tab. */
export function colKey(category: string): ColKey {
  return `cat:${category}`
}

/** A stored key, checked: one of the meta keys or ``cat:<name>``, else null. */
export function parseColKey(raw: unknown): ColKey | null {
  if (typeof raw !== 'string' || raw.length > MAX_KEY_LENGTH) return null
  if (META_KEYS.includes(raw)) return raw as MetaKey
  if (raw.startsWith('cat:') && raw.length > 4) return raw as ColKey
  return null
}

/** What this device has stored: a key, an index from before keys
 *  (``mobileColLast``: -1 is Brief, 0.. counts the tabs after it), or
 *  nothing. */
export type StoredTab = { key: ColKey } | { index: number } | null

/** The stored key wins; a legacy index is read only when no key is
 *  stored yet. */
export function readStoredTab(rawKey: string | null, rawIndex: string | null): StoredTab {
  const key = parseColKey(rawKey)
  if (key) return { key }
  if (rawIndex == null || rawIndex === '') return null
  const n = Number(rawIndex)
  return Number.isInteger(n) && n >= -1 ? { index: n } : null
}

/** One tab in strip order; ``size`` is how many stories it shows. */
export type Tab = { key: ColKey; size: number }

/** The tab to show (its key and index in ``tabs``; null and -1 only for
 *  an empty strip), and the key to store when the stored one should
 *  change (null keeps what is stored). */
export type TabChoice = { key: ColKey | null; index: number; store: ColKey | null }

export type ResolveOpts = {
  // The tab list is complete: preferences have loaded and the first
  // refresh has answered. Until then a missing tab may still arrive.
  loaded: boolean
  // The source filter has replaced the tabs with its one column; the
  // stored tab comes back when the filter is cleared.
  filterActive: boolean
  // The tab last shown in place of the stored one while nothing could
  // be stored (not loaded, or under the filter). Held by key until the
  // list settles, so a tab appearing in front of it (a save adding
  // Saved) can't move the phone the way it would move a place.
  standIn?: ColKey | null
}

/** The tab a device opens on when it has none stored: the first tab
 *  after Brief that shows something, For You first. With nothing to
 *  show anywhere, For You (a new install's "No stories yet" and its
 *  first-feed button), or the first tab after Brief when For You is
 *  hidden. */
export function freshTabIndex(tabs: readonly Tab[]): number {
  const forYou = tabs.findIndex((t) => t.key === FORYOU_KEY && t.size > 0)
  if (forYou >= 0) return forYou
  const full = tabs.findIndex((t) => t.key !== BRIEF_KEY && t.size > 0)
  if (full >= 0) return full
  const empty = tabs.findIndex((t) => t.key === FORYOU_KEY)
  if (empty >= 0) return empty
  const any = tabs.findIndex((t) => t.key !== BRIEF_KEY)
  return any >= 0 ? any : 0
}

/** Which tab the phone shows.
 *
 *  - The wanted key is in the strip: show it.
 *  - Until the list settles (loaded, no filter), a stand-in shown
 *    earlier for a stored tab that is missing, a legacy index or a
 *    fresh device stays shown while it is in the strip. Every rule
 *    below picks a place, and places shift when a tab appears in
 *    front; the stand-in is followed by key, as a stored tab is. Once
 *    settled the rules below apply, and the tab they pick is stored.
 *  - It is missing: show the tab now at ``lastIndex`` (the place the
 *    stored tab last stood, so the neighbour of a tab that just went),
 *    or the fresh-device tab when it was never shown here. Once the
 *    list is loaded (and no filter replaces it), that tab becomes the
 *    stored one, so the tab coming back later (a hidden tab shown
 *    again, a category whose stories return) can't pull the phone
 *    back. Before that, the stored key is kept, and so
 *    it is when only Brief is left. A stored ``meta:filter`` whose
 *    filter was cleared gets the fresh-device tab.
 *  - A legacy index is the tab at that place after Brief (clamped to the
 *    last, -1 is Brief), counting an empty Saved or For You out, as the
 *    strip of the build that stored it did. It is stored as a key once
 *    loaded.
 *  - Nothing stored: the fresh-device tab, stored once loaded.
 *
 *  The fresh-device tab is stored only when it shows something: an
 *  install with no stories yet would otherwise store Brief and open on
 *  it once For You fills. */
export function resolveTab(
  tabs: readonly Tab[],
  wanted: StoredTab,
  lastIndex: number | null,
  { loaded, filterActive, standIn = null }: ResolveOpts,
): TabChoice {
  if (tabs.length === 0) return { key: null, index: -1, store: null }
  const settle = loaded && !filterActive
  const pick = (index: number): TabChoice => ({
    key: tabs[index].key,
    index,
    store: settle ? tabs[index].key : null,
  })
  const fresh = (): TabChoice => {
    const index = freshTabIndex(tabs)
    return tabs[index].size > 0 ? pick(index) : { ...pick(index), store: null }
  }
  const shown = (key: ColKey | null): TabChoice | null => {
    const at = key == null ? -1 : tabs.findIndex((t) => t.key === key)
    return at >= 0 ? { key: tabs[at].key, index: at, store: null } : null
  }
  const stored = wanted && 'key' in wanted ? shown(wanted.key) : null
  if (stored) return stored
  // Brief never stands in by choice (see below), so it is never held.
  const held = !settle && standIn !== BRIEF_KEY ? shown(standIn) : null
  if (held) return held
  if (wanted && 'key' in wanted) {
    // The filter's column has no place of its own once the filter is
    // cleared.
    if (lastIndex == null || wanted.key === FILTER_KEY) return fresh()
    const next = pick(Math.min(Math.max(lastIndex, 0), tabs.length - 1))
    // Brief stands in only when no other tab is left (every story gone
    // or hidden); the stored tab is kept for when stories come back.
    return next.key === BRIEF_KEY ? { ...next, store: null } : next
  }
  if (wanted && 'index' in wanted) {
    if (wanted.index === -1) {
      const brief = tabs.findIndex((t) => t.key === BRIEF_KEY)
      if (brief >= 0) return pick(brief)
    } else {
      // Counted as the strip stood when the index was stored: Saved and
      // For You were in it only while they had stories.
      const counted = (t: Tab) => t.key !== BRIEF_KEY && (t.size > 0 || (t.key !== SAVED_KEY && t.key !== FORYOU_KEY))
      const after = tabs.flatMap((t, i) => (counted(t) ? [i] : []))
      if (after.length) return pick(after[Math.min(wanted.index, after.length - 1)])
    }
    return fresh()
  }
  return fresh()
}
