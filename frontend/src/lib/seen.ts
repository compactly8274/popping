// Seen on scroll (PR3c, D3): on the phone, a story is used up once you
// have had it on screen and scrolled past it. No imports and no DOM (see
// feedRules.ts): the timers and the page's state come in as functions, and
// the observer and its entries are described by the little of them read
// here, so the unit tests run it under strip-types.
//
// - A card qualifies once it has been at least half visible for
//   ``SEEN_DWELL_MS`` straight. Half of the card, or half of the list
//   when the card is taller than it, so a tall card counts too.
// - A qualified card is seen when it leaves through the top of the list.
//   Leaving through the bottom doesn't count, and neither does a card
//   flung past without ever qualifying.
// - The end of the tab: once the marker ("N not seen · Mark as read")
//   has been half visible for ``SEEN_DWELL_MS``, every qualified card on
//   screen is seen, and so is each card that qualifies while the marker
//   stays on screen. Nothing is scrolled past the last cards.
// - A dwell timer that fires while the page is hidden or one of App's
//   sheets is open starts again instead: a card behind the Menu or under
//   Safari wasn't being read. App also says when the list is covered and
//   uncovered (``setCovered``): every dwell under way stops, and starts
//   over once uncovered. iOS freezes a hidden page's timers and fires
//   them late, just after it is back, so a timer left running across a
//   hide would qualify a card that was on screen for a moment before the
//   user left.
//
// The registry of cards outlives any one observer: App's observer is torn
// down when the list goes (a tab switch, or the phone turned past 768px,
// where the phone layout is hidden and every rect would read 0) and a new
// one observes every card already registered, so no card has to remount.

/** On screen at least this long, at least half visible, to qualify. The
 *  owner tunes it at Gate 1 (too eager: 1500; too slow: 700). */
export const SEEN_DWELL_MS = 1000
/** How long scroll-seen stories are collected before they are written:
 *  one ``addIds`` per ``seen:<category>`` key, one render per batch. */
export const SEEN_BATCH_MS = 300
/** The observer's thresholds: every tenth, so a card crossing half
 *  visible is reported whichever way it moves. */
export const SEEN_THRESHOLDS: readonly number[] = Array.from({ length: 11 }, (_, i) => i / 10)

/** What the tracker needs of an element: whether it is in the document. */
export type SeenTarget = { readonly isConnected: boolean }

/** What the tracker reads of an ``IntersectionObserverEntry``. */
export type Sighting<T extends SeenTarget> = {
  readonly target: T
  readonly isIntersecting: boolean
  readonly intersectionRect: { readonly height: number }
  readonly boundingClientRect: { readonly bottom: number; readonly height: number }
  readonly rootBounds: { readonly top: number; readonly height: number } | null
}

/** The calls the tracker makes on an ``IntersectionObserver``. */
export type SeenObserver<T extends SeenTarget> = {
  observe(target: T): void
  unobserve(target: T): void
  disconnect(): void
  takeRecords(): readonly Sighting<T>[]
}

export type TrackerDeps = {
  setTimer: (fn: () => void, ms: number) => unknown
  clearTimer: (timer: unknown) => void
  /** The page is hidden (``document.visibilityState``). */
  pageHidden: () => boolean
  /** One of App's sheets is open. App's own boolean, never a selector:
   *  the closed Drawer is always mounted with ``aria-modal``. */
  modalOpen: () => boolean
  /** Cards just seen, by id: once per observer callback (or end rule),
   *  each card at most once while an observer is attached. */
  onSeen: (ids: number[]) => void
  dwellMs?: number
}

/** Where the tracker learns about cards and the marker (App hands it to
 *  the phone Column). Each call that detaches passes the element going
 *  away, so a late detach can't drop the element that replaced it. */
export type SeenRegistry<T extends SeenTarget> = {
  /** A card attached (``el``) or detached (``null``, with ``prev``). */
  card: (id: number, el: T | null, prev?: T | null) => void
  /** The marker attached, or detached. */
  marker: (el: T | null, prev?: T | null) => void
}

// One card's state under the attached observer: its dwell timer, and
// where its last entry put it (at least half visible, on screen at all).
type Dwell = { timer: unknown; qualified: boolean; half: boolean; onScreen: boolean; seen: boolean }

/** Is the entry's target at least half visible (of itself, or of the
 *  list when it is taller)? Only with real rects: a detached or
 *  ``display: none`` target reports all zeros. */
function halfVisible<T extends SeenTarget>(e: Sighting<T>): boolean {
  const root = e.rootBounds
  if (!e.isIntersecting || !e.target.isConnected || !root || root.height <= 0 || e.boundingClientRect.height <= 0) return false
  return e.intersectionRect.height >= 0.5 * Math.min(e.boundingClientRect.height, root.height)
}

/** Has the entry's target left through the top of the list? */
function leftThroughTop<T extends SeenTarget>(e: Sighting<T>): boolean {
  const root = e.rootBounds
  if (e.isIntersecting || !e.target.isConnected || !root || root.height <= 0 || e.boundingClientRect.height <= 0) return false
  return e.boundingClientRect.bottom <= root.top
}

export function createSeenTracker<T extends SeenTarget>(deps: TrackerDeps) {
  const dwellMs = deps.dwellMs ?? SEEN_DWELL_MS
  const paused = () => deps.pageHidden() || deps.modalOpen()
  // The registry: outlives observers.
  const cards = new Map<number, T>()
  const ids = new Map<T, number>()
  let marker: T | null = null
  // The attached observer and what it has seen.
  let observer: SeenObserver<T> | null = null
  const dwell = new Map<number, Dwell>()
  let markerTimer: unknown = null
  let markerHalf = false
  // The marker has been half visible for the dwell: the end rule is on.
  let endOn = false
  // The list is covered (the page hidden, or a sheet open): no dwell runs.
  let covered = false

  const clearDwell = (id: number) => {
    const d = dwell.get(id)
    if (d && d.timer != null) deps.clearTimer(d.timer)
    dwell.delete(id)
  }
  const stopMarker = () => {
    if (markerTimer != null) deps.clearTimer(markerTimer)
    markerTimer = null
    markerHalf = false
    endOn = false
  }

  // Seen: once per card while this observer is attached.
  const see = (id: number, d: Dwell, out: number[]) => {
    if (d.seen) return
    d.seen = true
    if (d.timer != null) deps.clearTimer(d.timer)
    d.timer = null
    out.push(id)
  }
  const emit = (out: number[]) => {
    if (out.length) deps.onSeen(out)
  }

  const startDwell = (id: number, d: Dwell) => {
    const fire = () => {
      if (dwell.get(id) !== d) return
      if (paused()) {
        d.timer = deps.setTimer(fire, dwellMs)
        return
      }
      d.timer = null
      d.qualified = true
      const out: number[] = []
      if (endOn && d.onScreen) see(id, d, out)
      emit(out)
    }
    d.timer = deps.setTimer(fire, dwellMs)
  }

  const startMarker = () => {
    const fire = () => {
      if (paused()) {
        markerTimer = deps.setTimer(fire, dwellMs)
        return
      }
      markerTimer = null
      endOn = true
      const out: number[] = []
      for (const [id, d] of dwell) if (d.qualified && d.onScreen) see(id, d, out)
      emit(out)
    }
    markerTimer = deps.setTimer(fire, dwellMs)
  }

  const onCard = (id: number, e: Sighting<T>, out: number[]) => {
    let d = dwell.get(id)
    if (!d) {
      d = { timer: null, qualified: false, half: false, onScreen: false, seen: false }
      dwell.set(id, d)
    }
    if (d.seen) return
    d.onScreen = e.isIntersecting && e.target.isConnected
    d.half = halfVisible(e)
    if (leftThroughTop(e)) {
      if (d.qualified) see(id, d, out)
      else if (d.timer != null) {
        deps.clearTimer(d.timer)
        d.timer = null
      }
      return
    }
    if (d.qualified) {
      // Back on screen while the end rule is on.
      if (endOn && d.onScreen) see(id, d, out)
      return
    }
    if (d.half) {
      if (d.timer == null && !covered) startDwell(id, d)
      return
    }
    // Under half: the dwell has to start over.
    if (d.timer != null) {
      deps.clearTimer(d.timer)
      d.timer = null
    }
  }

  const onMarker = (e: Sighting<T>) => {
    if (halfVisible(e)) {
      markerHalf = true
      if (!endOn && markerTimer == null && !covered) startMarker()
    } else {
      stopMarker()
    }
  }

  /** The list covered (the page hidden, or one of App's sheets open) or
   *  uncovered. Covered, every dwell under way stops; uncovered, each card
   *  (and the marker) still at least half on screen starts its dwell
   *  over. A card already qualified stays qualified. */
  const setCovered = (on: boolean) => {
    if (on === covered) return
    covered = on
    if (on) {
      for (const d of dwell.values()) {
        if (d.timer != null) deps.clearTimer(d.timer)
        d.timer = null
      }
      if (markerTimer != null) deps.clearTimer(markerTimer)
      markerTimer = null
      return
    }
    for (const [id, d] of dwell) if (d.half && !d.qualified && !d.seen && d.timer == null) startDwell(id, d)
    if (markerHalf && !endOn && markerTimer == null) startMarker()
  }

  /** The observer's entries (its callback). Entries from an observer that
   *  is no longer attached are dropped: one can still deliver what it had
   *  queued after ``disconnect``. */
  const handle = (entries: readonly Sighting<T>[], from: SeenObserver<T>) => {
    if (from !== observer) return
    const out: number[] = []
    for (const e of entries) {
      if (e.target === marker) {
        onMarker(e)
        continue
      }
      const id = ids.get(e.target)
      if (id != null && cards.get(id) === e.target) onCard(id, e, out)
    }
    emit(out)
  }

  const card = (id: number, el: T | null, prev?: T | null) => {
    const had = cards.get(id)
    if (el) {
      if (had === el) return
      if (had) {
        ids.delete(had)
        observer?.unobserve(had)
      }
      clearDwell(id)
      cards.set(id, el)
      ids.set(el, id)
      observer?.observe(el)
      return
    }
    if (!had || (prev != null && had !== prev)) return
    cards.delete(id)
    ids.delete(had)
    clearDwell(id)
    observer?.unobserve(had)
  }

  const setMarker = (el: T | null, prev?: T | null) => {
    if (el) {
      if (marker === el) return
      if (marker) observer?.unobserve(marker)
      stopMarker()
      marker = el
      observer?.observe(el)
      return
    }
    if (!marker || (prev != null && marker !== prev)) return
    observer?.unobserve(marker)
    stopMarker()
    marker = null
  }

  /** A new observer (a new list, or back under 768px): it observes every
   *  card registered and the marker. */
  const attach = (obs: SeenObserver<T>) => {
    if (observer) detach(observer)
    observer = obs
    for (const el of cards.values()) obs.observe(el)
    if (marker) obs.observe(marker)
  }

  /** The observer goes: disconnected, every timer cleared and every
   *  card's dwell forgotten. The registry stays. */
  const detach = (obs: SeenObserver<T>) => {
    if (observer !== obs) return
    obs.disconnect()
    observer = null
    for (const id of Array.from(dwell.keys())) clearDwell(id)
    stopMarker()
  }

  /** Handle what the observer has queued and not yet delivered, now (the
   *  page is hiding: a card that just left the top must be in the
   *  keepalive write). */
  const takeRecords = () => {
    if (observer) handle(observer.takeRecords(), observer)
  }

  const registry: SeenRegistry<T> = { card, marker: setMarker }
  return {
    registry,
    attach,
    detach,
    handle,
    takeRecords,
    setCovered,
    /** For tests: the elements registered, by id. */
    registered: () => new Map(cards),
  }
}

export type BatchDeps<T> = {
  setTimer: (fn: () => void, ms: number) => unknown
  clearTimer: (timer: unknown) => void
  /** Everything collected, once per batch, in the order it came. */
  drain: (items: T[]) => void
  key: (item: T) => number
  delayMs?: number
}

/** Collects scroll-seen stories for ``SEEN_BATCH_MS`` from the first
 *  one, then hands them on in one call. ``flush`` hands them on now (the
 *  page is hiding, or Mark all is about to read the marks). ``has``
 *  says whether a story is still waiting here: seen, its mark not
 *  written yet (a snapshot started meanwhile puts it under the marker). */
export function createSeenBatch<T>(deps: BatchDeps<T>) {
  const delay = deps.delayMs ?? SEEN_BATCH_MS
  let items = new Map<number, T>()
  let timer: unknown = null
  const flush = () => {
    if (timer != null) deps.clearTimer(timer)
    timer = null
    if (!items.size) return
    const out = Array.from(items.values())
    items = new Map()
    deps.drain(out)
  }
  const add = (list: readonly T[]) => {
    for (const item of list) {
      const k = deps.key(item)
      if (!items.has(k)) items.set(k, item)
    }
    if (items.size && timer == null) timer = deps.setTimer(flush, delay)
  }
  return { add, flush, has: (key: number) => items.has(key), size: () => items.size }
}
