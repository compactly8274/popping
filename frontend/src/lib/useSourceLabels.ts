import { useSyncExternalStore } from 'react'
import { getDisplayNamesVersion, subscribeDisplayNames } from './labels'

/** Re-renders the caller when a feed's typed name changes, so memoized
 *  rows (Card) pick up a rename without waiting for new entries. */
export function useSourceLabels(): number {
  return useSyncExternalStore(subscribeDisplayNames, getDisplayNamesVersion)
}
