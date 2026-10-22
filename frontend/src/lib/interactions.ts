// Client-side buffering of engagement events.
//
// A ``view`` is recorded when a story is used up (PR3c, D3): opened
// (App's ``openEntry``, on any layout) or, on the phone, scrolled past
// (App's ``markSeen``), not when its card is drawn. A scroll can use up
// several at once, and one POST per ``view`` would hammer the backend.
// These helpers batch events into an in-memory queue and flush them on
// ``requestIdleCallback`` (with a setTimeout fallback for browsers that
// don't ship rIC — Safari before 16.4, Firefox before 126). On
// ``pagehide`` (visibilitychange hidden OR window unload) we switch to
// ``navigator.sendBeacon`` so the request survives the tab closing.
//
// Click events bypass the queue and fire immediately — they are
// sparse, the user expects immediate feedback (the link they're
// clicking should open), and one extra POST on a click is negligible.
//
// De-duplication: views are deduplicated by entry id via a
// Set<number>, so a story sends one view per session even if it is
// opened and also scrolled past, or seen on scroll again (a tab that
// starts over). The set is session-scoped — we deliberately don't
// persist it across page reloads; a return visit is a fresh "view"
// signal for the recommendation ranker.
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
const uninstalled = false

// When a story last opened from this page (Date.now()), 0 for never.
// The update check (useUpdateCheck.ts) leaves alone a page that hid
// right after opening one, so the story's card is still there when the
// user comes back to vote on or save it.
let storyOpenedAt = 0

// POSTs on the way (batches and immediate events), so the flush before
// a reload can let them land instead of cutting them off.
const inFlight = new Set<Promise<unknown>>()

function track(p: Promise<unknown>): void {
  inFlight.add(p)
  void p.finally(() => inFlight.delete(p))
}

// Cap matches the backend's _BATCH_MAX so a queued flush never 422s.
const MAX_QUEUE = 50

// Events held back until their Undo window closes (D25): a Hide's
// ``never`` waits while its toast offers Undo, so a hide taken back
// never reaches the ranking. ``holdInteraction`` adds one; its
// ``release`` sends it and ``cancel`` drops it. The page hiding sends
// whatever is still held (``onPageHide``), ahead of the queue: the
// toast's timer may never fire again, and a signal is better than none.
const held = new Set<InteractionEvent>()

/** Moves every held event to the front of the queue. */
function queueHeld(): void {
  if (held.size === 0) return
  queue.unshift(...held)
  held.clear()
}

// ---- flushing ----

async function flush(): Promise<void> {
  flushScheduled = false
  if (queue.length === 0) return
  // Drain atomically. Anything appended during the request goes into
  // a fresh buffer the next flush picks up.
  const batch = queue.splice(0, queue.length)
  const sent = api.recordInteractionBatch(batch).catch((err) => {
    // Best-effort; the events are advisory. Log in dev so a developer
    // debugging "why isn't the ranking changing" can see the network
    // issue but don't toast the user — engagement is in the background.
    if (import.meta.env.DEV) console.warn('interactions: batch flush failed', err)
  })
  track(sent)
  await sent
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
// silently caps body size to 64KB and returns ``false`` when the
// payload can't be queued (oversized, fatal error); we MUST NOT
// drain the queue before knowing the beacon succeeded — the
// fallback path then needs the events.
function onPageHide(): void {
  queueHeld()
  if (queue.length === 0) return
  // Drain into a local snapshot BEFORE attempting any send, so a
  // failed sendBeacon still has the events to fall back to. We swap
  // the queue with a fresh empty array rather than clearing in
  // place so any in-flight ``recordBatched`` between the splice and
  // the send lands in the new (now-abandoned) array. Those events
  // are lost on tab close anyway — the user is leaving.
  const batch = queue.splice(0, queue.length)
  try {
    const payload = JSON.stringify({ events: batch })
    const blob = new Blob([payload], { type: 'application/json' })
    if (navigator.sendBeacon?.('/api/interactions/batch', blob)) {
      return
    }
  } catch {
    // Fall through to the keepalive fetch below.
  }
  // sendBeacon failed or unavailable. ``keepalive: true`` lets the
  // browser finish an in-flight fetch even after the document
  // unloads — much better odds than a bare fetch().
  void fetch('/api/interactions/batch', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ events: batch }),
    keepalive: true,
  }).catch(() => {
    // Best-effort. We don't escalate this to a toast — the user is
    // already closing the tab.
  })
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
  // Every way a card opens its story records a ``click``.
  if (event.type === 'click') noteStoryOpened()
  // Drop in-page dedup for non-view events — a thumb_down click on
  // an already-voted card could legitimately mean "double down" in
  // a future UI even if it doesn't today.
  const sent = api
    .recordInteraction(event)
    .catch((err) => {
      if (import.meta.env.DEV)
        console.warn('interactions: immediate flush failed', err)
    })
  track(sent)
}

/** Holds ``event`` back until ``release`` sends it (the Undo toast
 * expired) or ``cancel`` drops it (Undo). Either is a no-op once the
 * other has run, or once the page hiding has sent it. A Hide's
 * ``never`` goes through here (D25). */
export function holdInteraction(event: InteractionEvent): { release: () => void; cancel: () => void } {
  held.add(event)
  return {
    release: () => {
      if (!held.delete(event)) return
      recordImmediate(event)
    },
    cancel: () => {
      held.delete(event)
    },
  }
}

/** For tests: number of events held back. */
export function heldCount(): number {
  return held.size
}

/** Records that a story opened. ``recordImmediate`` does it for every
 * ``click``; the paths that open a story without one (the alternatives
 * under a card, the context menu's "Open in new tab") call it. */
export function noteStoryOpened(): void {
  storyOpenedAt = Date.now()
}

/** When a story last opened from this page, 0 for never. */
export function lastStoryOpenAt(): number {
  return storyOpenedAt
}

/** Drain the queue right now without waiting for rIC (tests, dev
 * tools, and the flush before a reload onto a new build). Resolves when
 * that POST and every one already on the way have settled. A held
 * event goes too: the reload takes its Undo toast with it. */
export async function flushNow(): Promise<void> {
  queueHeld()
  await flush()
  await Promise.allSettled(Array.from(inFlight))
}

/** For tests: number of events currently buffered. */
export function pendingCount(): number {
  return queue.length
}
