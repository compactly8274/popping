// Client-side buffering of engagement events.
//
// The dashboard renders 20-50 visible cards at once; firing one
// POST per ``view`` would hammer the backend as the user scrolls.
// These helpers batch events into an in-memory queue and flush
// them on ``requestIdleCallback`` (with a setTimeout fallback for
// browsers that don't ship rIC — Safari before 16.4, Firefox before
// 126). On ``pagehide`` (visibilitychange hidden OR window unload)
// we switch to ``navigator.sendBeacon`` so the request survives the
// tab closing.
//
// Click events bypass the queue and fire immediately — they are
// sparse, the user expects immediate feedback (the link they're
// clicking should open), and one extra POST on a click is negligible.
//
// De-duplication: views are deduplicated by entry id via a
// Set<number> so re-renders (e.g. category filter toggles) don't
// fire duplicate events for cards already on screen. The set is
// session-scoped — we deliberately don't persist it across page
// reloads; a return visit is a fresh "view" signal for the
// recommendation ranker.
//
// Failure handling: if a flush POST fails (network blip, 5xx), we
// drop the events. Engagement signals are advisory; losing a few
// view events when the server is in a bad state is acceptable.

/// <reference types="vite/client" />

import { api } from '../api'

export type InteractionType =
  | 'view'
  | 'click'
  | 'dwell'
  | 'thumb_up'
  | 'thumb_down'
  | 'bookmark'
  | 'share'
  | 'never'

export interface InteractionEvent {
  entry_id: number
  type: InteractionType
  value?: number
}

// ---- module-level state ----

// Buffered events awaiting a flush. Synchronized only on the
// main thread, so a plain array is fine — no locking needed.
const queue: InteractionEvent[] = []

// Already-seen entries in this session. A Set fits both fast lookup
// and frequent insertion.
const seen = new Set<number>()

let flushScheduled = false
let uninstalled = false

// Cap matches the backend's _BATCH_MAX so a queued flush never 422s.
const MAX_QUEUE = 50

// ---- flushing ----

async function flush(): Promise<void> {
  flushScheduled = false
  if (queue.length === 0) return
  // Drain atomically. Anything appended during the request goes into
  // a fresh buffer the next flush picks up.
  const batch = queue.splice(0, queue.length)
  try {
    await api.recordInteractionBatch(batch)
  } catch (err) {
    // Best-effort; the events are advisory. Log in dev so a developer
    // debugging "why isn't the ranking changing" can see the network
    // issue but don't toast the user — engagement is in the background.
    if (import.meta.env.DEV) console.warn('interactions: batch flush failed', err)
  }
}

function scheduleFlush(): void {
  if (flushScheduled) return
  flushScheduled = true
  // Wait for a quiet period before flushing so a fast scroll past
  // 30 cards coalesces into one POST. ``requestIdleCallback`` honors
  // this; the setTimeout fallback uses a 250ms budget, which is short
  // enough to feel responsive but long enough to coalesce a render
  // burst.
  const ric = (window as unknown as {
    requestIdleCallback?: (
      cb: () => void,
      opts?: { timeout: number },
    ) => number
  }).requestIdleCallback
  if (typeof ric === 'function') {
    ric(() => void flush(), { timeout: 1000 })
  } else {
    window.setTimeout(() => void flush(), 250)
  }
}

// On page hide the user is leaving — anything still in the queue
// goes via sendBeacon so the request doesn't depend on the
// page staying alive long enough for a fetch to resolve. sendBeacon
// silently caps body size to 64KB; our events are tiny, so the only
// real risk is more than ~3000 events queued which can't happen
// (we cap at MAX_QUEUE per flush).
function onPageHide(): void {
  if (queue.length === 0) return
  const payload = JSON.stringify({ events: queue })
  queue.length = 0
  try {
    const blob = new Blob([payload], { type: 'application/json' })
    if (navigator.sendBeacon?.('/api/interactions/batch', blob)) {
      return
    }
  } catch {
    // Fall through to the best-effort flush below.
  }
  // sendBeacon unavailable (rare; very old browsers). Try the
  // normal POST anyway — most page-hide handlers DO get a chance to
  // complete a fetch. Best-effort.
  void api.recordInteractionBatch(queue)
}

if (typeof window !== 'undefined' && !uninstalled) {
  // The two events both fire on most modern browsers when a tab is
  // being closed or hidden. ``pagehide`` is the more modern one;
  // ``visibilitychange`` is the polyfill for older mobile browsers.
  window.addEventListener('pagehide', onPageHide)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') onPageHide()
  })
}

// ---- public API ----

/** Queue an event for the next batch flush. Used for ``view``
 * events where many fire in a short window and the synchronous
 * round-trip is wasted bandwidth. */
export function recordBatched(event: InteractionEvent): void {
  // View dedup — only one ``view`` per entry per session. Re-mounts
  // (theme switches, filter toggles) shouldn't multiply.
  if (event.type === 'view') {
    if (seen.has(event.entry_id)) return
    seen.add(event.entry_id)
  }
  queue.push(event)
  // Safety net: if the queue is full, flush synchronously rather than
  // growing without bound. Reaching this means the rIC / setTimeout
  // scheduling got starved (e.g. a long-running script on the page).
  if (queue.length >= MAX_QUEUE) {
    void flush()
    return
  }
  scheduleFlush()
}

/** Fire an event immediately, bypassing the batch queue. Used for
 * ``click`` (the user clicked a link; we want the server to know
 * synchronously) and for one-off preference changes (thumb_up /
 * thumb_down / never) where latency matters. */
export function recordImmediate(event: InteractionEvent): void {
  // Drop in-page dedup for non-view events — a thumb_down click on
  // an already-voted card could legitimately mean "double down" in
  // a future UI even if it doesn't today.
  void api
    .recordInteraction(event)
    .catch((err) => {
      if (import.meta.env.DEV)
        console.warn('interactions: immediate flush failed', err)
    })
}

/** For tests / dev tools: drain the queue right now without waiting
 * for rIC. Returns a promise that resolves when the in-flight POST
 * settles. */
export function flushNow(): Promise<void> {
  return flush()
}

/** For tests: number of events currently buffered. */
export function pendingCount(): number {
  return queue.length
}
