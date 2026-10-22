// The save store behind the feed editor: every change to a feed is
// one PATCH with one key, sent in order per feed, without a Save
// button. No imports (see feedRules.ts); App passes in the API call
// and its state through configureSourceSaves.
//
// Why no debounce timer: every PATCH on an active feed rebuilds its
// scheduler job, which fetches the feed at once. Text fields commit
// on blur or Return, selects on change, so a half-typed address is
// never saved.
//
// Per feed: a queue of fields not yet sent (a newer value for a
// queued field replaces it), at most one request in flight (while the
// page is hiding, everything goes at once with keepalive), and one
// network retry after 2 s.

export type Field =
  | 'name'
  | 'display_name'
  | 'url'
  | 'category'
  | 'refresh_interval_seconds'
  | 'active'
  | 'custom_headers'
  | 'sitemap_url'
  | 'link_pattern'

export type SaveState = Readonly<{
  status: 'idle' | 'saving' | 'slow' | 'saved' | 'error'
  pending?: unknown
  failedValue?: unknown
  message?: string
  retryable?: boolean
}>

export type SourceRow = { id: number; name: string }

type Deps = {
  patch: (id: number, body: Record<string, unknown>, opts: { keepalive: boolean; signal: AbortSignal }) => Promise<SourceRow>
  getSource: (id: number) => SourceRow | undefined
  onServerRow: (row: SourceRow) => void
  onRenamed: (oldName: string, newName: string) => void
  // 404: the feed was deleted elsewhere.
  onGone: (id: number) => void
  // Saves that failed after their editor closed, one call per feed.
  onFailedAway: (id: number, failures: Array<{ label: string; message: string }>) => void
  describe: (err: unknown, field: Field, value: unknown) => { message: string; retryable: boolean }
  equal: (field: Field, a: unknown, b: unknown) => boolean
}

const TIMEOUT_MS = 35_000
const SLOW_MS = 8_000
const RETRY_MS = 2_000
const SAVED_MS = 1_500

const IDLE: SaveState = Object.freeze({ status: 'idle' })

type Item = { field: Field; value: unknown; label: string; retried: boolean }

type Feed = {
  queue: Map<Field, Item>
  inFlight: Set<Item>
  retrying: Item | null
  retryTimer: ReturnType<typeof setTimeout> | null
  lastRejected: Map<Field, { value: unknown; message: string }>
  states: Map<Field, SaveState>
  labels: Map<Field, string>
  // Bumped by dropSource, so results for a dropped feed are ignored.
  generation: number
  idleWaiters: Array<() => void>
}

let deps: Deps | null = null
let pageHiding = false
const feeds = new Map<number, Feed>()
const subscribers = new Map<string, Set<() => void>>()
const flushers = new Set<() => void>()
const failedAway = new Map<number, Array<{ label: string; message: string }>>()
let failedAwayTimer: ReturnType<typeof setTimeout> | null = null

export function configureSourceSaves(d: Deps): void {
  deps = d
}

function feedFor(id: number): Feed {
  let f = feeds.get(id)
  if (!f) {
    f = {
      queue: new Map(),
      inFlight: new Set(),
      retrying: null,
      retryTimer: null,
      lastRejected: new Map(),
      states: new Map(),
      labels: new Map(),
      generation: 0,
      idleWaiters: [],
    }
    feeds.set(id, f)
  }
  return f
}

const keyOf = (id: number, field: Field) => `${id}:${field}`

function setState(id: number, field: Field, state: SaveState): void {
  const f = feedFor(id)
  if (f.states.get(field) === state) return
  if (state === IDLE) f.states.delete(field)
  else f.states.set(field, Object.freeze({ ...state }))
  subscribers.get(keyOf(id, field))?.forEach((cb) => cb())
}

function isIdle(f: Feed): boolean {
  return f.queue.size === 0 && f.inFlight.size === 0 && !f.retrying
}

function settleWaiters(f: Feed): void {
  if (!isIdle(f)) return
  const waiters = f.idleWaiters
  f.idleWaiters = []
  waiters.forEach((w) => w())
}

// The newest value this store holds for a field that the server
// hasn't confirmed yet: queued, then in flight, then waiting to retry.
function latestUnconfirmed(f: Feed, field: Field): { value: unknown } | null {
  const queued = f.queue.get(field)
  if (queued) return { value: queued.value }
  let flying: Item | null = null
  for (const item of f.inFlight) if (item.field === field) flying = item
  if (flying) return { value: flying.value }
  if (f.retrying?.field === field) return { value: f.retrying.value }
  return null
}

function serverValue(id: number, field: Field): unknown {
  const row = deps?.getSource(id) as Record<string, unknown> | undefined
  return row?.[field]
}

export function commitField(id: number, field: Field, value: unknown, label: string): void {
  if (!deps) return
  const f = feedFor(id)
  const unconfirmed = latestUnconfirmed(f, field)
  const compareTo = unconfirmed ? unconfirmed.value : serverValue(id, field)
  if (deps.equal(field, value, compareTo)) {
    // Nothing to send. A stale error clears once the field is back to
    // what the server has.
    if (!unconfirmed && f.states.get(field)?.status === 'error') setState(id, field, IDLE)
    return
  }
  const rejected = f.lastRejected.get(field)
  if (rejected && deps.equal(field, value, rejected.value)) {
    // The server already refused exactly this; say so again.
    setState(id, field, { status: 'error', message: rejected.message, failedValue: value, retryable: false })
    return
  }
  enqueue(id, f, { field, value, label, retried: false })
}

function enqueue(id: number, f: Feed, item: Item): void {
  const queued = f.queue.get(item.field)
  // Newest wins for a field that hasn't started. This also absorbs an
  // iOS select that fires change more than once.
  if (queued) queued.value = item.value
  else f.queue.set(item.field, item)
  f.labels.set(item.field, item.label)
  setState(id, item.field, { status: 'saving', pending: item.value })
  pump(id)
}

/** Send the failed value of a field again, even if the server refused
 *  it before (Try again). */
export function retryField(id: number, field: Field): void {
  const f = feedFor(id)
  const state = f.states.get(field)
  if (!state || state.status !== 'error') return
  f.lastRejected.delete(field)
  enqueue(id, f, { field, value: state.failedValue, label: f.labels.get(field) ?? field, retried: false })
}

export function clearError(id: number, field: Field): void {
  if (feedFor(id).states.get(field)?.status === 'error') setState(id, field, IDLE)
}

export function getSaveState(id: number, field: Field): SaveState {
  return feeds.get(id)?.states.get(field) ?? IDLE
}

export function subscribe(id: number, field: Field, cb: () => void): () => void {
  const key = keyOf(id, field)
  let set = subscribers.get(key)
  if (!set) subscribers.set(key, (set = new Set()))
  set.add(cb)
  return () => {
    set!.delete(cb)
    if (!set!.size) subscribers.delete(key)
  }
}

export function currentSource(id: number): SourceRow | undefined {
  return deps?.getSource(id)
}

/** Resolves once nothing for this feed is queued, in flight or
 *  waiting to retry. */
export function whenIdle(id: number): Promise<void> {
  const f = feeds.get(id)
  if (!f || isIdle(f)) return Promise.resolve()
  return new Promise((resolve) => f.idleWaiters.push(resolve))
}

/** The same for every feed: before a reload onto a new build (PR1r),
 *  so the reload doesn't cut a save off. */
export function whenAllIdle(): Promise<void> {
  return Promise.all(Array.from(feeds.keys(), whenIdle)).then(() => undefined)
}

/** Forget a deleted feed: nothing more is sent for it, and results
 *  still on the way are ignored. Its last states stay readable. */
export function dropSource(id: number): void {
  const f = feeds.get(id)
  if (!f) return
  f.generation++
  if (f.retryTimer) clearTimeout(f.retryTimer)
  f.retryTimer = null
  f.retrying = null
  f.queue = new Map()
  f.inFlight = new Set()
  settleWaiters(f)
}

/** Called from visibilitychange and pagehide. While true, everything
 *  queued goes out at once with keepalive, so iOS suspending the page
 *  doesn't strand an edit. */
export function setPageHiding(hiding: boolean): void {
  pageHiding = hiding
  if (!hiding) return
  for (const [id, f] of feeds) {
    // A retry timer won't run while the page is suspended; send the
    // retry now instead.
    if (f.retrying) {
      if (f.retryTimer) clearTimeout(f.retryTimer)
      const item = f.retrying
      f.retrying = null
      f.retryTimer = null
      if (!f.queue.has(item.field)) f.queue = new Map([[item.field, item], ...f.queue])
    }
    pump(id)
  }
}

export function registerFlusher(fn: () => void): () => void {
  flushers.add(fn)
  return () => {
    flushers.delete(fn)
  }
}

/** Ask every open editor to commit its dirty, valid text drafts. */
export function flushEditors(): void {
  for (const fn of Array.from(flushers)) fn()
}

function pump(id: number): void {
  const f = feeds.get(id)
  if (!f || !deps) return
  if (pageHiding) {
    const items = Array.from(f.queue.values())
    f.queue = new Map()
    items.forEach((item) => send(id, f, item))
    return
  }
  if (f.inFlight.size || f.retrying) return
  const next = f.queue.values().next()
  if (next.done) {
    settleWaiters(f)
    return
  }
  f.queue.delete(next.value.field)
  send(id, f, next.value)
}

function hasNewer(f: Feed, item: Item): boolean {
  if (f.queue.has(item.field) || f.retrying?.field === item.field) return true
  for (const other of f.inFlight) if (other !== item && other.field === item.field) return true
  return false
}

function send(id: number, f: Feed, item: Item): void {
  const d = deps!
  const generation = f.generation
  const controller = new AbortController()
  const abortTimer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  const slowTimer = setTimeout(() => {
    const s = f.states.get(item.field)
    if (s?.status === 'saving' && s.pending === item.value) setState(id, item.field, { status: 'slow', pending: item.value })
  }, SLOW_MS)
  f.inFlight.add(item)

  let request: Promise<SourceRow>
  try {
    request = d.patch(id, { [item.field]: item.value }, { keepalive: pageHiding, signal: controller.signal })
  } catch (err) {
    request = Promise.reject(err)
  }
  request
    .then(
      (row) => {
        if (generation !== f.generation) return
        const prev = d.getSource(id)
        d.onServerRow(row)
        if (prev && prev.name !== row.name) d.onRenamed(prev.name, row.name)
        f.lastRejected.delete(item.field)
        if (!hasNewer(f, item)) {
          setState(id, item.field, { status: 'saved' })
          const saved = f.states.get(item.field)
          setTimeout(() => {
            if (f.states.get(item.field) === saved) setState(id, item.field, IDLE)
          }, SAVED_MS)
        }
      },
      (err) => {
        if (generation !== f.generation) return
        onFailure(id, f, item, err)
      },
    )
    .finally(() => {
      clearTimeout(abortTimer)
      clearTimeout(slowTimer)
      f.inFlight.delete(item)
      if (generation !== f.generation) return
      pump(id)
      settleWaiters(f)
    })
}

function onFailure(id: number, f: Feed, item: Item, err: unknown): void {
  const d = deps!
  const name = (err as { name?: unknown } | null)?.name
  const status = (err as { status?: unknown } | null)?.status
  if (name === 'TypeError' && !item.retried) {
    item.retried = true
    // A newer value for the field is already queued: send that instead.
    if (f.queue.has(item.field)) return
    if (pageHiding) {
      f.queue = new Map([[item.field, item], ...f.queue])
      return
    }
    f.retrying = item
    f.retryTimer = setTimeout(() => {
      f.retrying = null
      f.retryTimer = null
      if (!f.queue.has(item.field)) f.queue = new Map([[item.field, item], ...f.queue])
      pump(id)
    }, RETRY_MS)
    return
  }
  if (status === 404) {
    d.onGone(id)
    dropSource(id)
    setState(id, item.field, { status: 'error', message: 'This feed was deleted.', failedValue: item.value, retryable: false })
    return
  }
  const { message, retryable } = d.describe(err, item.field, item.value)
  if (typeof status === 'number' && status >= 400 && status < 500) {
    f.lastRejected.set(item.field, { value: item.value, message })
  }
  if (hasNewer(f, item)) return
  setState(id, item.field, { status: 'error', message, failedValue: item.value, retryable })
  if (!subscribers.has(keyOf(id, item.field))) {
    // The editor is gone. Toast.tsx shows one toast at a time, so
    // gather this tick's failures into one call per feed. The value
    // and message stay here for when the editor is reopened.
    const list = failedAway.get(id) ?? []
    list.push({ label: item.label, message })
    failedAway.set(id, list)
    if (!failedAwayTimer) {
      failedAwayTimer = setTimeout(() => {
        failedAwayTimer = null
        const batches = Array.from(failedAway)
        failedAway.clear()
        for (const [fid, failures] of batches) deps?.onFailedAway(fid, failures)
      }, 0)
    }
  }
}
