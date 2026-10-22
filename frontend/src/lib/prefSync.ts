// Preferences sync by change, not by overwrite (PR2b). No imports, no
// React and no DOM (see feedRules.ts): the provider (preferences.tsx)
// hands in the requests, the clock, the timers and the page's
// visibility, so the unit tests run every rule under strip-types.
//
// Set keys (saves, hides, votes, read and seen marks) are written as
// changes: PATCH ``{add, remove}``, or ``{set}`` for votes. The server
// applies a change under a row lock (routes/preferences.py), so two
// devices, or two taps in one tick, both land; a whole-list PUT would
// let the later list drop the earlier one's change. The other keys (a
// tab's sort, its section folds, saved views) are small whole values
// and stay PUT.
//
// What the screen shows for a key is the server's value (``base``: the
// last one adopted, from a GET or from a write's response) with every
// write not yet acknowledged applied on top, oldest first: those given
// up on (sync rule 4) until the next answer about the key, those on the
// way, then the one still queued. A GET can be older than a write the
// server already took, so a GET's row is ignored for a key that has a
// write on the way, or one that settled after the GET was sent (request
// sequence numbers, not clocks).

// ---------------------------------------------------------------------------
// Keys and caps.
// ---------------------------------------------------------------------------

// The most each set key holds. Pinned against the server's
// ``SET_KEY_CAPS`` by backend/tests/test_preferences_delta.py, which reads
// this file. The server trims to the same caps (smallest id first) as a
// backstop; the client prunes first (``pruneIds``) and sends what it
// drops in the same PATCH.
export const MAX_HIDDEN = 1000
export const MAX_STARRED = 1000
// Votes accumulate one story at a time, so they get more headroom than
// saves and hides (deliberate actions).
export const MAX_VOTED = 2000
// Each ``read_entries:<column>`` and ``seen:<category>`` key.
export const MAX_READ_PER_KEY = 1000

export const VOTES_KEY = 'voted_entries'
const SUFFIXED_SET_KEYS = ['read_entries:', 'seen:']

/** A key whose value is a set of story ids (or, for ``voted_entries``,
 *  a map of story id to vote): it is written by PATCH. */
export function isSetKey(key: string): boolean {
  if (key === 'starred_entries' || key === 'hidden_entries' || key === VOTES_KEY) return true
  return SUFFIXED_SET_KEYS.some((p) => key.startsWith(p) && key.length > p.length)
}

export function isVoteKey(key: string): boolean {
  return key === VOTES_KEY
}

/** The cap of a set key, null for any other key. */
export function capFor(key: string): number | null {
  if (key === 'starred_entries') return MAX_STARRED
  if (key === 'hidden_entries') return MAX_HIDDEN
  if (key === VOTES_KEY) return MAX_VOTED
  return isSetKey(key) ? MAX_READ_PER_KEY : null
}

// ---------------------------------------------------------------------------
// Values and changes.
// ---------------------------------------------------------------------------

export type Vote = 'up' | 'down'
export type VoteMap = Record<string, Vote>
/** A PATCH body for an id-set key. Each id is in one list at most. */
export type IdDelta = { add: number[]; remove: number[] }
/** A PATCH body for ``voted_entries``: ``null`` clears a vote. */
export type VoteDelta = { set: Record<string, Vote | null> }
export type Delta = IdDelta | VoteDelta
/** One write to a key: a change (set keys), or a whole value or a
 *  delete (the other keys). */
export type Write = { op: 'patch'; delta: Delta } | { op: 'put'; value: unknown } | { op: 'delete' }
/** A row as the server sends it. */
export type PrefRow = { key: string; value: unknown; updated_at: string }
/** Key -> value. Set keys hold clean values (``coerceIds``,
 *  ``coerceVotes``); the others hold what the server sent. */
export type Rows = Readonly<Record<string, unknown>>

const isId = (x: unknown): x is number => typeof x === 'number' && Number.isSafeInteger(x) && x > 0
const VOTE_ID_RE = /^[1-9][0-9]*$/
const isPlainObject = (x: unknown): x is Record<string, unknown> =>
  !!x && typeof x === 'object' && !Array.isArray(x)

/** A stored id list as clean ids: positive integers, each once, in
 *  stored order. Anything that isn't a list is empty. The same array
 *  when it was already clean. */
export function coerceIds(value: unknown): number[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<number>()
  let clean = true
  for (const x of value) {
    if (!isId(x) || seen.has(x)) {
      clean = false
      break
    }
    seen.add(x)
  }
  if (clean) return value as number[]
  seen.clear()
  const out: number[] = []
  for (const x of value) {
    if (isId(x) && !seen.has(x)) {
      seen.add(x)
      out.push(x)
    }
  }
  return out
}

/** Stored votes as ``{"<id>": "up" | "down"}``; the same object when it
 *  was already clean. */
export function coerceVotes(value: unknown): VoteMap {
  if (!isPlainObject(value)) return {}
  const entries = Object.entries(value)
  if (entries.every(([k, v]) => VOTE_ID_RE.test(k) && (v === 'up' || v === 'down'))) return value as VoteMap
  const out: VoteMap = {}
  for (const [k, v] of entries) if (VOTE_ID_RE.test(k) && (v === 'up' || v === 'down')) out[k] = v
  return out
}

/** A server value as the screen keeps it: set keys cleaned, the rest as
 *  sent. */
export function normalizeValue(key: string, value: unknown): unknown {
  if (!isSetKey(key)) return value
  return isVoteKey(key) ? coerceVotes(value) : coerceIds(value)
}

/** Deep equality for JSON values. */
export function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (Array.isArray(a)) {
    if (!Array.isArray(b) || a.length !== b.length) return false
    return a.every((x, i) => sameValue(x, b[i]))
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a)
    if (ka.length !== Object.keys(b).length) return false
    return ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && sameValue(a[k], b[k]))
  }
  return false
}

/** What turns ``prev`` into ``next``, as a change. */
export function diffIds(prev: readonly number[], next: readonly number[]): IdDelta {
  const before = new Set(prev)
  const after = new Set(next)
  return {
    add: next.filter((id) => !before.has(id)),
    remove: prev.filter((id) => !after.has(id)),
  }
}

const isVoteDelta = (d: Delta): d is VoteDelta => 'set' in d

/** ``older`` then ``newer`` as one change; for each id the later op wins.
 *  Within one change ``remove`` applies before ``add``, as on the
 *  server. */
export function foldDelta(older: Delta, newer: Delta): Delta {
  if (isVoteDelta(older) || isVoteDelta(newer)) {
    return { set: { ...(isVoteDelta(older) ? older.set : {}), ...(isVoteDelta(newer) ? newer.set : {}) } }
  }
  const ops = new Map<number, 'add' | 'remove'>()
  const op = (id: number, o: 'add' | 'remove') => {
    ops.delete(id)
    ops.set(id, o)
  }
  for (const d of [older, newer]) {
    for (const id of d.remove) op(id, 'remove')
    for (const id of d.add) op(id, 'add')
  }
  const out: IdDelta = { add: [], remove: [] }
  for (const [id, o] of ops) out[o].push(id)
  return out
}

const isEmptyDelta = (d: Delta): boolean =>
  isVoteDelta(d) ? Object.keys(d.set).length === 0 : d.add.length === 0 && d.remove.length === 0

/** ``ids`` with ``delta`` applied: ``remove`` first, then ``add`` (new
 *  ids at the end). The same array when nothing changed. */
export function applyIdDelta(ids: readonly number[], delta: IdDelta): number[] {
  let out = ids as number[]
  if (delta.remove.length) {
    const rm = new Set(delta.remove)
    const kept = out.filter((id) => !rm.has(id))
    if (kept.length !== out.length) out = kept
  }
  let present: Set<number> | null = null
  for (const id of delta.add) {
    present ??= new Set(out)
    if (present.has(id)) continue
    if (out === ids) out = [...ids]
    out.push(id)
    present.add(id)
  }
  return out
}

/** ``votes`` with ``delta`` applied (``null`` clears). The same object
 *  when nothing changed. */
export function applyVoteDelta(votes: VoteMap, delta: VoteDelta): VoteMap {
  let out = votes
  for (const [id, v] of Object.entries(delta.set)) {
    if (v == null ? !(id in out) : out[id] === v) continue
    if (out === votes) out = { ...votes }
    if (v == null) delete out[id]
    else out[id] = v
  }
  return out
}

/** ``ids`` cut down to ``cap``: ids absent from ``keep`` go first, then
 *  the rest, smallest first (story ids grow with time, so the oldest
 *  stories go). ``dropped`` is what went, for the PATCH's ``remove``. */
export function pruneIds(
  ids: readonly number[],
  keep: Iterable<number>,
  cap: number,
): { ids: number[]; dropped: number[] } {
  const over = ids.length - cap
  if (over <= 0) return { ids: ids as number[], dropped: [] }
  const keepSet = new Set(keep)
  const asc = (a: number, b: number) => a - b
  const order = [...ids.filter((id) => !keepSet.has(id)).sort(asc), ...ids.filter((id) => keepSet.has(id)).sort(asc)]
  const drop = new Set(order.slice(0, over))
  return { ids: ids.filter((id) => !drop.has(id)), dropped: ids.filter((id) => drop.has(id)) }
}

/** ``base`` (undefined: no row) with ``writes`` applied in order. For a
 *  set key with no row and no write, undefined. */
export function applyWrites(key: string, base: unknown, writes: readonly Write[]): unknown {
  let v = base
  for (const w of writes) {
    if (w.op === 'put') v = w.value
    else if (w.op === 'delete') v = undefined
    else if (isVoteDelta(w.delta)) v = applyVoteDelta(coerceVotes(v), w.delta)
    else v = applyIdDelta(coerceIds(v), w.delta)
  }
  return v
}

/** ``older`` then ``newer`` to one key as one write: two changes fold
 *  (``foldDelta``); a whole value or a delete replaces what came before. */
export function foldWrite(older: Write, newer: Write): Write {
  return older.op === 'patch' && newer.op === 'patch' ? { op: 'patch', delta: foldDelta(older.delta, newer.delta) } : newer
}

/** ``delta`` cut to what one PATCH may carry: at most ``limit`` ids per
 *  list, or ``limit`` votes (the key's cap, which is what the server's
 *  body check allows). ``now`` goes first and ``later`` (null when it all
 *  fits) after its answer. Removes and cleared votes go before adds and
 *  votes, so the server's trim after each PATCH (smallest id first) ends
 *  where one PATCH of the whole change would. */
export function splitDelta(delta: Delta, limit: number): { now: Delta; later: Delta | null } {
  if (isVoteDelta(delta)) {
    const entries = Object.entries(delta.set)
    if (entries.length <= limit) return { now: delta, later: null }
    const ordered = [...entries.filter(([, v]) => v == null), ...entries.filter(([, v]) => v != null)]
    return { now: { set: Object.fromEntries(ordered.slice(0, limit)) }, later: { set: Object.fromEntries(ordered.slice(limit)) } }
  }
  if (delta.remove.length > limit) {
    return { now: { add: [], remove: delta.remove.slice(0, limit) }, later: { add: delta.add, remove: delta.remove.slice(limit) } }
  }
  if (delta.add.length > limit) {
    return { now: { add: delta.add.slice(0, limit), remove: delta.remove }, later: { add: delta.add.slice(limit), remove: [] } }
  }
  return { now: delta, later: null }
}

const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k)

/** ``write`` without what ``covered`` already carry: each id whose op (or
 *  vote) in ``write`` is the same in one of them. A whole value or a
 *  delete comes back as it is. Null when nothing is left. */
export function withoutOps(write: Write, covered: readonly Write[]): Write | null {
  if (write.op !== 'patch') return write
  const d = write.delta
  const others = covered.flatMap((w) => (w.op === 'patch' ? [w.delta] : []))
  if (isVoteDelta(d)) {
    const set: Record<string, Vote | null> = {}
    for (const [id, v] of Object.entries(d.set)) {
      if (!others.some((o) => isVoteDelta(o) && has(o.set, id) && o.set[id] === v)) set[id] = v
    }
    return Object.keys(set).length ? { op: 'patch', delta: { set } } : null
  }
  const ids = others.filter((o): o is IdDelta => !isVoteDelta(o))
  const add = d.add.filter((id) => !ids.some((o) => o.add.includes(id)))
  const remove = d.remove.filter((id) => !ids.some((o) => o.remove.includes(id)))
  return add.length || remove.length ? { op: 'patch', delta: { add, remove } } : null
}

/** What a merge must leave alone, and what it re-applies. */
export type Pending = {
  /** Keys whose incoming row is ignored (sync rule 5). */
  skip: ReadonlySet<string>
  /** Per key, the writes not yet acknowledged, oldest first. */
  writes: ReadonlyMap<string, readonly Write[]>
}

const NOTHING_PENDING: Pending = { skip: new Set(), writes: new Map() }

/** ``rows`` from the server merged into ``prev``, with the pending
 *  writes re-applied on top. ``full``: the response holds every row,
 *  so a key missing from it was deleted (unless it is skipped; a
 *  pending write still shows). Since mode leaves missing keys alone.
 *  An empty list or object is a value like any other. Unchanged values
 *  keep their reference, and with nothing changed ``prev`` comes back
 *  itself, so a resync doesn't re-render what it didn't change. */
export function mergeIncoming(
  prev: Rows,
  rows: readonly PrefRow[],
  pending: Pending = NOTHING_PENDING,
  opts: { full: boolean } = { full: false },
): Rows {
  let out: Record<string, unknown> | null = null
  const set = (key: string, value: unknown) => {
    const cur = (out ?? prev) as Record<string, unknown>
    if (value === undefined) {
      if (!(key in cur)) return
      out ??= { ...prev }
      delete out[key]
      return
    }
    if (key in cur && sameValue(cur[key], value)) return
    out ??= { ...prev }
    out[key] = value
  }
  const got = new Set<string>()
  for (const row of rows) {
    got.add(row.key)
    if (pending.skip.has(row.key)) continue
    set(row.key, applyWrites(row.key, normalizeValue(row.key, row.value), pending.writes.get(row.key) ?? []))
  }
  if (opts.full) {
    for (const key of Object.keys(prev)) {
      if (got.has(key) || pending.skip.has(key)) continue
      set(key, applyWrites(key, undefined, pending.writes.get(key) ?? []))
    }
  }
  return out ?? prev
}

// An ``updated_at`` in microseconds since the epoch, or null. The server
// sends microseconds; a Date keeps milliseconds, so the string is kept and
// only compared and shifted through this.
const STAMP_RE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/
export function stampMicros(stamp: string): number | null {
  const m = STAMP_RE.exec(stamp)
  if (!m) return null
  const frac = (m[2] ?? '').padEnd(6, '0')
  const ms = Date.parse(`${m[1]}.${frac.slice(0, 3)}${m[3]}`)
  return Number.isFinite(ms) ? ms * 1000 + Number(frac.slice(3, 6)) : null
}

/** The later of two ``updated_at`` strings (null loses). */
export function laterStamp(a: string | null, b: string): string | null {
  const mb = stampMicros(b)
  if (mb == null) return a
  const ma = a == null ? null : stampMicros(a)
  return ma == null || mb > ma ? b : a
}

const errorStatus = (err: unknown): number => (isPlainObject(err) && typeof err.status === 'number' ? err.status : 0)

/** How a failed write ends: tried again (it may get through later: no
 *  answer, a 5xx, or a 404 / 405 from a server rolled back to before
 *  PATCH), or refused for good (400, 409, 422 and other 4xx). */
export function classifyWriteError(err: unknown): 'retry' | 'refuse' {
  const status = errorStatus(err)
  if (status === 0 || status >= 500) return 'retry'
  return [404, 405, 408, 429].includes(status) ? 'retry' : 'refuse'
}

/** A 404 or 405: the server has no route for the write (rolled back to
 *  before PATCH). Tried again like any retry (rule 2), but only a reload
 *  onto the build the server serves can save it, so it doesn't hold that
 *  reload back (``flushPending``). */
export function isRolledBack(err: unknown): boolean {
  const status = errorStatus(err)
  return status === 404 || status === 405
}

/** What an old build's localStorage still holds, written so it can't take
 *  anything from the server: set keys add only the ids the screen
 *  (``current``) lacks, and the other keys are written only where no row
 *  exists. */
export function legacySeedPlan(
  legacy: Rows,
  current: Rows,
): { adds: Array<[string, number[]]>; puts: Array<[string, unknown]> } {
  const adds: Array<[string, number[]]> = []
  const puts: Array<[string, unknown]> = []
  for (const [key, value] of Object.entries(legacy)) {
    if (isSetKey(key)) {
      if (isVoteKey(key)) continue
      const add = diffIds(coerceIds(current[key]), coerceIds(value)).add
      if (add.length) adds.push([key, add])
    } else if (!(key in current) && value !== undefined) {
      puts.push([key, value])
    }
  }
  return { adds, puts }
}

// ---------------------------------------------------------------------------
// The engine.
// ---------------------------------------------------------------------------

/** Set keys go out in a fixed 1 s batch (a scroll marks many stories),
 *  other keys in a fixed 250 ms one. Fixed, not a debounce: the first
 *  write starts the clock. */
export const PATCH_BATCH_MS = 1000
export const PUT_BATCH_MS = 250
/** A write that didn't get through is tried again after these. */
export const RETRY_MS = [2000, 4000, 8000]
/** A write with no answer after this is given up on (sync rule 4). A
 *  keepalive or suspended request may never settle on iOS. */
export const INFLIGHT_DEADLINE_MS = 30_000
/** Polls ask for rows updated since the newest one seen, minus this:
 *  ``updated_at`` is the writing transaction's start, so a write that
 *  started first can commit after a later one was read. */
export const POLL_OVERLAP_MS = 60_000
export const GET_TIMEOUT_MS = 20_000
/** A poll this soon after the last GET was sent is answered by it. */
export const RESYNC_COALESCE_MS = 1000
/** Reset waits at most this long for the writes on the way, and the ones
 *  it sends, before its DELETEs (sync rule 9), like PR1r's flush before a
 *  reload: a fetch that never settles must not stop Reset. */
export const DISCARD_WAIT_MS = 3000

export type SyncTransport = {
  get: (since: string | null, signal: AbortSignal) => Promise<PrefRow[]>
  patch: (key: string, delta: Delta, keepalive: boolean) => Promise<PrefRow>
  put: (key: string, value: unknown, keepalive: boolean) => Promise<PrefRow>
  delete: (key: string, keepalive: boolean) => Promise<unknown>
}

export type SyncDeps = {
  transport: SyncTransport
  /** Shown rows changed. */
  onRows: (rows: Rows) => void
  /** A full GET ended: ``ok`` when its rows were adopted. */
  onLoad?: (ok: boolean) => void
  /** ``unsaved``: a write is still failing after its retries (once per
   *  round of retries). ``refused``: the server refused a write, which
   *  was dropped. */
  onError?: (kind: 'unsaved' | 'refused') => void
  now?: () => number
  setTimer?: (fn: () => void, ms: number) => unknown
  clearTimer?: (timer: unknown) => void
  /** Whether the page is in front. */
  visible?: () => boolean
}

type Flight = {
  key: string
  write: Write
  seq: number
  sentAt: number
  // The hide count when it was sent, and whether the page was hidden
  // then: once the page is back in front after a later hide, or after
  // the hide it was sent in, it is no longer waited for (sync rule 4).
  epoch: number
  whileHidden: boolean
  // Sent with ``keepalive`` (the leave flush, Reset): it outlives the page.
  keepalive: boolean
  done: Promise<void>
  // How it settled, once it has, and the row it was answered with when
  // that was OK (a DELETE's answer has none).
  outcome?: 'ok' | 'retry' | 'refused'
  row?: PrefRow
  // Its last failure was a 404 or 405 (``isRolledBack``).
  rolledBack?: boolean
  // The leave flush folded it into this later request (``by``) while it
  // was on the way; ``carries`` on that request lists the ones it holds.
  // ``gaveUp``: given up on (rule 4) while folded.
  by?: Flight
  carries?: Flight[]
  gaveUp?: boolean
}

type Get = { seq: number; full: boolean; ctrl: AbortController; done: Promise<void> }

export type PrefSync = ReturnType<typeof createPrefSync>

export function createPrefSync(deps: SyncDeps) {
  const t = deps.transport
  const now = deps.now ?? (() => Date.now())
  const setTimer = deps.setTimer ?? ((fn: () => void, ms: number) => setTimeout(fn, ms))
  const clearTimer = deps.clearTimer ?? ((timer: unknown) => clearTimeout(timer as ReturnType<typeof setTimeout>))
  const visible = deps.visible ?? (() => true)

  let base: Rows = {}
  // The request (GET or write) whose answer set each key's base.
  const baseSeq = new Map<string, number>()
  let shown: Rows = {}
  const queue = new Map<string, Write>()
  const flights = new Map<string, Flight[]>()
  // Writes the leave flush folded into a later request while they were
  // still on the way (sync rule 1's one exception). They don't hold up
  // their key, and their answers set nothing; how each settled is kept in
  // case the request that carries it fails (``settleStale``, ``unfold``).
  const stale = new Map<string, Flight[]>()
  // Writes given up on (sync rule 4), folded per key: still shown, never
  // sent again, until the next answer about the key replaces them.
  const limbo = new Map<string, Write>()
  // Per key, the ids that a write added (or, for votes, voted on), each
  // with the number of the request that last sent it: the server may hold
  // them although base doesn't, until an answer read after that request
  // is adopted (``adopted``).
  const sentAdds = new Map<string, Map<string, number>>()
  // Per key, the ids that a write given up on (rule 4) added or voted on.
  // That write may land at any time, after any answer, so these are never
  // forgotten: removing one of them always goes to the server.
  const lateAdds = new Map<string, Set<string>>()
  const retries = new Map<string, { attempt: number; timer: unknown | null }>()
  // When each key's latest write settled (or was given up on), as a
  // request number: a GET sent before that is ignored for the key.
  const settledAt = new Map<string, number>()
  let seq = 0
  let watermark: string | null = null
  let loadedOnce = false
  // A full resync is due: a write was refused or given up on, so its
  // key's base or what it shows may be out of date. Cleared by a full GET
  // sent after it.
  let needFullAfter: number | null = null
  let get: Get | null = null
  let followUp: { promise: Promise<void>; resolve: () => void } | null = null
  // Runs for RESYNC_COALESCE_MS after a GET is sent. A timer, not a
  // clock reading: the device's clock can jump.
  let coalesceTimer: unknown | null = null
  let patchTimer: unknown | null = null
  let putTimer: unknown | null = null
  // A batch held back by a write still on the way runs again at that
  // write's deadline, in case it never answers.
  let deadlineTimer: unknown | null = null
  let hideEpoch = 0
  let resumedEpoch = 0
  // Reset has begun (sync rule 9): no more GETs, and nothing is sent or
  // queued for the keys ``dropped`` names (the ones Reset deletes).
  let resetting = false
  let dropped: (key: string) => boolean = () => false
  const beforeFlush = new Set<() => void>()

  const emit = (next: Rows) => {
    if (next === shown) return
    shown = next
    deps.onRows(shown)
  }

  const busy = (key: string) => (flights.get(key)?.length ?? 0) > 0
  const allFlights = () => Array.from(flights.values()).flat()
  const allStale = () => Array.from(stale.values()).flat()
  const overlay = (key: string): Write[] => {
    const out: Write[] = []
    const l = limbo.get(key)
    if (l) out.push(l)
    for (const f of flights.get(key) ?? []) out.push(f.write)
    const q = queue.get(key)
    if (q) out.push(q)
    return out
  }

  // Shown values for `keys`, from base and what is pending.
  const recompute = (keys: Iterable<string>) => {
    let out: Record<string, unknown> | null = null
    for (const key of keys) {
      const value = applyWrites(key, base[key], overlay(key))
      const cur = (out ?? shown) as Record<string, unknown>
      if (value === undefined) {
        if (!(key in cur)) continue
        out ??= { ...shown }
        delete out[key]
      } else if (!(key in cur) || !sameValue(cur[key], value)) {
        out ??= { ...shown }
        out[key] = value
      }
    }
    if (out) emit(out)
  }

  // The server's value for `key` was just adopted, from the answer to
  // request `at`, sent after anything given up on: that answer takes the
  // place of the given-up writes on screen, and the adds of the requests
  // up to `at` are in it or gone. A later request's adds aren't (its
  // answer may be lost: ``unfold``), nor a given-up write's, which may
  // land after this answer was read (``lateAdds``). (A write the leave
  // flush folded into a later one may still land after it; see
  // ``settleStale``.)
  const adopted = (key: string, at: number) => {
    limbo.delete(key)
    const sent = sentAdds.get(key)
    if (!sent) return
    for (const [id, by] of sent) if (by <= at) sent.delete(id)
    if (!sent.size) sentAdds.delete(key)
  }

  const setBase = (key: string, value: unknown, at: number) => {
    const next = { ...base }
    if (value === undefined) delete next[key]
    else next[key] = normalizeValue(key, value)
    base = next
    baseSeq.set(key, at)
    adopted(key, at)
  }

  // Off the list: whatever its request answers later is ignored.
  const removeFlight = (f: Flight) => {
    const rest = (flights.get(f.key) ?? []).filter((x) => x !== f)
    if (rest.length) flights.set(f.key, rest)
    else flights.delete(f.key)
  }

  const removeStale = (f: Flight) => {
    const rest = (stale.get(f.key) ?? []).filter((x) => x !== f)
    if (rest.length) stale.set(f.key, rest)
    else stale.delete(f.key)
  }

  const schedule = (kind: 'patch' | 'put') => {
    if (kind === 'patch') {
      if (patchTimer == null) patchTimer = setTimer(() => {
        patchTimer = null
        sendBatch('patch')
      }, PATCH_BATCH_MS)
    } else if (putTimer == null) {
      putTimer = setTimer(() => {
        putTimer = null
        sendBatch('put')
      }, PUT_BATCH_MS)
    }
  }
  const kindOf = (key: string) => (isSetKey(key) ? 'patch' : 'put')

  const stopBatches = () => {
    for (const timer of [patchTimer, putTimer, deadlineTimer]) if (timer != null) clearTimer(timer)
    patchTimer = putTimer = deadlineTimer = null
  }

  const stopRetry = (key: string) => {
    const r = retries.get(key)
    if (r?.timer != null) {
      clearTimer(r.timer)
      r.timer = null
    }
  }

  // Back in the queue ahead of anything newer: a newer whole value
  // replaces it, and a newer change, which doesn't hold it, folds on top.
  const putBack = (key: string, write: Write) => {
    const q = queue.get(key)
    queue.set(key, q ? foldWrite(write, q) : write)
  }

  // Sync rule 4: no answer by the deadline, or, once the page is back in
  // front, sent before it last hid or while it was hidden.
  const givenUp = (f: Flight, at: number, back: boolean) =>
    at - f.sentAt >= INFLIGHT_DEADLINE_MS || (back && (f.epoch < hideEpoch || f.whileHidden))

  // Sync rule 4: a write that may never settle (a keepalive or a
  // suspended fetch on iOS) stops holding up its key, and its late answer
  // is ignored. It is not sent again: it may have landed, and another
  // device may have changed the same story since, which a second send
  // would undo. What it changed stays on screen until the next answer
  // about the key (the next full GET, which this makes the next resync,
  // or a later write's answer) shows what the server holds.
  const expire = () => {
    const at = now()
    const back = visible()
    for (const f of allFlights()) {
      if (!givenUp(f, at, back)) continue
      removeFlight(f)
      settledAt.set(f.key, ++seq)
      toLimbo(f)
    }
    // Folded into a later request, which carries its change: only its
    // late answer is ignored from now on (``unfold`` reads ``gaveUp``).
    for (const f of allStale()) {
      if (!givenUp(f, at, back)) continue
      removeStale(f)
      f.gaveUp = true
    }
  }

  // Given up on (rule 4): its change stays shown, never sent again, until
  // the next answer about the key, which the next full GET is sure to be.
  const toLimbo = (f: Flight) => {
    const l = limbo.get(f.key)
    limbo.set(f.key, l ? foldWrite(l, f.write) : f.write)
    const ids = f.write.op === 'patch' ? addedIds(f.write.delta) : []
    if (ids.length) {
      const late = lateAdds.get(f.key) ?? new Set<string>()
      for (const id of ids) late.add(id)
      lateAdds.set(f.key, late)
    }
    needFullAfter = seq
  }

  // Sync rule 1: one write in flight per key; a key waiting on a retry
  // timer waits for it.
  const sendBatch = (kind: 'patch' | 'put') => {
    expire()
    let deadline = Infinity
    for (const key of Array.from(queue.keys())) {
      if (kindOf(key) !== kind || retries.get(key)?.timer != null) continue
      if (busy(key)) {
        for (const f of flights.get(key)!) deadline = Math.min(deadline, f.sentAt + INFLIGHT_DEADLINE_MS)
        continue
      }
      send(key, false)
    }
    if (deadline < Infinity && deadlineTimer == null) {
      deadlineTimer = setTimer(() => {
        deadlineTimer = null
        sendBatch('patch')
        sendBatch('put')
      }, Math.max(0, deadline - now()))
    }
  }

  // A change as the server can take it. A remove (or a cleared vote) of
  // an id that is not in base, and that no write the server may still be
  // holding added (``sentAdds``, ``lateAdds``), is dropped: that id was
  // added and taken back, or pruned, before it ever went, so removing it
  // changes nothing. Without this, a key at its cap whose writes keep
  // failing would grow ``remove`` by one pruned id per add, past what one
  // PATCH may carry. A remove kept when it wasn't needed costs nothing.
  const forServer = (key: string, delta: Delta): Delta => {
    const sent = sentAdds.get(key)
    const late = lateAdds.get(key)
    const mayHold = (id: string) => !!sent?.has(id) || !!late?.has(id)
    if (isVoteDelta(delta)) {
      const held = coerceVotes(base[key])
      let set: Record<string, Vote | null> | null = null
      for (const [id, v] of Object.entries(delta.set)) {
        if (v != null || has(held, id) || mayHold(id)) continue
        set ??= { ...delta.set }
        delete set[id]
      }
      return set ? { set } : delta
    }
    const held = new Set(coerceIds(base[key]))
    const remove = delta.remove.filter((id) => held.has(id) || mayHold(String(id)))
    return remove.length === delta.remove.length ? delta : { add: delta.add, remove }
  }

  // The ids a change adds, or votes on, as vote-map keys.
  const addedIds = (delta: Delta): string[] =>
    isVoteDelta(delta) ? Object.entries(delta.set).flatMap(([id, v]) => (v == null ? [] : [id])) : delta.add.map(String)

  const noteSent = (key: string, delta: Delta, by: number) => {
    const ids = addedIds(delta)
    if (!ids.length) return
    let s = sentAdds.get(key)
    if (!s) sentAdds.set(key, (s = new Map()))
    for (const id of ids) s.set(id, by)
  }

  // The queued write for `key` goes out. A change goes as the server can
  // take it (``forServer``), at most one PATCH's worth at a time
  // (``splitDelta``): the rest stays queued for the next batch after the
  // answer. Returns the request on the way, or null when nothing went.
  const send = (key: string, keepalive: boolean): Flight | null => {
    let write = queue.get(key)
    if (!write) return null
    queue.delete(key)
    if (write.op === 'patch') {
      const { now: part, later } = splitDelta(forServer(key, write.delta), capFor(key) ?? Infinity)
      if (later) queue.set(key, { op: 'patch', delta: later })
      if (isEmptyDelta(part)) {
        recompute([key])
        return null
      }
      write = { op: 'patch', delta: part }
    }
    const sent = write
    const f: Flight = { key, write: sent, seq: ++seq, sentAt: now(), epoch: hideEpoch, whileHidden: !visible(), keepalive, done: Promise.resolve() }
    if (sent.op === 'patch') noteSent(key, sent.delta, f.seq)
    flights.set(key, [...(flights.get(key) ?? []), f])
    let req: Promise<unknown>
    try {
      req =
        sent.op === 'patch'
          ? t.patch(key, sent.delta, keepalive)
          : sent.op === 'put'
            ? t.put(key, sent.value, keepalive)
            : t.delete(key, keepalive)
    } catch (err) {
      req = Promise.reject(err)
    }
    f.done = req.then(
      (row) => settle(f, row as PrefRow | undefined, null),
      (err) => settle(f, undefined, err ?? new Error('failed')),
    )
    return f
  }

  const settle = (f: Flight, row: PrefRow | undefined, err: unknown) => {
    if ((stale.get(f.key) ?? []).includes(f)) {
      settleStale(f, row, err)
      return
    }
    // Off the list: dropped by Reset, or given up on (rule 4).
    if (!(flights.get(f.key) ?? []).includes(f)) return
    const key = f.key
    removeFlight(f)
    settledAt.set(key, ++seq)
    if (err == null) {
      f.outcome = 'ok'
      if (f.write.op !== 'delete') f.row = row
      // The whole row after this write. A newer answer about the key
      // (a later write's, or a GET sent after this write) wins.
      if (f.seq > (baseSeq.get(key) ?? -1)) setBase(key, f.write.op === 'delete' ? undefined : row?.value, f.seq)
      retries.delete(key)
      recompute([key])
      if (queue.has(key)) schedule(kindOf(key))
      return
    }
    if (classifyWriteError(err) === 'retry') {
      // Sync rule 2.
      f.outcome = 'retry'
      f.rolledBack = isRolledBack(err)
      const again = f.carries ? unfold(f) : f.write
      if (again) {
        putBack(key, again)
        armRetry(key)
      } else if (queue.has(key)) schedule(kindOf(key))
    } else {
      // Sync rule 3: dropped, and the screen goes back to what the
      // server has. A newer write queued behind it still goes, with a
      // fresh round of retries.
      f.outcome = 'refused'
      needFullAfter = seq
      retries.delete(key)
      deps.onError?.('refused')
      if (queue.has(key)) schedule(kindOf(key))
    }
    recompute([key])
  }

  // A request of the leave flush (``c``) failed and is tried again. What it
  // carried for the requests folded into it doesn't simply go again with
  // it: a resend of a change that landed undoes whatever another device
  // changed since. Each folded request keeps its own fate instead. One
  // that answered OK landed, and its row becomes base unless a newer
  // answer is in. One still on the way is waited for again like any
  // request (rule 1), and settles as one. One given up on (rule 4) stays
  // shown and isn't sent again. Only the change of one that failed too
  // goes again, inside ``c``'s. Returns what of ``c`` goes back in the
  // queue, null when nothing does.
  const unfold = (c: Flight): Write | null => {
    const key = c.key
    const carried = c.carries ?? []
    const covered: Write[] = []
    for (const p of carried) {
      if (p.outcome !== 'ok') continue
      covered.push(p.write)
      if (p.seq > (baseSeq.get(key) ?? -1)) setBase(key, p.write.op === 'delete' ? undefined : p.row?.value, p.seq)
    }
    for (const p of carried) {
      if (p.outcome != null) continue
      covered.push(p.write)
      if (p.gaveUp) toLimbo(p)
      else if ((stale.get(key) ?? []).includes(p)) {
        removeStale(p)
        p.by = undefined
        flights.set(key, [...(flights.get(key) ?? []), p].sort((a, b) => a.seq - b.seq))
      }
      // Otherwise it is already back on the list, or given up on there.
    }
    return withoutOps(c.write, covered)
  }

  // A write the leave flush folded into a later request (``f.by``)
  // settled. Its answer never sets base, and nothing is sent because of
  // it: ``f.by`` carries its change too. How it went is kept for the case
  // where ``f.by`` fails and is tried again (``unfold``).
  //
  // Two requests for one key were on the way at once, so this one may have
  // landed after ``f.by`` and put back what ``f.by`` reversed of it (an
  // Undo made just before the page hid). That can't be told from here:
  // the row each one answered with shows its own change whichever landed
  // first, and the answers can come in either order. Sending the reversal
  // again on a guess undid another device's later change whenever the
  // guess was wrong, so nothing is sent; the next GET shows what the server
  // holds (see the notes' Deviations).
  const settleStale = (f: Flight, row: PrefRow | undefined, err: unknown) => {
    removeStale(f)
    settledAt.set(f.key, ++seq)
    if (err != null) {
      f.outcome = classifyWriteError(err) === 'retry' ? 'retry' : 'refused'
      return
    }
    f.outcome = 'ok'
    if (f.write.op !== 'delete') f.row = row
  }

  // Sync rule 2: retries after RETRY_MS; once they are used up, one toast
  // for the round. A round ends when the key's write is saved or refused,
  // or the page comes back to the front.
  const armRetry = (key: string) => {
    const r = retries.get(key) ?? { attempt: 0, timer: null }
    retries.set(key, r)
    stopRetry(key)
    r.attempt++
    if (r.attempt > RETRY_MS.length) {
      if (r.attempt === RETRY_MS.length + 1) deps.onError?.('unsaved')
      return
    }
    r.timer = setTimer(() => {
      r.timer = null
      expire()
      if (queue.has(key) && !busy(key)) send(key, false)
    }, RETRY_MS[r.attempt - 1])
  }

  const enqueue = (key: string, write: Write) => {
    // Reset deletes this key next: a write made now would write it back.
    if (dropped(key)) return
    const q = queue.get(key)
    queue.set(key, q ? foldWrite(q, write) : write)
    recompute([key])
    schedule(write.op === 'patch' ? 'patch' : 'put')
  }

  // ---- writes ----

  /** Add story ids to an id-set key. Ids already shown are skipped. Over
   *  the cap, ``pruneIds`` drops ids (never these, nor ``keep``) in the
   *  same PATCH. */
  const addIds = (key: string, ids: readonly number[], keep: Iterable<number> = []) => {
    if (!isSetKey(key) || isVoteKey(key)) throw new Error(`addIds: ${key} is not an id-set key`)
    const cur = coerceIds(shown[key])
    const have = new Set(cur)
    const add = [...new Set(ids)].filter((id) => isId(id) && !have.has(id))
    if (!add.length) return
    const keepAll = new Set(keep)
    for (const id of add) keepAll.add(id)
    const { dropped } = pruneIds(applyIdDelta(cur, { add, remove: [] }), keepAll, capFor(key)!)
    const gone = new Set(dropped)
    // Only ids held now go in ``remove``: a dropped new id (more added
    // at once than the cap holds) is simply not sent.
    enqueue(key, { op: 'patch', delta: { add: add.filter((id) => !gone.has(id)), remove: dropped.filter((id) => have.has(id)) } })
  }

  /** Remove story ids from an id-set key. Ids not shown are skipped. */
  const removeIds = (key: string, ids: readonly number[]) => {
    if (!isSetKey(key) || isVoteKey(key)) throw new Error(`removeIds: ${key} is not an id-set key`)
    const have = new Set(coerceIds(shown[key]))
    const remove = [...new Set(ids)].filter((id) => have.has(id))
    if (remove.length) enqueue(key, { op: 'patch', delta: { add: [], remove } })
  }

  /** Set or clear (``null``) votes. Over ``MAX_VOTED``, the smallest
   *  other ids are cleared in the same PATCH. */
  const setVotes = (changes: Readonly<Record<string, Vote | null>>) => {
    const cur = coerceVotes(shown[VOTES_KEY])
    const set: Record<string, Vote | null> = {}
    for (const [id, v] of Object.entries(changes)) {
      if (!VOTE_ID_RE.test(id) || (v !== null && v !== 'up' && v !== 'down')) continue
      if ((cur[id] ?? null) !== v) set[id] = v
    }
    if (!Object.keys(set).length) return
    const after = Object.keys(applyVoteDelta(cur, { set })).map(Number)
    const { dropped } = pruneIds(after, Object.keys(set).map(Number), MAX_VOTED)
    for (const id of dropped) set[String(id)] = null
    enqueue(VOTES_KEY, { op: 'patch', delta: { set } })
  }

  /** Write a whole value to a key that isn't a set key. */
  const put = (key: string, value: unknown) => {
    if (isSetKey(key)) throw new Error(`put: ${key} is a set key; use addIds, removeIds or setVotes`)
    enqueue(key, { op: 'put', value })
  }

  /** Delete a key that isn't a set key. */
  const del = (key: string) => {
    if (isSetKey(key)) throw new Error(`delete: ${key} is a set key`)
    enqueue(key, { op: 'delete' })
  }

  // ---- reads ----

  const adopt = (g: Get, rows: readonly PrefRow[]) => {
    expire()
    const keys = new Set(rows.map((r) => r.key))
    if (g.full) for (const k of [...Object.keys(base), ...Object.keys(shown), ...limbo.keys()]) keys.add(k)
    // Sync rule 5.
    const skip = new Set<string>()
    for (const key of keys) if (busy(key) || (settledAt.get(key) ?? -1) > g.seq) skip.add(key)
    const writes = new Map<string, Write[]>()
    for (const key of keys) {
      if (skip.has(key)) continue
      baseSeq.set(key, g.seq)
      adopted(key, g.seq)
      writes.set(key, overlay(key))
    }
    base = mergeIncoming(base, rows, { skip, writes: new Map() }, { full: g.full })
    for (const row of rows) watermark = laterStamp(watermark, row.updated_at)
    if (g.full) {
      loadedOnce = true
      if (needFullAfter != null && g.seq > needFullAfter) needFullAfter = null
    }
    emit(mergeIncoming(shown, rows, { skip, writes }, { full: g.full }))
  }

  const startGet = (full: boolean): Promise<void> => {
    const g: Get = { seq: ++seq, full, ctrl: new AbortController(), done: Promise.resolve() }
    if (coalesceTimer != null) clearTimer(coalesceTimer)
    coalesceTimer = setTimer(() => {
      coalesceTimer = null
    }, RESYNC_COALESCE_MS)
    get = g
    const timer = setTimer(() => g.ctrl.abort(), GET_TIMEOUT_MS)
    const w = watermark == null ? null : stampMicros(watermark)
    const since = full || w == null ? null : new Date(Math.floor(w / 1000) - POLL_OVERLAP_MS).toISOString()
    let req: Promise<PrefRow[]>
    try {
      req = t.get(since, g.ctrl.signal)
    } catch (err) {
      req = Promise.reject(err)
    }
    g.done = req
      .then(
        (rows) => {
          if (get !== g || g.ctrl.signal.aborted) return
          adopt(g, rows)
          if (full) deps.onLoad?.(true)
        },
        () => {
          if (full) deps.onLoad?.(false)
        },
      )
      .finally(() => {
        clearTimer(timer)
        if (get !== g) return
        get = null
        const next = followUp
        followUp = null
        if (next) void resync({ full: true }).then(next.resolve)
      })
    return g.done
  }

  /** Read the server's rows. A poll asks only for rows changed since the
   *  newest one seen (minus ``POLL_OVERLAP_MS``); ``full`` asks for all,
   *  which is also how deleted rows go. One GET at a time (sync rule 7):
   *  a poll during a GET, or within ``RESYNC_COALESCE_MS`` of the last
   *  one, is answered by it; a full request during a poll runs right
   *  after it. */
  const resync = (opts: { full?: boolean } = {}): Promise<void> => {
    if (resetting) return Promise.resolve()
    expire()
    const full = !!opts.full || !loadedOnce || watermark == null || needFullAfter != null
    if (get) {
      if (!full || get.full) return get.done
      if (!followUp) {
        let resolve = () => {}
        const promise = new Promise<void>((r) => (resolve = r))
        followUp = { promise, resolve }
      }
      return followUp.promise
    }
    if (!full && coalesceTimer != null) return Promise.resolve()
    return startGet(full)
  }

  // ---- leaving and coming back ----

  const runBeforeFlush = () => {
    for (const fn of Array.from(beforeFlush)) {
      try {
        fn()
      } catch {
        // A collector that throws doesn't stop the writes already queued.
      }
    }
  }

  /** The page is going away or hiding (``visibilitychange: hidden``,
   *  ``pagehide``, ``beforeunload``): everything queued goes now with
   *  ``keepalive``, one key per request (a change over one PATCH's worth
   *  sends its first part; the rest waits for the return). A key whose
   *  earlier write is still on the way sends that change again, folded
   *  with the queued one, because the page may never come back to send
   *  it after the answer; the earlier request is kept as stale (``by``,
   *  ``carries``), and how it settles is kept (``settleStale``). So does
   *  a set key with nothing queued whose change is on the way in an
   *  ordinary request: iOS may cut that fetch off, and if its failure
   *  only comes after the return, rule 4 has given it up and nothing
   *  would send it again. One already sent with ``keepalive`` outlives
   *  the page, so a second leave flush doesn't send it a third time. */
  const leave = () => {
    runBeforeFlush()
    stopBatches()
    if (get) {
      get.ctrl.abort()
      get = null
      followUp?.resolve()
      followUp = null
    }
    for (const [key, fs] of flights) {
      if (queue.has(key) || !isSetKey(key) || dropped(key) || fs.every((f) => f.keepalive)) continue
      queue.set(key, { op: 'patch', delta: isVoteKey(key) ? { set: {} } : { add: [], remove: [] } })
    }
    for (const key of Array.from(queue.keys())) {
      const older = flights.get(key) ?? []
      const q = queue.get(key)!
      if (older.length && q.op === 'patch') {
        let delta: Delta | null = null
        for (const f of older) {
          // A request it carries that answered OK landed: its change
          // doesn't go again (as in ``unfold``).
          const known = (f.carries ?? []).filter((p) => p.outcome === 'ok').map((p) => p.write)
          const w = known.length ? withoutOps(f.write, known) : f.write
          if (w?.op === 'patch') delta = delta ? foldDelta(delta, w.delta) : w.delta
        }
        if (delta) queue.set(key, { op: 'patch', delta: foldDelta(delta, q.delta) })
      }
      stopRetry(key)
      const carrier = send(key, true)
      if (!carrier || !older.length) continue
      const folded = [...(stale.get(key) ?? []), ...older]
      for (const f of folded) {
        removeFlight(f)
        f.by = carrier
      }
      stale.set(key, folded)
      // One that answered OK while folded into an earlier request of the
      // leave flush stays on the list too: it landed, and this one carries
      // its change again.
      const landed = older.flatMap((f) => (f.carries ?? []).filter((p) => p.outcome === 'ok'))
      carrier.carries = [...landed, ...folded]
    }
    if (!visible()) hideEpoch++
  }

  /** Back in front after a hide: writes sent before it, or while the
   *  page was hidden, with no answer yet are given up on (rule 4), and
   *  everything queued, those waiting on a retry included, goes now with
   *  a new round of retries. (The full resync is the caller's.) */
  const resume = () => {
    if (resumedEpoch === hideEpoch) return
    resumedEpoch = hideEpoch
    expire()
    for (const [key, r] of retries) {
      stopRetry(key)
      r.attempt = 0
    }
    for (const key of Array.from(queue.keys())) if (!busy(key)) send(key, false)
  }

  // Everything queued goes now (one behind an earlier write after its
  // answer; a change over one PATCH's worth in parts), and this waits. A
  // write it sent that fails isn't sent again here; it waits on its retry
  // timer. True when nothing is left to try again, or what is left failed
  // with a 404 or 405 (``isRolledBack``).
  const flush = async (keepalive: boolean): Promise<boolean> => {
    runBeforeFlush()
    stopBatches()
    const mine = new Set<Flight>()
    const failed = new Set<string>()
    const rolledBack = new Set<string>()
    for (;;) {
      expire()
      for (const key of Array.from(queue.keys())) {
        if (failed.has(key) || busy(key)) continue
        stopRetry(key)
        const f = send(key, keepalive)
        if (f) mine.add(f)
      }
      const waiting = allFlights()
      if (!waiting.length) break
      await Promise.all(waiting.map((f) => f.done))
      for (const f of waiting) {
        if (!mine.has(f) || f.outcome !== 'retry') continue
        failed.add(f.key)
        if (f.rolledBack) rolledBack.add(f.key)
        else rolledBack.delete(f.key)
      }
    }
    return allFlights().length === 0 && Array.from(queue.keys()).every((key) => rolledBack.has(key))
  }

  /** Send everything queued now and wait (see ``flush``). True when
   *  nothing is left to try again: each write was saved, or refused for
   *  good. The reload onto a new build runs this first and holds off on
   *  false (PR1r). A write the server answered with a 404 or 405 doesn't
   *  count: the server has no route for it, a rollback, and only that
   *  reload, onto the build the server serves, can cure it. (The reload's
   *  leave flush sends it once more.) */
  const flushPending = (): Promise<boolean> => flush(false)

  /** Reset only (it reloads next). ``drop`` names the keys Reset deletes.
   *  For those, everything queued and its retries is dropped, what their
   *  writes on the way answer is ignored, and from here on nothing is
   *  queued or sent, not a write made meanwhile, nor the leave flush's:
   *  sent after the DELETEs, it would write the row back. Every other key
   *  goes on as before, and what is queued for it goes now, with
   *  ``keepalive`` (a save, hide or vote must outlive the reload). Waits
   *  for all of it and for the writes on the way, at most
   *  ``DISCARD_WAIT_MS`` (one given up on by rule 4 isn't waited for):
   *  what isn't answered by then goes on, and the reload's leave flush
   *  sends what is still queued. No GET from here on. */
  const discardPending = async (drop: (key: string) => boolean): Promise<void> => {
    const at = now()
    const back = visible()
    const before = dropped
    dropped = (key) => before(key) || drop(key)
    resetting = true
    get?.ctrl.abort()
    followUp?.resolve()
    followUp = null
    const waiting = [...allFlights(), ...allStale()].filter((f) => !givenUp(f, at, back)).map((f) => f.done)
    const keys = new Set([...queue.keys(), ...flights.keys(), ...stale.keys(), ...limbo.keys(), ...retries.keys()].filter(dropped))
    for (const key of keys) {
      stopRetry(key)
      retries.delete(key)
      queue.delete(key)
      flights.delete(key)
      stale.delete(key)
      limbo.delete(key)
      sentAdds.delete(key)
      lateAdds.delete(key)
    }
    recompute(keys)
    let timer: unknown = null
    const capped = new Promise<void>((resolve) => {
      timer = setTimer(resolve, DISCARD_WAIT_MS)
    })
    await Promise.race([Promise.allSettled([flush(true), ...waiting]), capped])
    clearTimer(timer)
  }

  /** Run ``fn`` before the leave flush and ``flushPending`` read the
   *  queue (the seen collectors drain into it). Returns the unregister. */
  const registerBeforeFlush = (fn: () => void) => {
    beforeFlush.add(fn)
    return () => {
      beforeFlush.delete(fn)
    }
  }

  return {
    rows: () => shown,
    addIds,
    removeIds,
    setVotes,
    put,
    delete: del,
    resync,
    leave,
    resume,
    flushPending,
    discardPending,
    registerBeforeFlush,
  }
}
