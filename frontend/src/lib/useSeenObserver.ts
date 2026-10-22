// React glue for lib/seen.ts (PR3c): one tracker for the App, one
// IntersectionObserver per root. App calls it with the phone list
// (``phoneScroller``, the element PR1b holds in state) and null on the
// desktop, where nothing is tracked (C2): the phone layout stays mounted
// there, hidden, and every rect would read 0.
//
// - A new root (a tab's list remounted) or none (the phone turned past
//   768px) disconnects the observer and clears its timers; the next one
//   observes every card still registered, so no card has to remount.
// - ``modalOpen`` and ``onSeen`` are read when they are needed, never
//   from a render-old closure.
// - The registry is stable: Column's memo keeps it, and Card's ref
//   callback runs only on attach and detach.

import { useEffect, useMemo, useRef, useState } from 'react'
import { SEEN_THRESHOLDS, createSeenTracker, type SeenRegistry } from './seen'

export type SeenObserverOptions = {
  /** One of App's sheets is open (App builds it from its own state). */
  modalOpen: boolean
  /** Cards seen on scroll, by id (``lib/seen.ts`` for the rules). */
  onSeen: (ids: number[]) => void
}

export type SeenOnScroll = SeenRegistry<Element> & {
  /** Handle what the observer has queued, now (before the hide flush). */
  takeRecords: () => void
}

export function useSeenObserver(root: HTMLElement | null, opts: SeenObserverOptions): SeenOnScroll {
  const modalRef = useRef(opts.modalOpen)
  modalRef.current = opts.modalOpen
  const onSeenRef = useRef(opts.onSeen)
  onSeenRef.current = opts.onSeen

  const [tracker] = useState(() =>
    createSeenTracker<Element>({
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (timer) => window.clearTimeout(timer as number),
      pageHidden: () => document.visibilityState === 'hidden',
      modalOpen: () => modalRef.current,
      onSeen: (ids) => onSeenRef.current(ids),
    }),
  )

  // Covered while the page is hidden or a sheet is open: the dwells stop,
  // and start over once the list is uncovered (iOS fires a hidden page's
  // frozen timers late, after it is back).
  const { modalOpen } = opts
  useEffect(() => {
    const sync = () => tracker.setCovered(document.visibilityState === 'hidden' || modalOpen)
    sync()
    document.addEventListener('visibilitychange', sync)
    return () => document.removeEventListener('visibilitychange', sync)
  }, [tracker, modalOpen])

  useEffect(() => {
    if (!root || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver((entries) => tracker.handle(entries, io), {
      root,
      threshold: SEEN_THRESHOLDS as number[],
    })
    tracker.attach(io)
    return () => tracker.detach(io)
  }, [root, tracker])

  return useMemo(
    () => ({ card: tracker.registry.card, marker: tracker.registry.marker, takeRecords: tracker.takeRecords }),
    [tracker],
  )
}
