// React glue for lib/feedOrder.ts (PR3b): one snapshot per tab, kept in a
// ref across renders. App calls ``partition`` on every render with the
// tabs it draws.
//
// - A tab App doesn't draw in a render is reset, so a tab switch starts
//   both tabs over: the one left and the one opened.
// - Until ``ready`` (the preferences have answered and the first refresh
//   is done) nothing is kept: a snapshot taken before the stored marks
//   arrive would leave stories already seen above the marker.
// - Every function here is stable, for handlers that Column's memo keeps
//   across renders: ``resetSnapshot`` (one tab, or every tab) and
//   ``setHold`` change what the next render draws, and ``latest`` reads
//   the last partition from the ref, never a render-old closure.

import { useCallback, useMemo, useRef, useState } from 'react'
import { partitionTab, type Item, type Partition, type Snapshot } from './feedOrder'

/** A tab to partition: its key, its stories in its order, and that order
 *  as a string (a new one starts a new snapshot). */
export type FeedTab<E extends Item> = { key: string; entries: readonly E[]; order: string }

export type Sections<E> = { unseen: E[]; earlier: E[] }

export type TabPartition<E extends Item> = Partition<E> & {
  /** ``{unseen, earlier}`` as one object, the same one while both arrays
   *  are (Column's ``sections`` prop; its memo compares by reference). */
  sections: Sections<E>
}

export type PartitionOptions<E extends Item> = {
  isSeen: (e: E) => boolean
  isRemoved: (e: E) => boolean
  /** Seen, its mark not written yet (``partitionTab``'s ``isPending``). */
  isPending?: (e: E) => boolean
  /** Goes in at its place, not at the end (``partitionTab``'s ``inOrder``). */
  inOrder?: (e: E) => boolean
  ready: boolean
}

export type FeedOrder<E extends Item> = {
  /** This render's partitions of ``tabs``, by tab key. Call once per
   *  render, with every tab App draws. */
  partition: (tabs: readonly FeedTab<E>[], opts: PartitionOptions<E>) => ReadonlyMap<string, TabPartition<E>>
  /** Start a tab's snapshot over on the next render; no key: every tab. */
  resetSnapshot: (key?: string) => void
  /** Column's report that its marker is above the screen (or not). */
  setHold: (key: string, hold: boolean) => void
  /** The last partition of a tab, read now (handlers). */
  latest: (key: string) => TabPartition<E> | undefined
}

export function useFeedOrder<E extends Item>(): FeedOrder<E> {
  const snaps = useRef(new Map<string, Snapshot<E>>())
  const resets = useRef(new Set<string>())
  const holds = useRef(new Map<string, boolean>())
  const sectionsRef = useRef(new Map<string, Sections<E>>())
  const latestRef = useRef<ReadonlyMap<string, TabPartition<E>>>(new Map())
  const [, rerender] = useState(0)

  const partition = useCallback((tabs: readonly FeedTab<E>[], opts: PartitionOptions<E>) => {
    const parts = new Map<string, TabPartition<E>>()
    for (const tab of tabs) {
      if (parts.has(tab.key)) continue
      const p = partitionTab({
        entries: tab.entries,
        isSeen: opts.isSeen,
        isRemoved: opts.isRemoved,
        isPending: opts.isPending,
        inOrder: opts.inOrder,
        order: tab.order,
        snapshot: opts.ready ? snaps.current.get(tab.key) : null,
        hold: holds.current.get(tab.key) ?? false,
        reset: resets.current.has(tab.key),
      })
      if (opts.ready) {
        snaps.current.set(tab.key, p.snapshot)
        resets.current.delete(tab.key)
      }
      const prev = sectionsRef.current.get(tab.key)
      const sections =
        prev && prev.unseen === p.unseen && prev.earlier === p.earlier ? prev : { unseen: p.unseen, earlier: p.earlier }
      sectionsRef.current.set(tab.key, sections)
      parts.set(tab.key, { ...p, sections })
    }
    // A tab not drawn now starts over the next time it is.
    for (const key of snaps.current.keys()) if (!parts.has(key)) resets.current.add(key)
    latestRef.current = parts
    return parts
  }, [])

  const resetSnapshot = useCallback((key?: string) => {
    if (key == null) for (const k of snaps.current.keys()) resets.current.add(k)
    else resets.current.add(key)
    rerender((n) => n + 1)
  }, [])

  const setHold = useCallback((key: string, hold: boolean) => {
    if ((holds.current.get(key) ?? false) === hold) return
    holds.current.set(key, hold)
    // Let go: the arrivals that waited go in now.
    if (!hold && (snaps.current.get(key)?.held.length ?? 0) > 0) rerender((n) => n + 1)
  }, [])

  const latest = useCallback((key: string) => latestRef.current.get(key), [])

  return useMemo(() => ({ partition, resetSnapshot, setHold, latest }), [partition, resetSnapshot, setHold, latest])
}
