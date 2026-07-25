/**
 * Pure helpers for the interaction-event queue. Extracted from
 * interactions.ts so the dedup + batching logic can be unit
 * tested without spinning up jsdom, mocking window.fetch, or
 * stubbing requestIdleCallback. The module-level interactions.ts
 * uses these as its underlying state machine.
 *
 * No DOM dependencies: no window, no navigator, no fetch. The
 * functions take a "now" timestamp and a "schedule" callback as
 * inputs so the test can advance time + flush in lockstep with
 * the production code.
 */
import type { InteractionEvent, InteractionType } from './interactions_types'

/**
 * Cap matches the backend's _BATCH_MAX. The production queue
 * flushes when it reaches this size; tests verify the same.
 */
export const MAX_QUEUE = 50

/**
 * A pure-function view of the in-memory state: the buffer of
 * pending events, the set of entries already seen in this
 * session, and whether a flush is already scheduled.
 *
 * Designed for ``structuredClone``-friendly tests — no class
 * methods, no closures over module state. The production code
 * holds one instance; the test code creates fresh ones.
 */
export interface InteractionQueue {
  pending: InteractionEvent[]
  seen: Set<number>
  flushScheduled: boolean
}

export function newInteractionQueue(): InteractionQueue {
  return {
    pending: [],
    seen: new Set<number>(),
    flushScheduled: false,
  }
}

/**
 * Result of ``enqueue`` — tells the caller whether to schedule a
 * flush, and whether the queue hit the cap (which means the
 * caller should flush synchronously rather than waiting).
 */
export type EnqueueResult = 'added' | 'duplicate-view' | 'flush-immediately'

/**
 * Enqueue an event with the dedup + cap semantics from
 * interactions.ts. Pure: takes the queue by value, returns a
 * new queue. The caller is responsible for actually scheduling
 * the flush (the schedule callback) — this function only decides
 * *whether* a flush is needed.
 */
export function enqueue(
  queue: InteractionQueue,
  event: InteractionEvent,
): { queue: InteractionQueue; result: EnqueueResult } {
  // View dedup: only one ``view`` per entry per session.
  if (event.type === 'view') {
    if (queue.seen.has(event.entry_id)) {
      return { queue, result: 'duplicate-view' }
    }
    const nextSeen = new Set(queue.seen)
    nextSeen.add(event.entry_id)
    const next: InteractionQueue = {
      pending: [...queue.pending, event],
      seen: nextSeen,
      flushScheduled: queue.flushScheduled,
    }
    return {
      queue: next,
      result: next.pending.length >= MAX_QUEUE ? 'flush-immediately' : 'added',
    }
  }
  const next: InteractionQueue = {
    pending: [...queue.pending, event],
    seen: queue.seen,
    flushScheduled: queue.flushScheduled,
  }
  return {
    queue: next,
    result: next.pending.length >= MAX_QUEUE ? 'flush-immediately' : 'added',
  }
}

/**
 * Drain a batch from the queue. The caller passes the batch
 * size (typically ``queue.pending.length``); the function
 * returns the new queue with the first N events removed and the
 * batch as a separate array. Pure.
 */
export function drainBatch(
  queue: InteractionQueue,
  size?: number,
): { queue: InteractionQueue; batch: InteractionEvent[] } {
  const n = size ?? queue.pending.length
  const batch = queue.pending.slice(0, n)
  const next: InteractionQueue = {
    pending: queue.pending.slice(n),
    seen: queue.seen,
    flushScheduled: queue.flushScheduled,
  }
  return { queue: next, batch }
}

/**
 * Toggle the ``flushScheduled`` flag. Used by the scheduler —
 * the production code sets it to ``true`` on first enqueue, and
 * the flush handler resets it to ``false`` before sending. Test
 * the toggle is symmetrical: setting then unsetting returns the
 * queue to its original shape.
 */
export function setFlushScheduled(
  queue: InteractionQueue,
  scheduled: boolean,
): InteractionQueue {
  if (queue.flushScheduled === scheduled) return queue
  return {
    pending: queue.pending,
    seen: queue.seen,
    flushScheduled: scheduled,
  }
}

/**
 * Determine whether an event type triggers the per-session
 * dedup. Currently only ``view`` does — clicks, votes, and
 * bookmarks are all one-off. The production code uses this in
 * ``enqueue``; tests verify the boundary.
 */
export function isDeduplicatedType(type: InteractionType): boolean {
  return type === 'view'
}
