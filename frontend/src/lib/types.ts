/**
 * Shared types used by both ``App.tsx`` and ``lib/preferences.tsx``.
 *
 * These were originally duplicated (with comments acknowledging the
 * duplication) to avoid a circular import. Moving them here breaks
 * the cycle by giving both modules a leaf import to share, and
 * eliminates a class of silent drift where a future field added to
 * one module's copy wouldn't reach the other.
 *
 * Adding a new column-shape type? Add it here, not in either
 * consumer, and have both ``App.tsx`` and ``preferences.tsx`` import
 * from this module.
 */

/** One column's sort/filter preferences. */
export type ColumnPrefsValue = {
  sort: 'top' | 'newest' | 'oldest'
  minScore: number
  maxAgeHours: number | null
}

/** One column's Fresh/History section collapse state. */
export type ColumnSectionsValue = {
  newCollapsed: boolean
  historyCollapsed: boolean
}
