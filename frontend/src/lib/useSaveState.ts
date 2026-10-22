// The save store's state for one field of one feed, as a React hook.
// In lib, not SaveStatus.tsx, so that file exports only components
// (react-refresh).

import { useCallback, useSyncExternalStore } from 'react'
import { getSaveState, subscribe, type Field, type SaveState } from './sourceSaves'

/** getSaveState returns the same object until the state changes, as
 *  useSyncExternalStore needs; subscribe and getSnapshot are memoised
 *  per (id, field). */
export function useSaveState(id: number, field: Field): SaveState {
  const sub = useCallback((cb: () => void) => subscribe(id, field, cb), [id, field])
  const snap = useCallback(() => getSaveState(id, field), [id, field])
  return useSyncExternalStore(sub, snap, snap)
}
