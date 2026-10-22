import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_HIDDEN,
  MAX_READ_PER_KEY,
  MAX_VOTED,
  PATCH_BATCH_MS,
  POLL_OVERLAP_MS,
  PUT_BATCH_MS,
  RETRY_MS,
  INFLIGHT_DEADLINE_MS,
  DISCARD_WAIT_MS,
  applyIdDelta,
  applyVoteDelta,
  capFor,
  classifyWriteError,
  createPrefSync,
  diffIds,
  foldDelta,
  isRolledBack,
  isSetKey,
  laterStamp,
  legacySeedPlan,
  mergeIncoming,
  pruneIds,
  splitDelta,
  stampMicros,
  withoutOps,
} from '../../src/lib/prefSync.ts'
import type { Delta, PrefRow, Rows, Write } from '../../src/lib/prefSync.ts'

// ---------------------------------------------------------------------------
// A fake server that applies writes the way routes/preferences.py does, a
// fake clock, and a transport whose requests a test answers when it wants.
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-10-20T12:00:00Z')

// updated_at with microseconds, like the server's.
function stamp(micros: number): string {
  const ms = Math.floor(micros / 1000)
  return new Date(T0 + ms).toISOString().replace('Z', '') + String(micros % 1000).padStart(3, '0') + 'Z'
}

const CAPS: Record<string, number> = { starred_entries: 1000, hidden_entries: 1000, voted_entries: 2000 }
const serverCap = (key: string) => CAPS[key] ?? 1000

class FakeServer {
  rows = new Map<string, { value: unknown; updated_at: string }>()
  tick = 0
  write(key: string, value: unknown): PrefRow {
    const row = { value, updated_at: stamp(++this.tick * 1_000_001) }
    this.rows.set(key, row)
    return { key, ...row }
  }
  patch(key: string, body: Delta): PrefRow {
    // The route's body check: a list (or the votes) past the key's cap is a 422.
    const cap = serverCap(key)
    if ('set' in body ? Object.keys(body.set).length > cap : body.add.length > cap || body.remove.length > cap) {
      throw { name: 'ApiError', status: 422 }
    }
    const cur = this.rows.get(key)?.value
    if ('set' in body) {
      const votes: Record<string, string> = { ...((cur as Record<string, string>) ?? {}) }
      for (const [id, v] of Object.entries(body.set)) {
        if (v == null) delete votes[id]
        else votes[id] = v
      }
      const ids = Object.keys(votes).sort((a, b) => Number(a) - Number(b))
      for (const id of ids.slice(0, Math.max(0, ids.length - serverCap(key)))) delete votes[id]
      return this.write(key, votes)
    }
    const rm = new Set(body.remove)
    const ids = ((cur as number[]) ?? []).filter((id) => !rm.has(id))
    for (const id of body.add) if (!ids.includes(id)) ids.push(id)
    const keep = new Set([...ids].sort((a, b) => a - b).slice(-serverCap(key)))
    return this.write(key, ids.filter((id) => keep.has(id)))
  }
  list(since: string | null): PrefRow[] {
    const after = since == null ? null : stampMicros(since)!
    return [...this.rows].filter(([, r]) => after == null || stampMicros(r.updated_at)! > after).map(([key, r]) => ({ key, ...r }))
  }
  value(key: string): unknown {
    return this.rows.get(key)?.value
  }
}

type Req = {
  method: 'GET' | 'PATCH' | 'PUT' | 'DELETE'
  key: string
  body: unknown
  since: string | null
  keepalive: boolean
  settled: boolean
  // Runs it on the server now (a GET reads the rows as they are now).
  serve: () => void
  // Hands the answer to the page (serving it first if needed).
  respond: () => void
  // Fails it: a status, or no answer at all (a network error).
  fail: (status?: number) => void
}

// Every list a PATCH carries is within what the server takes.
const fits = (r: Req) => {
  const b = r.body as Delta
  const cap = serverCap(r.key)
  return 'set' in b ? Object.keys(b.set).length <= cap : b.add.length <= cap && b.remove.length <= cap
}

function setup(opts: { server?: FakeServer; auto?: boolean; visible?: () => boolean } = {}) {
  const server = opts.server ?? new FakeServer()
  let now = T0
  let timerId = 0
  const timers = new Map<number, { at: number; fn: () => void }>()
  const reqs: Req[] = []
  const errors: string[] = []
  let rows: Rows = {}
  let emits = 0
  let loads = 0
  // on: each request is answered as soon as it is made; offline: each
  // fails at once with no answer (a network error).
  const auto = { on: opts.auto ?? true, offline: false }

  const request = <T>(method: Req['method'], key: string, body: unknown, since: string | null, keepalive: boolean, run: () => T, signal?: AbortSignal) =>
    new Promise<T>((resolve, reject) => {
      let result: { v: T } | null = null
      const r: Req = {
        method,
        key,
        body,
        since,
        keepalive,
        settled: false,
        serve: () => {
          result ??= { v: run() }
        },
        respond: () => {
          if (r.settled) return
          try {
            r.serve()
          } catch (err) {
            r.settled = true
            reject(err)
            return
          }
          r.settled = true
          resolve(result!.v)
        },
        fail: (status?: number) => {
          if (r.settled) return
          r.settled = true
          reject(status ? { name: 'ApiError', status } : new TypeError('Failed to fetch'))
        },
      }
      signal?.addEventListener('abort', () => {
        if (r.settled) return
        r.settled = true
        reject(new DOMException('aborted', 'AbortError'))
      })
      reqs.push(r)
      if (auto.offline) queueMicrotask(() => r.fail())
      else if (auto.on) queueMicrotask(r.respond)
    })

  const sync = createPrefSync({
    transport: {
      get: (since, signal) => request('GET', '', null, since, false, () => server.list(since), signal),
      patch: (key, delta, keepalive) => request('PATCH', key, structuredClone(delta), null, keepalive, () => server.patch(key, delta)),
      put: (key, value, keepalive) => request('PUT', key, { value }, null, keepalive, () => server.write(key, value)),
      delete: (key, keepalive) =>
        request('DELETE', key, null, null, keepalive, () => {
          server.rows.delete(key)
        }),
    },
    onRows: (r) => {
      rows = r
      emits++
    },
    onLoad: (ok) => {
      if (ok) loads++
    },
    onError: (kind) => errors.push(kind),
    now: () => now,
    setTimer: (fn, ms) => {
      timers.set(++timerId, { at: now + ms, fn })
      return timerId
    },
    clearTimer: (id) => {
      timers.delete(id as number)
    },
    visible: opts.visible,
  })

  // Let promise callbacks run.
  const settle = async () => {
    for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r))
  }
  // Move the clock, firing timers in order, and let their requests run.
  const advance = async (ms: number) => {
    await settle()
    const end = now + ms
    for (;;) {
      const due = [...timers].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      timers.delete(due[0])
      now = due[1].at
      due[1].fn()
      await settle()
    }
    now = end
    await settle()
  }
  return {
    sync,
    server,
    reqs,
    errors,
    auto,
    rows: () => rows,
    emits: () => emits,
    loads: () => loads,
    settle,
    advance,
    setNow: (t: number) => {
      now = t
    },
    writes: () => reqs.filter((r) => r.method !== 'GET'),
    gets: () => reqs.filter((r) => r.method === 'GET'),
  }
}

// Loaded: the first full GET has been adopted.
async function loaded(opts: Parameters<typeof setup>[0] = {}) {
  const s = setup(opts)
  await s.sync.resync({ full: true })
  await s.settle()
  return s
}

const patchBody = (r: Req) => r.body as Delta

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

test('isSetKey and capFor: the five set namespaces, nothing else', () => {
  for (const k of ['starred_entries', 'hidden_entries', 'voted_entries', 'read_entries:News', 'seen:News/Tech']) assert.ok(isSetKey(k), k)
  for (const k of ['read_entries:', 'seen:', 'column_prefs:news', 'last_viewed:news', 'filter_presets', 'history_group_by', 'muted_sources']) {
    assert.ok(!isSetKey(k), k)
    assert.equal(capFor(k), null, k)
  }
  assert.equal(capFor('hidden_entries'), MAX_HIDDEN)
  assert.equal(capFor('voted_entries'), MAX_VOTED)
  assert.equal(capFor('seen:x'), MAX_READ_PER_KEY)
  assert.equal(capFor('read_entries:x'), MAX_READ_PER_KEY)
})

test('foldDelta: the later op on an id wins; remove applies before add within one', () => {
  assert.deepEqual(foldDelta({ add: [1, 2], remove: [] }, { add: [], remove: [1] }), { add: [2], remove: [1] })
  assert.deepEqual(foldDelta({ add: [], remove: [3] }, { add: [3], remove: [] }), { add: [3], remove: [] })
  // An id in both lists of one change ends added, as the server does it.
  assert.deepEqual(foldDelta({ add: [4], remove: [4] }, { add: [], remove: [] }), { add: [4], remove: [] })
  assert.deepEqual(foldDelta({ set: { 1: 'up', 2: 'down' } }, { set: { 1: null } }), { set: { 1: null, 2: 'down' } })
})

test('applyIdDelta and applyVoteDelta: remove first, new ids last, same reference when nothing changes', () => {
  const ids = [3, 1, 2]
  assert.equal(applyIdDelta(ids, { add: [1], remove: [9] }), ids)
  assert.deepEqual(applyIdDelta(ids, { add: [4, 1], remove: [3] }), [1, 2, 4])
  assert.deepEqual(ids, [3, 1, 2], 'the input is not changed')
  const votes = { 1: 'up' as const }
  assert.equal(applyVoteDelta(votes, { set: { 1: 'up', 2: null } }), votes)
  assert.deepEqual(applyVoteDelta(votes, { set: { 1: null, 2: 'down' } }), { 2: 'down' })
})

test('diffIds: what was added and what went', () => {
  assert.deepEqual(diffIds([1, 2, 3], [3, 4, 1]), { add: [4], remove: [2] })
})

test('pruneIds: ids outside keep go first, then the smallest; dropped is returned', () => {
  assert.deepEqual(pruneIds([5, 1, 9, 3], [], 4), { ids: [5, 1, 9, 3], dropped: [] })
  assert.deepEqual(pruneIds([5, 1, 9, 3, 7], [], 3), { ids: [5, 9, 7], dropped: [1, 3] })
  // 1 is kept (just added), so the next smallest outside keep goes.
  assert.deepEqual(pruneIds([5, 1, 9, 3, 7], [1], 3), { ids: [1, 9, 7], dropped: [5, 3] })
  // Everything is in keep: the smallest of it goes.
  assert.deepEqual(pruneIds([4, 2, 6], [4, 2, 6], 2), { ids: [4, 6], dropped: [2] })
})

test('splitDelta: one PATCH carries at most the cap per list; removes and cleared votes go first', () => {
  assert.deepEqual(splitDelta({ add: [1], remove: [2] }, 3), { now: { add: [1], remove: [2] }, later: null })
  assert.deepEqual(splitDelta({ add: [9], remove: [1, 2, 3, 4] }, 3), { now: { add: [], remove: [1, 2, 3] }, later: { add: [9], remove: [4] } })
  assert.deepEqual(splitDelta({ add: [5, 6, 7, 8], remove: [1] }, 3), { now: { add: [5, 6, 7], remove: [1] }, later: { add: [8], remove: [] } })
  assert.deepEqual(splitDelta({ set: { 1: 'up', 2: null, 3: 'down', 4: null } }, 3), { now: { set: { 2: null, 4: null, 1: 'up' } }, later: { set: { 3: 'down' } } })
  const small = { set: { 1: 'up' as const } }
  assert.equal(splitDelta(small, 3).now, small)
})

test('withoutOps: what the covering writes carry themselves is taken out; a reversal stays', () => {
  const add5: Write = { op: 'patch', delta: { add: [5], remove: [] } }
  assert.deepEqual(withoutOps({ op: 'patch', delta: { add: [5, 6], remove: [7] } }, [add5]), { op: 'patch', delta: { add: [6], remove: [7] } })
  assert.deepEqual(withoutOps({ op: 'patch', delta: { add: [], remove: [5] } }, [add5]), { op: 'patch', delta: { add: [], remove: [5] } })
  assert.equal(withoutOps({ op: 'patch', delta: { add: [5], remove: [] } }, [add5]), null)
  const up1: Write = { op: 'patch', delta: { set: { 1: 'up' } } }
  assert.deepEqual(withoutOps({ op: 'patch', delta: { set: { 1: 'up', 2: 'down' } } }, [up1]), { op: 'patch', delta: { set: { 2: 'down' } } })
  assert.deepEqual(withoutOps({ op: 'patch', delta: { set: { 1: null } } }, [up1]), { op: 'patch', delta: { set: { 1: null } } })
  const put: Write = { op: 'put', value: 1 }
  assert.equal(withoutOps(put, [put]), put)
})

test('stamps keep microseconds: the later of two, and a poll overlap from the string', () => {
  assert.equal(stampMicros('2026-10-20T12:00:00.000001Z')! - stampMicros('2026-10-20T12:00:00Z')!, 1)
  assert.equal(stampMicros('2026-10-20T12:00:00.5+00:00'), stampMicros('2026-10-20T12:00:00.500000Z'))
  assert.equal(laterStamp('2026-10-20T12:00:00.000002Z', '2026-10-20T12:00:00.000001Z'), '2026-10-20T12:00:00.000002Z')
  assert.equal(laterStamp(null, '2026-10-20T12:00:00.000001Z'), '2026-10-20T12:00:00.000001Z')
  assert.equal(laterStamp('2026-10-20T12:00:00Z', 'garbage'), '2026-10-20T12:00:00Z')
})

test('classifyWriteError: no answer, 5xx, 404, 405, 408, 429 retry; other 4xx are refused', () => {
  for (const e of [new TypeError('Failed to fetch'), { status: 503 }, { status: 404 }, { status: 405 }, { status: 429 }, { status: 408 }]) {
    assert.equal(classifyWriteError(e), 'retry', JSON.stringify(e))
  }
  for (const s of [400, 401, 409, 413, 422]) assert.equal(classifyWriteError({ status: s }), 'refuse', String(s))
  for (const s of [404, 405]) assert.equal(isRolledBack({ status: s }), true, String(s))
  for (const e of [new TypeError('Failed to fetch'), { status: 503 }, { status: 408 }, { status: 400 }]) assert.equal(isRolledBack(e), false, JSON.stringify(e))
})

// ---------------------------------------------------------------------------
// mergeIncoming.
// ---------------------------------------------------------------------------

const row = (key: string, value: unknown, at = '2026-10-20T12:00:00Z'): PrefRow => ({ key, value, updated_at: at })
const none = { skip: new Set<string>(), writes: new Map<string, Write[]>() }

test('mergeIncoming: unchanged keys keep their reference, and nothing changed returns prev', () => {
  const prev: Rows = { starred_entries: [1, 2], 'column_prefs:news': { sort: 'best', minScore: 0, maxAgeHours: null } }
  const same = mergeIncoming(prev, [row('starred_entries', [1, 2]), row('column_prefs:news', { sort: 'best', minScore: 0, maxAgeHours: null })], none, { full: true })
  assert.equal(same, prev)
  const next = mergeIncoming(prev, [row('starred_entries', [1, 2, 3]), row('column_prefs:news', { sort: 'best', minScore: 0, maxAgeHours: null })], none, { full: true })
  assert.notEqual(next, prev)
  assert.deepEqual(next.starred_entries, [1, 2, 3])
  assert.equal(next['column_prefs:news'], prev['column_prefs:news'])
})

test('mergeIncoming: full mode deletes a missing key and keeps an empty list; since mode leaves missing keys alone', () => {
  const prev: Rows = { starred_entries: [1], hidden_entries: [2], 'read_entries:news': [3] }
  const full = mergeIncoming(prev, [row('starred_entries', [])], none, { full: true })
  assert.deepEqual(full, { starred_entries: [] })
  const since = mergeIncoming(prev, [row('starred_entries', [])], none, { full: false })
  assert.deepEqual(since, { starred_entries: [], hidden_entries: [2], 'read_entries:news': [3] })
})

test('mergeIncoming: skipped keys are untouched; pending writes are applied on top, and keep a missing key', () => {
  const prev: Rows = { hidden_entries: [1, 9], starred_entries: [5], 'column_prefs:news': { sort: 'best' } }
  const pending = {
    skip: new Set(['hidden_entries']),
    writes: new Map<string, Write[]>([
      ['starred_entries', [{ op: 'patch', delta: { add: [6], remove: [] } }]],
      ['column_prefs:news', [{ op: 'put', value: { sort: 'best' } }]],
    ]),
  }
  const next = mergeIncoming(prev, [row('hidden_entries', [1]), row('starred_entries', [5, 7])], pending, { full: true })
  assert.equal(next.hidden_entries, prev.hidden_entries)
  assert.deepEqual(next.starred_entries, [5, 7, 6])
  assert.equal(next['column_prefs:news'], prev['column_prefs:news'], 'a queued PUT keeps the key the GET no longer has')
})

test('mergeIncoming: set-key values are cleaned on the way in', () => {
  const next = mergeIncoming({}, [row('starred_entries', [1, '2', 1, 0, 3.5, 4]), row('voted_entries', { 5: 'up', x: 'down', 6: 'sideways' }), row('hidden_entries', 'junk')], none, { full: true })
  assert.deepEqual(next, { starred_entries: [1, 4], voted_entries: { 5: 'up' }, hidden_entries: [] })
})

// ---------------------------------------------------------------------------
// The engine.
// ---------------------------------------------------------------------------

test('set keys are never written whole: put and delete refuse them, and the other keys take no changes', async () => {
  const s = await loaded()
  for (const key of ['starred_entries', 'hidden_entries', 'voted_entries', 'read_entries:News', 'seen:News']) {
    assert.throws(() => s.sync.put(key, []), /set key/, key)
    assert.throws(() => s.sync.delete(key), /set key/, key)
  }
  assert.throws(() => s.sync.addIds('column_prefs:news', [1]), /not an id-set key/)
  assert.throws(() => s.sync.removeIds('voted_entries', [1]), /not an id-set key/)
  s.sync.addIds('starred_entries', [1])
  s.sync.setVotes({ 1: 'up' })
  s.sync.put('history_group_by', 'none')
  s.sync.delete('last_viewed:news')
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(
    s.writes().map((r) => [r.method, r.key]),
    [
      ['PUT', 'history_group_by'],
      ['DELETE', 'last_viewed:news'],
      ['PATCH', 'starred_entries'],
      ['PATCH', 'voted_entries'],
    ],
  )
})

test('two adds in one tick both land, in one PATCH after the 1 s batch', async () => {
  const s = await loaded()
  s.sync.addIds('starred_entries', [101])
  s.sync.addIds('starred_entries', [102])
  assert.deepEqual(s.rows().starred_entries, [101, 102], 'both show at once')
  assert.equal(s.writes().length, 0, 'nothing before the batch')
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 1)
  assert.deepEqual(patchBody(s.writes()[0]), { add: [101, 102], remove: [] })
  assert.deepEqual(s.server.value('starred_entries'), [101, 102])
  assert.ok(s.writes().every((r) => r.method === 'PATCH'))
})

test('add then remove: folded before it goes, and ordered once the first is on the way', async () => {
  const s = await loaded()
  s.sync.addIds('hidden_entries', [7])
  s.sync.removeIds('hidden_entries', [7])
  assert.deepEqual(s.rows().hidden_entries, [])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 0, '7 never went, so removing it is nothing to send')

  // The add is on the way when the remove comes: the remove waits for it.
  s.auto.on = false
  s.sync.addIds('hidden_entries', [8])
  await s.advance(PATCH_BATCH_MS)
  s.sync.removeIds('hidden_entries', [8])
  await s.advance(PATCH_BATCH_MS * 3)
  assert.equal(s.writes().length, 1, 'one PATCH per key on the way')
  s.writes()[0].respond()
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2)
  assert.deepEqual(patchBody(s.writes()[1]), { add: [], remove: [8] })
  s.writes()[1].respond()
  await s.settle()
  assert.deepEqual(s.server.value('hidden_entries'), [])
  assert.deepEqual(s.rows().hidden_entries, [])
})

test('two devices: neither undoes the other, and a resync shows both', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [1])
  const a = await loaded({ server })
  const b = await loaded({ server })
  a.sync.addIds('starred_entries', [2])
  b.sync.addIds('starred_entries', [3])
  b.sync.removeIds('starred_entries', [1])
  await a.advance(PATCH_BATCH_MS)
  await b.advance(PATCH_BATCH_MS)
  assert.deepEqual(server.value('starred_entries'), [2, 3])
  await a.advance(2000)
  await a.sync.resync()
  await a.settle()
  assert.deepEqual(a.rows().starred_entries, [2, 3])
  assert.deepEqual(b.rows().starred_entries, [2, 3], "B's PATCH answer holds A's save")
})

test('an in-flight change survives a stale GET, and a GET sent before its answer is ignored for that key', async () => {
  const server = new FakeServer()
  server.write('hidden_entries', [1])
  server.write('starred_entries', [50])
  const s = await loaded({ server })
  s.auto.on = false
  s.sync.addIds('hidden_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  const patch = s.writes()[0]

  // A GET answered with the old set while the PATCH is on the way.
  const poll = s.sync.resync({ full: true })
  const get1 = s.gets().at(-1)!
  server.write('starred_entries', [50, 51])
  get1.respond()
  await poll
  assert.deepEqual(s.rows().hidden_entries, [1, 2], 'still hidden')
  assert.deepEqual(s.rows().starred_entries, [50, 51], 'other keys are adopted')

  // A GET served before the PATCH and answered after the PATCH's answer.
  const poll2 = s.sync.resync({ full: true })
  const get2 = s.gets().at(-1)!
  get2.serve()
  patch.respond()
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [1, 2])
  get2.respond()
  await poll2
  assert.deepEqual(s.rows().hidden_entries, [1, 2], 'the old set from before the answer is ignored')

  // A GET sent after the answer is adopted.
  server.patch('hidden_entries', { add: [], remove: [1] })
  const poll3 = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await poll3
  assert.deepEqual(s.rows().hidden_entries, [2])
})

test('a PUT key in flight survives a stale full GET', async () => {
  const server = new FakeServer()
  server.write('column_prefs:news', { sort: 'newest', minScore: 0, maxAgeHours: null })
  const s = await loaded({ server })
  s.auto.on = false
  const best = { sort: 'best', minScore: 0, maxAgeHours: null }
  s.sync.put('column_prefs:news', best)
  await s.advance(PUT_BATCH_MS)
  const put = s.writes()[0]
  assert.equal(put.method, 'PUT')
  const poll = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await poll
  assert.deepEqual(s.rows()['column_prefs:news'], best)
  put.respond()
  await s.settle()
  assert.deepEqual(s.rows()['column_prefs:news'], best)
  // A key queued but not yet sent: a full GET without the row keeps it.
  s.sync.put('column_sections:news', { newCollapsed: true, historyCollapsed: false })
  const poll2 = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await poll2
  assert.deepEqual(s.rows()['column_sections:news'], { newCollapsed: true, historyCollapsed: false })
})

test('reference identity: a resync that brings nothing new changes nothing', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [1, 2])
  server.write('column_prefs:news', { sort: 'best', minScore: 0, maxAgeHours: null })
  const s = await loaded({ server })
  const before = s.rows()
  const emits = s.emits()
  await s.advance(2000)
  await s.sync.resync({ full: true })
  await s.settle()
  assert.equal(s.rows(), before)
  assert.equal(s.emits(), emits)
  server.write('hidden_entries', [9])
  await s.advance(2000)
  await s.sync.resync({ full: true })
  await s.settle()
  assert.notEqual(s.rows(), before)
  assert.equal(s.rows().starred_entries, before.starred_entries)
  assert.equal(s.rows()['column_prefs:news'], before['column_prefs:news'])
})

test('caps: an add over the cap drops the smallest ids in the same PATCH, and the server ends with the same set', async () => {
  const server = new FakeServer()
  const full = Array.from({ length: MAX_READ_PER_KEY }, (_, i) => 100 + i)
  server.write('read_entries:News', full)
  const s = await loaded({ server })
  // 50 is older than everything stored but was just added: it stays.
  s.sync.addIds('read_entries:News', [50, 5000])
  await s.advance(PATCH_BATCH_MS)
  const body = patchBody(s.writes()[0])
  assert.deepEqual(body, { add: [50, 5000], remove: [100, 101] })
  const client = s.rows()['read_entries:News'] as number[]
  assert.equal(client.length, MAX_READ_PER_KEY)
  assert.deepEqual([...(server.value('read_entries:News') as number[])].sort((a, b) => a - b), [...client].sort((a, b) => a - b))
  // keep: ids on screen survive a prune (50 is no longer just added).
  s.sync.addIds('read_entries:News', [6000], [102])
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[1]), { add: [6000], remove: [50] })
  s.sync.addIds('read_entries:News', [7000], [102, 103])
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[2]), { add: [7000], remove: [104] })
  // More new ids than the cap holds: the smallest new ones are not sent,
  // and only ids held go in remove, so neither list passes the cap.
  const many = Array.from({ length: MAX_READ_PER_KEY + 5 }, (_, i) => 10_000 + i)
  s.sync.addIds('read_entries:News', many)
  await s.advance(PATCH_BATCH_MS)
  const big = patchBody(s.writes()[3]) as { add: number[]; remove: number[] }
  assert.equal(big.add.length, MAX_READ_PER_KEY)
  assert.deepEqual(big.add, many.slice(5))
  assert.equal(big.remove.length, MAX_READ_PER_KEY)
  assert.ok(big.remove.every((id) => id < 10_000), 'remove holds only ids that were stored')
  assert.deepEqual([...(server.value('read_entries:News') as number[])].sort((a, b) => a - b), many.slice(5))
})

test('votes: set, change and clear; over MAX_VOTED the smallest other ids are cleared', async () => {
  const server = new FakeServer()
  const votes: Record<string, string> = {}
  for (let i = 1; i <= MAX_VOTED; i++) votes[String(i + 10)] = 'up'
  server.write('voted_entries', votes)
  const s = await loaded({ server })
  s.sync.setVotes({ 5: 'down', 11: null, 12: 'down' })
  s.sync.setVotes({ 13: 'up' }) // no change: not sent
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[0]), { set: { 5: 'down', 11: null, 12: 'down' } })
  s.sync.setVotes({ 1: 'up', 2: 'up' })
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[1]), { set: { 1: 'up', 2: 'up', 5: null, 12: null } })
  const shown = s.rows().voted_entries as Record<string, string>
  assert.equal(Object.keys(shown).length, MAX_VOTED)
  assert.deepEqual(shown, server.value('voted_entries'))
})

test('a vote set and cleared before it goes is nothing to send; a cleared vote the server holds still goes', async () => {
  const server = new FakeServer()
  server.write('voted_entries', { 1: 'up' })
  const s = await loaded({ server })
  s.sync.setVotes({ 3: 'up' })
  s.sync.setVotes({ 3: null })
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 0)
  s.sync.setVotes({ 1: null, 4: 'down' })
  s.sync.setVotes({ 4: null })
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[0]), { set: { 1: null } })
  assert.deepEqual(server.value('voted_entries'), {})
})

test('a failed change goes back ahead of newer ones; retries after 2, 4 and 8 s, then one toast', async () => {
  const s = await loaded()
  s.auto.on = false
  s.sync.addIds('starred_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  s.writes()[0].fail(503)
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [1], 'still shown')
  // Newer: remove 1 and add 2. Later ops win over the failed add.
  s.sync.removeIds('starred_entries', [1])
  s.sync.addIds('starred_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 1, 'the batch waits for the retry')
  await s.advance(RETRY_MS[0] - PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2)
  assert.deepEqual(patchBody(s.writes()[1]), { add: [2], remove: [1] })
  s.writes()[1].fail()
  await s.advance(RETRY_MS[1] - 1)
  assert.equal(s.writes().length, 2)
  await s.advance(1)
  s.writes()[2].fail(500)
  await s.advance(RETRY_MS[2])
  assert.deepEqual(s.errors, [])
  s.writes()[3].fail(502)
  await s.settle()
  assert.deepEqual(s.errors, ['unsaved'], 'one toast once the retries are used up')
  assert.deepEqual(s.rows().starred_entries, [2])
  await s.advance(60_000)
  assert.equal(s.writes().length, 4, 'no more timed retries')
  // Coming back after a hide tries again.
  s.sync.leave()
  assert.equal(s.writes().length, 5)
  assert.ok(s.writes()[4].keepalive)
  s.writes()[4].respond()
  await s.settle()
  assert.deepEqual(s.server.value('starred_entries'), [2])
})

test('405 keeps the change queued and shown; 400 and 422 drop it and the screen goes back', async () => {
  const server = new FakeServer()
  server.write('hidden_entries', [1])
  const s = await loaded({ server })
  s.auto.on = false
  s.sync.addIds('hidden_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  s.writes()[0].fail(405)
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [1, 2])
  assert.deepEqual(s.errors, [])
  await s.advance(RETRY_MS[0])
  assert.deepEqual(patchBody(s.writes()[1]), { add: [2], remove: [] })
  s.writes()[1].fail(400)
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [1])
  assert.deepEqual(s.errors, ['refused'])
  s.sync.addIds('hidden_entries', [3])
  await s.advance(PATCH_BATCH_MS)
  s.writes()[2].fail(422)
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [1])
  await s.advance(30_000)
  assert.equal(s.writes().length, 3, 'a refused change is not sent again')
  // A refusal ends the key's round of retries: the next failure waits
  // the first delay again, not the next one.
  s.sync.addIds('hidden_entries', [4])
  await s.advance(PATCH_BATCH_MS)
  s.writes()[3].fail(503)
  await s.advance(RETRY_MS[0] - 1)
  assert.equal(s.writes().length, 4)
  await s.advance(1)
  assert.equal(s.writes().length, 5, `retried ${RETRY_MS[0]} ms after the failure`)
})

test('a full request during a since GET runs right after it; polls during a GET are answered by it', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [1])
  const s = await loaded({ server })
  s.auto.on = false
  await s.advance(5000)
  const poll = s.sync.resync()
  assert.equal(s.gets().length, 2)
  assert.ok(s.gets()[1].since, 'a poll asks with since')
  const again = s.sync.resync()
  const full = s.sync.resync({ full: true })
  assert.equal(s.gets().length, 2, 'one GET at a time')
  s.gets()[1].respond()
  await poll
  await again
  await s.settle()
  assert.equal(s.gets().length, 3, 'the full one went right after')
  assert.equal(s.gets()[2].since, null)
  s.gets()[2].respond()
  await full
  // A poll right after a GET was sent is answered by it.
  const soon = s.sync.resync()
  await soon
  assert.equal(s.gets().length, 3)
})

test('the watermark moves only with GETs: a PATCH answer does not move it; polls overlap by 60 s', async () => {
  const server = new FakeServer()
  const first = server.write('hidden_entries', [1]).updated_at
  const s = await loaded({ server })
  s.sync.addIds('starred_entries', [9])
  await s.advance(PATCH_BATCH_MS)
  assert.ok(stampMicros(server.rows.get('starred_entries')!.updated_at)! > stampMicros(first)!)
  await s.advance(2000)
  await s.sync.resync()
  await s.settle()
  const since = s.gets().at(-1)!.since!
  assert.equal(Date.parse(since), Math.floor(stampMicros(first)! / 1000) - POLL_OVERLAP_MS)
  // That GET held the PATCHed row, so now the watermark moves.
  const latest = server.rows.get('starred_entries')!.updated_at
  await s.advance(2000)
  await s.sync.resync()
  await s.settle()
  assert.equal(Date.parse(s.gets().at(-1)!.since!), Math.floor(stampMicros(latest)! / 1000) - POLL_OVERLAP_MS)
})

test('an in-flight record is cleared by its deadline: not sent again, its late answer ignored, and the next full GET settles the key', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [1])
  const s = await loaded({ server })
  s.auto.on = false
  s.sync.addIds('starred_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  const stuck = s.writes()[0]
  // A GET answered now ignores the key: a write is on its way.
  const poll = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await poll
  assert.deepEqual(s.rows().starred_entries, [1, 2])
  await s.advance(INFLIGHT_DEADLINE_MS + 60_000)
  assert.equal(s.writes().length, 1, 'never sent again')
  // Past the deadline the next resync is full, and its GET is taken in
  // for the key: the change still shows until then, also when something
  // else changes the key meanwhile (a save made and taken back, which
  // shows the key again from base and what is pending, and sends nothing).
  const poll2 = s.sync.resync()
  const get2 = s.gets().at(-1)!
  assert.equal(get2.since, null, 'the next resync is full')
  s.sync.addIds('starred_entries', [7])
  assert.deepEqual(s.rows().starred_entries, [1, 2, 7], 'the change given up on still shows')
  s.sync.removeIds('starred_entries', [7])
  assert.deepEqual(s.rows().starred_entries, [1, 2], 'the change given up on still shows')
  get2.respond()
  await poll2
  assert.deepEqual(s.rows().starred_entries, [1], 'the server never got 2')
  // The stuck one's late answer, whatever it is, changes nothing.
  stuck.fail(400)
  await s.settle()
  assert.deepEqual(s.errors, [])
  assert.deepEqual(s.rows().starred_entries, [1])

  // A change queued behind a stuck one goes on its own at the deadline,
  // and its answer settles the key.
  s.sync.addIds('starred_entries', [3])
  await s.advance(PATCH_BATCH_MS)
  const stuck2 = s.writes()[1]
  s.sync.addIds('starred_entries', [4])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2, 'waits for the one on the way')
  assert.deepEqual(s.rows().starred_entries, [1, 3, 4])
  await s.advance(INFLIGHT_DEADLINE_MS)
  assert.equal(s.writes().length, 3)
  assert.deepEqual(patchBody(s.writes()[2]), { add: [4], remove: [] }, 'only the queued change')
  s.writes()[2].respond()
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [1, 4], "that answer is the server's value, without 3")
  stuck2.respond()
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [1, 4], 'a late answer is ignored')
  await s.advance(60_000)
  assert.equal(s.writes().length, 3)
})

test('coming back after a hide: a write sent before it with no answer is given up on, not sent again, and the full GET settles it', async () => {
  let front = true
  const s = await loaded({ visible: () => front })
  s.auto.on = false
  s.sync.addIds('hidden_entries', [4])
  front = false
  s.sync.leave()
  const keep = s.writes()[0]
  assert.ok(keep.keepalive, 'leaving sends the queued change with keepalive at once')
  // It reaches the server, but the page is suspended before its answer.
  keep.serve()
  front = true
  s.sync.resume()
  assert.equal(s.writes().length, 1, 'not sent again')
  // Still shown, also once the key is shown again from base and what is
  // pending (a hide made and taken back, which sends nothing).
  s.sync.addIds('hidden_entries', [9])
  assert.deepEqual(s.rows().hidden_entries, [4, 9], 'still shown')
  s.sync.removeIds('hidden_entries', [9])
  assert.deepEqual(s.rows().hidden_entries, [4], 'still shown')
  // The full GET on the return (the caller's) is taken in for the key.
  const full = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await full
  assert.deepEqual(s.rows().hidden_entries, [4])
  // A new change doesn't wait for the one given up on.
  s.sync.addIds('hidden_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2)
  assert.deepEqual(patchBody(s.writes()[1]), { add: [5], remove: [] })
  s.writes()[1].respond()
  await s.settle()
  keep.respond()
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [4, 5])

  // One that never got there: shown until the full GET, which drops it.
  s.sync.addIds('hidden_entries', [6])
  front = false
  s.sync.leave()
  const lost = s.writes()[2]
  front = true
  s.sync.resume()
  s.sync.addIds('hidden_entries', [9])
  s.sync.removeIds('hidden_entries', [9])
  assert.deepEqual(s.rows().hidden_entries, [4, 5, 6])
  const full2 = s.sync.resync({ full: true })
  s.gets().at(-1)!.respond()
  await full2
  assert.deepEqual(s.rows().hidden_entries, [4, 5])
  await s.advance(60_000)
  lost.fail()
  await s.settle()
  assert.equal(s.writes().length, 3, 'never sent again')
  assert.deepEqual(s.errors, [])
  // A return without a hide in between does nothing.
  s.sync.resume()
  assert.equal(s.writes().length, 3)
})

test('a write sent while the page is hidden is given up on when it comes back, so it holds nothing up', async () => {
  let front = true
  const s = await loaded({ visible: () => front })
  s.auto.on = false
  front = false
  s.sync.leave()
  // Made and sent while hidden (a timer that ran before iOS suspended the page).
  s.sync.addIds('starred_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 1)
  front = true
  s.sync.resume()
  s.sync.addIds('starred_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2, 'the next change does not wait for it')
  assert.deepEqual(patchBody(s.writes()[1]), { add: [2], remove: [] })
})

test('another device changes a story while this page is hidden with its keepalive answer unread: the change stands after the return', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [42])
  front = false
  phone.sync.leave()
  const keep = phone.writes()[0]
  keep.serve()
  // The laptop sees the save and takes it back.
  await laptop.advance(2000)
  await laptop.sync.resync()
  await laptop.settle()
  assert.deepEqual(laptop.rows().starred_entries, [42])
  laptop.sync.removeIds('starred_entries', [42])
  await laptop.advance(PATCH_BATCH_MS)
  assert.deepEqual(server.value('starred_entries'), [])
  // The phone comes back: no second PATCH, and the full GET is adopted.
  front = true
  phone.sync.resume()
  const full = phone.sync.resync({ full: true })
  phone.gets().at(-1)!.respond()
  await full
  await phone.advance(60_000)
  keep.respond()
  await phone.settle()
  assert.equal(phone.writes().length, 1, 'the save is not sent again')
  assert.deepEqual(server.value('starred_entries'), [], "the laptop's unsave stands")
  assert.deepEqual(phone.rows().starred_entries, [])
})

// A write given up on (rule 4) may land after any answer about its key
// was read. An Undo queued before such an answer was taken in still goes:
// the id isn't in base then, but the server may be about to hold it.
for (const kind of ['save', 'hide', 'vote'] as const) {
  test(`a ${kind} given up on at its deadline that lands after the full GET was read: its Undo, made before that GET answered, still goes`, async () => {
    const key = kind === 'save' ? 'starred_entries' : kind === 'hide' ? 'hidden_entries' : 'voted_entries'
    const none = kind === 'vote' ? {} : []
    const server = new FakeServer()
    server.write(key, none)
    const s = await loaded({ server })
    s.auto.on = false
    if (kind === 'vote') s.sync.setVotes({ 5: 'up' })
    else s.sync.addIds(key, [5])
    await s.advance(PATCH_BATCH_MS)
    const stuck = s.writes()[0]
    await s.advance(INFLIGHT_DEADLINE_MS)
    // The next resync is full, and the Undo is made before it answers.
    const full = s.sync.resync()
    const g = s.gets().at(-1)!
    assert.equal(g.since, null)
    if (kind === 'vote') s.sync.setVotes({ 5: null })
    else s.sync.removeIds(key, [5])
    g.respond()
    await full
    assert.deepEqual(s.rows()[key], none)
    // Only now does the given-up request land.
    stuck.serve()
    assert.notDeepEqual(server.value(key), none)
    s.auto.on = true
    await s.advance(PATCH_BATCH_MS)
    assert.deepEqual(s.writes().slice(1).map(patchBody), [kind === 'vote' ? { set: { 5: null } } : { add: [], remove: [5] }], 'the Undo goes')
    assert.deepEqual(server.value(key), none, 'the Undo stands')
    await s.sync.resync({ full: true })
    assert.deepEqual(s.rows()[key], none, 'and the next full GET shows it')
  })
}

test('a save given up on at the return, landing after the full GET was read: the unsave made before that GET answered still goes', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  // The story opens before the answer; on the return both requests for 5
  // are given up on, and the full GET goes.
  front = false
  phone.sync.leave()
  const sent = phone.writes().slice()
  front = true
  phone.sync.resume()
  const full = phone.sync.resync({ full: true })
  assert.deepEqual(phone.rows().starred_entries, [5], 'still shown')
  // The Undo, then the GET answers (nothing has landed yet), then the
  // keepalive request lands.
  await phone.advance(300)
  phone.sync.removeIds('starred_entries', [5])
  phone.gets().at(-1)!.respond()
  await full
  sent.at(-1)!.serve()
  assert.deepEqual(server.value('starred_entries'), [5])
  phone.auto.on = true
  await phone.advance(PATCH_BATCH_MS)
  assert.deepEqual(phone.writes().slice(sent.length).map(patchBody), [{ add: [], remove: [5] }], 'the unsave goes')
  assert.deepEqual(server.value('starred_entries'), [])
  await phone.sync.resync({ full: true })
  assert.deepEqual(phone.rows().starred_entries, [], 'not saved again on the next full GET')
})

test("a save given up on at the return, landing after a later save's answer: the unsave queued behind that save still goes", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  front = false
  phone.sync.leave()
  const sent = phone.writes().slice()
  front = true
  phone.sync.resume()
  void phone.sync.resync({ full: true })
  // Save 6; unsave 5 while that PATCH is on the way (it waits, rule 1).
  phone.sync.addIds('starred_entries', [6])
  await phone.advance(PATCH_BATCH_MS)
  const f2 = phone.writes()[sent.length]
  assert.deepEqual(patchBody(f2), { add: [6], remove: [] })
  phone.sync.removeIds('starred_entries', [5])
  // 6's answer comes before the given-up request lands.
  f2.respond()
  await phone.settle()
  sent.at(-1)!.serve()
  assert.deepEqual(server.value('starred_entries'), [6, 5])
  phone.auto.on = true
  await phone.advance(PATCH_BATCH_MS)
  assert.deepEqual(phone.writes().slice(sent.length + 1).map(patchBody), [{ add: [], remove: [5] }])
  assert.deepEqual(server.value('starred_entries'), [6], 'the unsave stands')
})

test("a leave flush in front (beforeunload, the page stays): the carrying request lands but its answer is lost; an unsave of what it added still goes after the earlier request's answer", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  const s = await loaded({ server })
  s.auto.on = false
  s.sync.addIds('starred_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  const f1 = s.writes()[0]
  s.sync.addIds('starred_entries', [6])
  s.sync.leave()
  const c = s.writes()[1]
  assert.deepEqual(patchBody(c), { add: [5, 6], remove: [] })
  f1.respond()
  await s.settle()
  // The keepalive request lands; its answer never arrives.
  c.serve()
  c.fail()
  await s.settle()
  assert.deepEqual(server.value('starred_entries'), [5, 6])
  assert.deepEqual(s.rows().starred_entries, [5, 6])
  // The earlier request's row ([5]) is base now. 6 is taken back before
  // the retry.
  s.sync.removeIds('starred_entries', [6])
  s.auto.on = true
  await s.advance(RETRY_MS[0])
  assert.deepEqual(s.writes().slice(2).map(patchBody), [{ add: [], remove: [6] }], 'the unsave goes')
  assert.deepEqual(server.value('starred_entries'), [5])
  await s.sync.resync({ full: true })
  assert.deepEqual(s.rows().starred_entries, [5])
})

test('back in front once the retries were used up: no second toast before anything is sent, and the next failure starts a new round', async () => {
  let front = true
  const s = await loaded({ visible: () => front })
  s.auto.on = false
  s.sync.addIds('starred_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  s.writes()[0].fail(503)
  for (const ms of RETRY_MS) {
    await s.advance(ms)
    s.writes().at(-1)!.fail(503)
  }
  await s.settle()
  assert.equal(s.writes().length, 4)
  assert.deepEqual(s.errors, ['unsaved'])
  // Leaving sends it again; the page is suspended before the answer.
  front = false
  s.sync.leave()
  assert.equal(s.writes().length, 5)
  front = true
  s.sync.resume()
  await s.settle()
  assert.deepEqual(s.errors, ['unsaved'], 'no toast on the return')
  // The next failure is the first of a new round: 2, 4 and 8 s, then one toast.
  s.sync.addIds('starred_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 6)
  s.writes()[5].fail(503)
  await s.advance(RETRY_MS[0] - 1)
  assert.equal(s.writes().length, 6)
  await s.advance(1)
  assert.equal(s.writes().length, 7, `retried ${RETRY_MS[0]} ms after the failure`)
  s.writes()[6].fail(503)
  await s.advance(RETRY_MS[1])
  s.writes()[7].fail(503)
  await s.advance(RETRY_MS[2])
  assert.equal(s.writes().length, 9)
  assert.deepEqual(s.errors, ['unsaved'])
  s.writes()[8].fail(503)
  await s.settle()
  assert.deepEqual(s.errors, ['unsaved', 'unsaved'])
})

test('leaving with a write on the way: the earlier request\'s answer sends nothing more, also when it landed after the leave flush and undid an Undo', async () => {
  // Neither row can show which of the two landed last (each shows its own
  // change whichever went first), and the answers can come in any order,
  // so nothing is sent on a guess. The cost, under Deviations: an Undo
  // made just before the hide, overtaken this way, is lost; the next GET
  // shows it.
  for (const answers of ['landing order', 'reversed'] as const) {
    const s = await loaded({ visible: () => false })
    s.auto.on = false
    s.sync.addIds('hidden_entries', [5])
    await s.advance(PATCH_BATCH_MS)
    const f1 = s.writes()[0]
    s.sync.removeIds('hidden_entries', [5])
    s.sync.leave()
    assert.equal(s.writes().length, 2)
    const f2 = s.writes()[1]
    assert.ok(f2.keepalive)
    assert.deepEqual(patchBody(f2), { add: [], remove: [5] })
    // The keepalive request reaches the server first, then the earlier one.
    f2.serve()
    f1.serve()
    assert.deepEqual(s.server.value('hidden_entries'), [5], 'the earlier request hid 5 again')
    for (const r of answers === 'landing order' ? [f2, f1] : [f1, f2]) {
      r.respond()
      await s.settle()
    }
    assert.deepEqual(s.rows().hidden_entries, [], answers)
    s.auto.on = true
    await s.advance(60_000)
    assert.equal(s.writes().length, 2, `${answers}: nothing more goes`)
    await s.sync.resync({ full: true })
    assert.deepEqual(s.rows().hidden_entries, [5], `${answers}: the full GET shows what the server holds`)
  }
})

test('leaving with a write on the way: when the earlier request answers first, nothing more goes', async () => {
  const s = await loaded({ visible: () => false })
  s.auto.on = false
  s.sync.addIds('hidden_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  const f1 = s.writes()[0]
  s.sync.removeIds('hidden_entries', [5])
  s.sync.leave()
  const f2 = s.writes()[1]
  f1.respond()
  await s.settle()
  assert.deepEqual(s.rows().hidden_entries, [], "the earlier answer doesn't set what is shown")
  f2.respond()
  await s.settle()
  await s.advance(60_000)
  assert.equal(s.writes().length, 2)
  assert.deepEqual(s.server.value('hidden_entries'), [])
  assert.deepEqual(s.rows().hidden_entries, [])
})

// The earlier request lands first, its answer comes last, and another
// device saves the story (votes it up) in between. A check that took the
// answer order for the landing order sent the Undo again here and undid
// the other device's change.
for (const kind of ['ids', 'votes'] as const) {
  test(`leaving with a write on the way (${kind}): when the earlier request landed first but answers last, nothing more goes, and another device's later change stands`, async () => {
    const server = new FakeServer()
    const key = kind === 'ids' ? 'starred_entries' : 'voted_entries'
    server.write(key, kind === 'ids' ? [] : {})
    const phone = await loaded({ server, visible: () => false })
    const laptop = await loaded({ server })
    phone.auto.on = false
    if (kind === 'ids') phone.sync.addIds(key, [5])
    else phone.sync.setVotes({ 5: 'up' })
    await phone.advance(PATCH_BATCH_MS)
    const f1 = phone.writes()[0]
    // The Undo (or the other vote), then the page hides.
    if (kind === 'ids') phone.sync.removeIds(key, [5])
    else phone.sync.setVotes({ 5: 'down' })
    phone.sync.leave()
    const c = phone.writes()[1]
    f1.serve()
    c.serve()
    c.respond()
    await phone.settle()
    // The laptop sees that and saves the story (votes it up).
    await laptop.sync.resync({ full: true })
    if (kind === 'ids') laptop.sync.addIds(key, [5])
    else laptop.sync.setVotes({ 5: 'up' })
    await laptop.advance(PATCH_BATCH_MS)
    const want = kind === 'ids' ? [5] : { 5: 'up' }
    assert.deepEqual(server.value(key), want)
    f1.respond()
    await phone.settle()
    phone.auto.on = true
    await phone.advance(60_000)
    assert.equal(phone.writes().length, 2, 'no third PATCH')
    assert.deepEqual(server.value(key), want, "the laptop's later change stands")
  })
}

test("leaving: the request that carries an earlier one fails after that one landed: only the queued change goes again, and another device's unsave stands", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  const f1 = phone.writes()[0]
  phone.sync.addIds('starred_entries', [6])
  front = false
  phone.sync.leave()
  const c = phone.writes()[1]
  assert.deepEqual(patchBody(c), { add: [5, 6], remove: [] })
  f1.respond()
  await phone.settle()
  assert.deepEqual(server.value('starred_entries'), [5])
  // The backend restarts (popping-update): the keepalive request gets a 503.
  c.fail(503)
  await phone.settle()
  assert.deepEqual(phone.rows().starred_entries, [5, 6], 'both still shown')
  // The laptop sees 5 and unsaves it.
  await laptop.sync.resync({ full: true })
  laptop.sync.removeIds('starred_entries', [5])
  await laptop.advance(PATCH_BATCH_MS)
  assert.deepEqual(server.value('starred_entries'), [])
  // The phone comes back.
  phone.auto.on = true
  front = true
  phone.sync.resume()
  await phone.sync.resync({ full: true })
  await phone.advance(60_000)
  assert.deepEqual(patchBody(phone.writes()[2]), { add: [6], remove: [] }, 'only the queued change goes again')
  assert.equal(phone.writes().length, 3)
  assert.deepEqual(server.value('starred_entries'), [6], "the laptop's unsave stands")
  assert.deepEqual(phone.rows().starred_entries, [6])
})

test("leaving: the carrying request fails first, then the earlier one answers OK: only the queued change goes again", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  const f1 = phone.writes()[0]
  phone.sync.addIds('starred_entries', [6])
  front = false
  phone.sync.leave()
  phone.writes()[1].fail(502)
  await phone.settle()
  // Hidden on the phone: its retry waits for the earlier request (rule 1).
  await phone.advance(RETRY_MS[0])
  assert.equal(phone.writes().length, 2, 'the retry waits for the earlier request')
  f1.respond()
  await phone.settle()
  assert.deepEqual(phone.rows().starred_entries, [5, 6])
  await laptop.sync.resync({ full: true })
  laptop.sync.removeIds('starred_entries', [5])
  await laptop.advance(PATCH_BATCH_MS)
  phone.auto.on = true
  front = true
  phone.sync.resume()
  await phone.sync.resync({ full: true })
  await phone.advance(60_000)
  assert.deepEqual(phone.writes().slice(2).map(patchBody), [{ add: [6], remove: [] }])
  assert.deepEqual(server.value('starred_entries'), [6], "the laptop's unsave stands")
})

test('leaving, on a desktop tab left hidden: the carrying request fails after the earlier one landed; the retry, sent while hidden, holds only the queued change', async () => {
  const server = new FakeServer()
  server.write('hidden_entries', [])
  const desk = await loaded({ server, visible: () => false })
  const phone = await loaded({ server })
  desk.auto.on = false
  desk.sync.addIds('hidden_entries', [5])
  await desk.advance(PATCH_BATCH_MS)
  const f1 = desk.writes()[0]
  desk.sync.addIds('hidden_entries', [6])
  desk.sync.leave()
  const c = desk.writes()[1]
  f1.respond()
  await desk.settle()
  c.fail(503)
  await desk.settle()
  // The phone unhides 5 within the next second.
  await phone.sync.resync({ full: true })
  phone.sync.removeIds('hidden_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  assert.deepEqual(server.value('hidden_entries'), [])
  desk.auto.on = true
  await desk.advance(RETRY_MS[0])
  assert.deepEqual(desk.writes().slice(2).map(patchBody), [{ add: [6], remove: [] }])
  assert.deepEqual(server.value('hidden_entries'), [6], "the phone's unhide stands")
})

test('leaving: the carrying request fails while the earlier one is still on the way; when that one fails too, both changes go again', async () => {
  const server = new FakeServer()
  const s = await loaded({ server, visible: () => false })
  s.auto.on = false
  s.sync.addIds('starred_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  const f1 = s.writes()[0]
  s.sync.addIds('starred_entries', [6])
  s.sync.leave()
  s.writes()[1].fail(503)
  await s.settle()
  await s.advance(RETRY_MS[0])
  assert.equal(s.writes().length, 2, 'the queued change waits for the earlier request')
  assert.deepEqual(s.rows().starred_entries, [5, 6])
  f1.fail(503)
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [5, 6])
  // The key's second failure in this round: the next retry is 4 s on.
  await s.advance(RETRY_MS[1])
  assert.equal(s.writes().length, 3)
  assert.deepEqual(patchBody(s.writes()[2]), { add: [5, 6], remove: [] })
  s.writes()[2].respond()
  await s.settle()
  assert.deepEqual(server.value('starred_entries'), [5, 6])
  assert.deepEqual(s.rows().starred_entries, [5, 6])
})

test('leaving: an earlier request given up on (rule 4) while folded, then the carrying request fails: its change stays shown, is not sent again, and the next resync is full', async () => {
  // A desktop tab left hidden: only the 30 s deadline gives requests up.
  // (A row on the server, so the page has a watermark: a since poll is
  // possible.)
  const server = new FakeServer()
  server.write('starred_entries', [])
  const s = await loaded({ server, visible: () => false })
  s.auto.on = false
  s.sync.addIds('starred_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  const f1 = s.writes()[0]
  await s.advance(10_000)
  s.sync.addIds('starred_entries', [6])
  s.sync.leave()
  const c = s.writes()[1]
  assert.deepEqual(patchBody(c), { add: [5, 6], remove: [] })
  // The earlier request passes its deadline; the next batch gives it up.
  await s.advance(INFLIGHT_DEADLINE_MS - 10_000)
  s.sync.addIds('hidden_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  c.fail(503)
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [5, 6], 'both still shown')
  await s.advance(RETRY_MS[0])
  const again = s.writes().filter((r) => r.key === 'starred_entries').slice(2)
  assert.deepEqual(again.map(patchBody), [{ add: [6], remove: [] }], 'the given-up change is not sent again')
  again[0].respond()
  f1.respond()
  await s.settle()
  assert.equal(s.gets().length, 1)
  const poll = s.sync.resync()
  assert.equal(s.gets().at(-1)!.since, null, 'the next resync is full')
  s.gets().at(-1)!.respond()
  await poll
  assert.deepEqual(s.rows().starred_entries, [6, 5], 'the full GET shows what the server holds: the given-up request did land, last')
})

test('leaving: the late answer of an earlier request given up on while folded is ignored (rule 4), also when the carrying request then fails', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  const s = await loaded({ server, visible: () => false })
  s.auto.on = false
  s.sync.addIds('starred_entries', [5])
  await s.advance(PATCH_BATCH_MS)
  const f1 = s.writes()[0]
  await s.advance(10_000)
  s.sync.addIds('starred_entries', [6])
  s.sync.leave()
  const c = s.writes()[1]
  await s.advance(INFLIGHT_DEADLINE_MS - 10_000)
  s.sync.addIds('hidden_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  // Given up on; its answer comes after that, then the carrying request fails.
  f1.respond()
  await s.settle()
  c.fail(503)
  await s.settle()
  assert.deepEqual(s.rows().starred_entries, [5, 6], 'both still shown')
  await s.advance(RETRY_MS[0])
  const again = s.writes().filter((r) => r.key === 'starred_entries').slice(2)
  assert.deepEqual(again.map(patchBody), [{ add: [6], remove: [] }], 'the given-up change is not sent again')
  again[0].respond()
  await s.settle()
  const poll = s.sync.resync()
  assert.equal(s.gets().at(-1)!.since, null, 'its late row was not taken as the server\'s: the next resync is full')
  s.gets().at(-1)!.respond()
  await poll
  assert.deepEqual(s.rows().starred_entries, [5, 6])
})

test('leaving twice (visibilitychange, then pagehide) with requests on the way: one that landed is not sent again when both later ones fail', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  const phone = await loaded({ server, visible: () => false })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  const f1 = phone.writes()[0]
  phone.sync.addIds('starred_entries', [6])
  phone.sync.leave()
  const c1 = phone.writes()[1]
  f1.respond()
  await phone.settle()
  assert.deepEqual(server.value('starred_entries'), [5])
  // Another change before the page goes (pagehide's leave flush).
  phone.sync.addIds('starred_entries', [7])
  phone.sync.leave()
  const c2 = phone.writes()[2]
  // c1 is still on the way and goes again folded in, without the change
  // of f1, which answered OK: that one landed.
  assert.deepEqual(patchBody(c2), { add: [6, 7], remove: [] })
  c1.fail(503)
  c2.fail(503)
  await phone.settle()
  assert.deepEqual(phone.rows().starred_entries, [5, 6, 7])
  await laptop.sync.resync({ full: true })
  laptop.sync.removeIds('starred_entries', [5])
  await laptop.advance(PATCH_BATCH_MS)
  phone.auto.on = true
  await phone.advance(RETRY_MS[0])
  assert.deepEqual(phone.writes().slice(3).map(patchBody), [{ add: [6, 7], remove: [] }])
  assert.deepEqual(server.value('starred_entries'), [6, 7], "the laptop's unsave stands")
})

test("leaving twice: the second leave flush doesn't send again what an earlier request's OK answer showed landed, so another device's change made since stands", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('starred_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  const f1 = phone.writes()[0]
  // beforeunload, and the page stays (a mailto: link): the save on the
  // way goes again with keepalive.
  phone.sync.leave()
  const c1 = phone.writes()[1]
  assert.ok(c1.keepalive)
  f1.respond()
  await phone.settle()
  c1.serve()
  // The laptop sees 5 and unsaves it.
  await laptop.sync.resync({ full: true })
  laptop.sync.removeIds('starred_entries', [5])
  await laptop.advance(PATCH_BATCH_MS)
  assert.deepEqual(server.value('starred_entries'), [])
  // A save of 6 waits for c1 (rule 1); then the page hides.
  phone.sync.addIds('starred_entries', [6])
  await phone.advance(PATCH_BATCH_MS)
  assert.equal(phone.writes().length, 2)
  front = false
  phone.sync.leave()
  const c2 = phone.writes()[2]
  assert.deepEqual(patchBody(c2), { add: [6], remove: [] }, 'f1 answered OK, so 5 landed: it does not go again')
  c2.respond()
  c1.respond()
  await phone.settle()
  assert.deepEqual(server.value('starred_entries'), [6], "the laptop's unsave stands")
})

test('a request folded by the leave flush and given up on at the return: its late answer queues nothing, even when it landed last', async () => {
  const server = new FakeServer()
  server.write('hidden_entries', [])
  let front = true
  const phone = await loaded({ server, visible: () => front })
  const laptop = await loaded({ server })
  phone.auto.on = false
  phone.sync.addIds('hidden_entries', [5])
  await phone.advance(PATCH_BATCH_MS)
  const f1 = phone.writes()[0]
  phone.sync.removeIds('hidden_entries', [5])
  front = false
  phone.sync.leave()
  const c = phone.writes()[1]
  // The keepalive request lands first, the earlier one after it (5 is
  // hidden again); only the keepalive request's answer is read.
  c.serve()
  f1.serve()
  c.respond()
  await phone.settle()
  front = true
  phone.sync.resume()
  const full = phone.sync.resync({ full: true })
  phone.gets().at(-1)!.respond()
  await full
  assert.deepEqual(phone.rows().hidden_entries, [5], 'the full GET shows what the server holds')
  // The laptop hides 5 on purpose; then the earlier request's answer comes.
  laptop.sync.addIds('hidden_entries', [5])
  await laptop.advance(PATCH_BATCH_MS)
  f1.respond()
  await phone.settle()
  phone.auto.on = true
  await phone.advance(60_000)
  assert.equal(phone.writes().length, 2, 'nothing more goes')
  assert.deepEqual(server.value('hidden_entries'), [5], "the laptop's hide stands")
})

// Save, the 1 s batch sends an ordinary PATCH, and the story opens before
// its answer, with nothing queued. iOS may cut that fetch off, and its
// failure can come after the return, when rule 4 has given it up and
// ignores it; so the leave flush sends the change again with keepalive.
for (const [kind, first] of [
  ['ids', 'network error'],
  ['ids', '503'],
  ['votes', 'network error'],
  ['ids', 'answered OK'],
] as const) {
  test(`leaving with a lone ordinary request on the way (${kind}): its change goes again with keepalive, once; the first one ${first === 'answered OK' ? 'answering OK costs nothing more' : `failing after the return (${first}) loses nothing`}`, async () => {
    const key = kind === 'ids' ? 'starred_entries' : 'voted_entries'
    const want = kind === 'ids' ? [5] : { 5: 'up' }
    const server = new FakeServer()
    server.write(key, kind === 'ids' ? [] : {})
    let front = true
    const phone = await loaded({ server, visible: () => front })
    phone.auto.on = false
    if (kind === 'ids') phone.sync.addIds(key, [5])
    else phone.sync.setVotes({ 5: 'up' })
    await phone.advance(PATCH_BATCH_MS)
    const f1 = phone.writes()[0]
    assert.ok(!f1.keepalive)
    front = false
    phone.sync.leave()
    assert.equal(phone.writes().length, 2, 'the leave flush sends the change on the way again')
    const c = phone.writes()[1]
    assert.ok(c.keepalive)
    assert.deepEqual(patchBody(c), patchBody(f1))
    // pagehide's leave flush right after: the keepalive request outlives
    // the page, so it isn't sent a third time.
    phone.sync.leave()
    assert.equal(phone.writes().length, 2, 'not a third time')
    if (first === 'answered OK') {
      f1.respond()
      await phone.settle()
    }
    // The keepalive request gets through; the page comes back before its
    // answer.
    c.serve()
    front = true
    phone.sync.resume()
    const full = phone.sync.resync({ full: true })
    if (first === '503') f1.fail(503)
    else if (first === 'network error') f1.fail()
    await phone.settle()
    phone.gets().at(-1)!.respond()
    await full
    c.respond()
    await phone.settle()
    phone.auto.on = true
    await phone.advance(60_000)
    assert.equal(phone.writes().length, 2, 'nothing more goes')
    assert.deepEqual(server.value(key), want)
    assert.deepEqual(phone.rows()[key], want)
    assert.deepEqual(phone.errors, [])
  })
}

test('leaving: before-flush collectors run first; a key still on the way resends its change folded with the queued one', async () => {
  const s = await loaded({ visible: () => false })
  s.auto.on = false
  s.sync.addIds('starred_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  s.sync.addIds('starred_entries', [2])
  const unregister = s.sync.registerBeforeFlush(() => s.sync.addIds('seen:News', [77]))
  s.sync.put('column_prefs:news', { sort: 'best', minScore: 0, maxAgeHours: null })
  s.sync.leave()
  const sent = s.writes().slice(1)
  assert.deepEqual(
    sent.map((r) => [r.method, r.key, r.keepalive]),
    [
      ['PATCH', 'starred_entries', true],
      ['PUT', 'column_prefs:news', true],
      ['PATCH', 'seen:News', true],
    ],
  )
  assert.deepEqual(patchBody(sent[0]), { add: [1, 2], remove: [] })
  unregister()
  s.sync.leave()
  assert.equal(s.writes().length, 4, 'nothing left, and the collector is gone')
})

test('flushPending: sends everything once and says whether anything is left to retry', async () => {
  const s = await loaded()
  s.sync.addIds('starred_entries', [1])
  s.sync.put('history_group_by', 'none')
  assert.equal(await s.sync.flushPending(), true)
  assert.deepEqual(s.server.value('starred_entries'), [1])
  assert.equal(s.server.value('history_group_by'), 'none')
  s.auto.on = false
  s.sync.addIds('starred_entries', [2])
  const flushing = s.sync.flushPending()
  await s.settle()
  s.writes().at(-1)!.fail(503)
  assert.equal(await flushing, false, 'a change that did not get through is left')
  const again = s.sync.flushPending()
  await s.settle()
  s.writes().at(-1)!.fail(409)
  assert.equal(await again, true, 'refused for good is not left to retry')
})

test('flushPending: a write the server answers with 404 or 405 (rolled back) does not hold the reload back; a 503 does', async () => {
  const s = await loaded()
  s.auto.on = false
  s.sync.addIds('starred_entries', [1])
  let flushing = s.sync.flushPending()
  await s.settle()
  s.writes().at(-1)!.fail(405)
  assert.equal(await flushing, true, 'only the reload onto the served build can save it')
  assert.deepEqual(s.rows().starred_entries, [1], 'still queued and shown')
  // The next returns, each answered 405 or 404: the same.
  for (const status of [405, 404]) {
    flushing = s.sync.flushPending()
    await s.settle()
    s.writes().at(-1)!.fail(status)
    assert.equal(await flushing, true, String(status))
  }
  // With another key failing with a 503, there is something to retry.
  s.sync.addIds('hidden_entries', [2])
  flushing = s.sync.flushPending()
  await s.settle()
  for (const r of s.writes().filter((x) => !x.settled)) r.fail(r.key === 'hidden_entries' ? 503 : 405)
  assert.equal(await flushing, false)
  // The reload's leave flush still sends what is queued.
  const n = s.writes().length
  s.sync.leave()
  assert.deepEqual(s.writes().slice(n).map((r) => [r.key, r.keepalive]).sort(), [['hidden_entries', true], ['starred_entries', true]])
})

// What Reset deletes (App's ``resetDeletes``): read marks and tab settings.
const resetDeletes = (key: string) => key === 'history_group_by' || /^(read_entries|last_viewed|column_prefs|column_sections):/.test(key)
const NEWEST = { sort: 'newest', minScore: 0, maxAgeHours: null }

test('discardPending: drops what Reset deletes, sends the rest at once with keepalive, waits for writes on the way; then nothing for the dropped keys', async () => {
  const s = await loaded()
  s.auto.on = false
  s.sync.addIds('read_entries:news', [1])
  await s.advance(PATCH_BATCH_MS)
  const onWay = s.writes()[0]
  s.sync.addIds('read_entries:news', [3])
  s.sync.put('column_prefs:news', NEWEST)
  s.sync.addIds('hidden_entries', [2])
  let done = false
  const discarding = s.sync.discardPending(resetDeletes).then(() => (done = true))
  await s.settle()
  assert.deepEqual(
    s.writes().slice(1).map((r) => [r.method, r.key, r.keepalive]),
    [['PATCH', 'hidden_entries', true]],
    'the hide goes at once; the read mark and the tab setting are dropped',
  )
  s.writes()[1].respond()
  await s.settle()
  assert.deepEqual(s.server.value('hidden_entries'), [2])
  assert.equal(done, false, 'waits for the read mark on the way')
  onWay.fail(503)
  await discarding
  // Reset's DELETEs and the reload follow. Written meanwhile (a handler
  // still running): the read mark and the tab setting aren't sent, not by
  // a retry, a batch or the leave flush; the save is. No GET goes.
  s.sync.addIds('read_entries:news', [4])
  s.sync.put('column_prefs:news', NEWEST)
  s.sync.addIds('starred_entries', [5])
  assert.deepEqual(s.rows()['read_entries:news'], undefined, 'nothing queued for a key Reset deletes')
  const gets = s.gets().length
  await s.sync.resync({ full: true })
  await s.advance(60_000)
  s.sync.leave()
  // The save's PATCH is still on the way at the reload, so its leave
  // flush sends it again with keepalive (the reload cuts the first off).
  assert.deepEqual(
    s.writes().slice(2).map((r) => [r.method, r.key, r.keepalive]),
    [
      ['PATCH', 'starred_entries', false],
      ['PATCH', 'starred_entries', true],
    ],
  )
  assert.deepEqual(patchBody(s.writes()[3]), { add: [5], remove: [] })
  assert.equal(s.gets().length, gets, 'no GET after the discard')
})

test('discardPending: a write that never answers holds Reset up at most DISCARD_WAIT_MS, and one given up on not at all', async () => {
  const s = await loaded()
  s.auto.on = false
  s.sync.addIds('read_entries:news', [1])
  s.sync.addIds('starred_entries', [2])
  await s.advance(PATCH_BATCH_MS)
  assert.equal(s.writes().length, 2)
  s.sync.addIds('hidden_entries', [3])
  let done = false
  const discarding = s.sync.discardPending(resetDeletes).then(() => (done = true))
  await s.settle()
  assert.equal(s.writes().length, 3, 'the hide goes at once')
  await s.advance(DISCARD_WAIT_MS - 1)
  assert.equal(done, false, 'it waits for the writes on the way')
  await s.advance(1)
  assert.equal(done, true, 'then goes on without them')
  await discarding
  s.sync.addIds('read_entries:news', [4])
  s.sync.leave()
  // The reload's leave flush: nothing for a key Reset deletes; the save
  // still on the way in an ordinary request goes again with keepalive.
  assert.deepEqual(
    s.writes().slice(3).map((r) => [r.key, r.keepalive]),
    [['starred_entries', true]],
    'and sends nothing after for a key Reset deletes',
  )

  // Sent before the last hide, with the page back in front.
  let front = true
  const t = await loaded({ visible: () => front })
  t.auto.on = false
  t.sync.addIds('read_entries:news', [1])
  front = false
  t.sync.leave()
  front = true
  let done2 = false
  const discarding2 = t.sync.discardPending(resetDeletes).then(() => (done2 = true))
  await t.settle()
  assert.equal(done2, true)
  await discarding2
})

test('discardPending waits for a write the leave flush folded into a later one, too', async () => {
  // beforeunload while the page is in front: the earlier request is kept
  // as stale, and isn't given up on.
  const s = await loaded()
  s.auto.on = false
  s.sync.addIds('read_entries:news', [1])
  await s.advance(PATCH_BATCH_MS)
  const first = s.writes()[0]
  s.sync.addIds('read_entries:news', [2])
  s.sync.leave()
  const carrier = s.writes()[1]
  assert.deepEqual(patchBody(carrier), { add: [1, 2], remove: [] })
  carrier.respond()
  await s.settle()
  let done = false
  const discarding = s.sync.discardPending(resetDeletes).then(() => (done = true))
  await s.settle()
  assert.equal(done, false, 'it waits for the earlier request: it could land after the DELETEs')
  first.respond()
  await discarding
  assert.equal(s.writes().length, 2)
})

test('Reset keeps every save: one whose retries were used up, one waiting on a retry, one still in its batch', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [7])
  const s = await loaded({ server })
  // A vote whose retries were used up while offline (after the toast it
  // stays queued).
  s.auto.offline = true
  s.sync.setVotes({ 2: 'up' })
  await s.advance(PATCH_BATCH_MS + RETRY_MS.reduce((a, b) => a + b) + 100)
  assert.deepEqual(s.errors, ['unsaved'])
  s.auto.offline = false
  s.auto.on = false
  // A hide whose PATCH answered 503 and waits on its 2 s retry, and the
  // vote sent again with it (its retries are used up, so a batch takes it).
  s.sync.addIds('hidden_entries', [1])
  await s.advance(PATCH_BATCH_MS)
  for (const r of s.writes().filter((x) => !x.settled)) r.fail(503)
  await s.settle()
  // A save still in its batch, and a read mark (Reset deletes those).
  s.sync.addIds('starred_entries', [8])
  s.sync.addIds('read_entries:news', [9])
  const before = s.writes().length
  s.auto.on = true
  await s.sync.discardPending(resetDeletes)
  const sent = s.writes().slice(before)
  assert.deepEqual(sent.map((r) => [r.key, r.keepalive]).sort(), [
    ['hidden_entries', true],
    ['starred_entries', true],
    ['voted_entries', true],
  ])
  assert.deepEqual(server.value('starred_entries'), [7, 8])
  assert.deepEqual(server.value('hidden_entries'), [1])
  assert.deepEqual(server.value('voted_entries'), { 2: 'up' })
  assert.equal(server.value('read_entries:news'), undefined)
  s.sync.leave()
  await s.advance(60_000)
  assert.equal(s.writes().length, before + 3, 'nothing more goes')
})

test("Reset: a save whose PATCH fails during the wait is tried again, and what still fails goes with the reload's leave flush", async () => {
  const server = new FakeServer()
  const s = await loaded({ server })
  s.auto.on = false
  s.sync.addIds('starred_entries', [8])
  await s.advance(PATCH_BATCH_MS)
  const onWay = s.writes()[0]
  const discarding = s.sync.discardPending(resetDeletes)
  await s.settle()
  onWay.fail(503)
  await s.settle()
  assert.equal(s.writes().length, 2, 'sent again at once')
  assert.ok(s.writes()[1].keepalive)
  s.writes()[1].fail(502)
  await discarding
  // A save made during Reset's wait, after the flush.
  s.sync.addIds('starred_entries', [9])
  // The reload: pagehide's leave flush.
  s.auto.on = true
  s.sync.leave()
  await s.settle()
  assert.equal(s.writes().length, 3)
  assert.ok(s.writes()[2].keepalive)
  assert.deepEqual(server.value('starred_entries'), [8, 9])
})

test('a key at its cap, then more adds than the cap while offline: every PATCH after reconnecting fits, and the server ends with the newest', async () => {
  const server = new FakeServer()
  server.write('read_entries:news', Array.from({ length: MAX_READ_PER_KEY }, (_, i) => i + 1))
  const s = await loaded({ server })
  s.auto.offline = true
  const added = Array.from({ length: MAX_READ_PER_KEY + 200 }, (_, i) => 5001 + i)
  for (const id of added) {
    s.sync.addIds('read_entries:news', [id])
    await s.advance(PATCH_BATCH_MS)
  }
  assert.deepEqual(s.errors, ['unsaved'], 'one toast for the round')
  const offline = s.writes().length
  assert.ok(offline > 1000, `${offline} failed PATCHes`)
  s.auto.offline = false
  assert.equal(await s.sync.flushPending(), true)
  const back = s.writes().slice(offline)
  assert.ok(back.length >= 1)
  for (const r of back) {
    const b = patchBody(r) as { add: number[]; remove: number[] }
    assert.ok(fits(r), `a PATCH of ${b.add.length} adds and ${b.remove.length} removes`)
  }
  assert.deepEqual(s.errors, ['unsaved'], 'nothing refused')
  assert.equal(back.length, 2, 'the removes, then the adds')
  assert.deepEqual((patchBody(back[1]) as { remove: number[] }).remove, [], 'pruned ids that never reached the server are not removed')
  const newest = added.slice(-MAX_READ_PER_KEY)
  const asc = (ids: unknown) => [...(ids as number[])].sort((a, b) => a - b)
  assert.deepEqual(asc(server.value('read_entries:news')), newest)
  assert.deepEqual(asc(s.rows()['read_entries:news']), newest)
})

test('votes at MAX_VOTED, then more votes than that while offline: every PATCH after reconnecting fits, and the server keeps the newest', async () => {
  const server = new FakeServer()
  const votes: Record<string, string> = {}
  for (let i = 1; i <= MAX_VOTED; i++) votes[String(i)] = 'up'
  server.write('voted_entries', votes)
  const s = await loaded({ server })
  s.auto.offline = true
  // Fifty at a time (the cap is the same; single votes only take longer).
  const ids = Array.from({ length: MAX_VOTED + 100 }, (_, i) => 10_001 + i)
  for (let i = 0; i < ids.length; i += 50) {
    s.sync.setVotes(Object.fromEntries(ids.slice(i, i + 50).map((id) => [String(id), 'down' as const])))
    await s.advance(PATCH_BATCH_MS)
  }
  const offline = s.writes().length
  s.auto.offline = false
  assert.equal(await s.sync.flushPending(), true)
  const back = s.writes().slice(offline)
  for (const r of back) assert.ok(fits(r), `a PATCH of ${Object.keys((patchBody(r) as { set: object }).set).length} votes`)
  assert.deepEqual(s.errors, ['unsaved'], 'nothing refused')
  assert.equal(back.length, 2, 'the cleared votes, then the votes: a cleared vote the server never had is not sent')
  const want = Object.fromEntries(ids.slice(-MAX_VOTED).map((id) => [String(id), 'down']))
  assert.deepEqual(server.value('voted_entries'), want)
  assert.deepEqual(s.rows().voted_entries, want)
})

test('hidden_entries at its cap: Remove all while offline, then a hide and its Undo; reconnecting, the server takes it and ends empty', async () => {
  const all = Array.from({ length: MAX_HIDDEN }, (_, i) => i + 1)
  const server = new FakeServer()
  server.write('hidden_entries', all)
  const s = await loaded({ server })
  s.auto.offline = true
  s.sync.removeIds('hidden_entries', all)
  await s.advance(PATCH_BATCH_MS)
  s.sync.addIds('hidden_entries', [5000])
  s.sync.removeIds('hidden_entries', [5000])
  const offline = s.writes().length
  s.auto.offline = false
  await s.advance(RETRY_MS[0])
  const back = s.writes().slice(offline)
  assert.equal(back.length, 1)
  assert.ok(fits(back[0]))
  assert.deepEqual(patchBody(back[0]), { add: [], remove: all }, '5000 never went, so it is not removed')
  await s.advance(60_000)
  assert.equal(s.writes().length, offline + 1, 'and nothing more goes')
  assert.deepEqual(s.errors, [])
  assert.deepEqual(server.value('hidden_entries'), [])
  assert.deepEqual(s.rows().hidden_entries, [])

  // The hide went out in an attempt the server took but whose answer was
  // lost: its Undo goes too, in a PATCH of its own after the first part.
  const server2 = new FakeServer()
  server2.write('hidden_entries', all)
  const t = await loaded({ server: server2 })
  t.auto.on = false
  t.sync.removeIds('hidden_entries', all)
  await t.advance(PATCH_BATCH_MS)
  t.writes()[0].fail()
  t.sync.addIds('hidden_entries', [5000])
  await t.advance(RETRY_MS[0])
  const lost = t.writes()[1]
  assert.deepEqual(patchBody(lost), { add: [5000], remove: all })
  lost.serve()
  lost.fail()
  t.sync.removeIds('hidden_entries', [5000])
  await t.advance(RETRY_MS[1])
  const first = t.writes()[2]
  assert.ok(fits(first))
  assert.deepEqual(patchBody(first), { add: [], remove: all })
  first.respond()
  await t.advance(PATCH_BATCH_MS)
  const second = t.writes()[3]
  assert.deepEqual(patchBody(second), { add: [], remove: [5000] })
  second.respond()
  await t.settle()
  assert.deepEqual(t.errors, [])
  assert.deepEqual(server2.value('hidden_entries'), [])
  assert.deepEqual(t.rows().hidden_entries, [])
  assert.equal(t.writes().length, 4)
})

test('the first full GET failing: nothing is written over the server, and the next resync is full', async () => {
  const server = new FakeServer()
  server.write('starred_entries', [10, 11])
  const s = setup({ server, auto: false })
  const first = s.sync.resync()
  assert.equal(s.gets()[0].since, null)
  s.gets()[0].fail(500)
  await first
  assert.equal(s.loads(), 0)
  s.sync.addIds('starred_entries', [12])
  await s.advance(PATCH_BATCH_MS)
  assert.deepEqual(patchBody(s.writes()[0]), { add: [12], remove: [] })
  s.writes()[0].respond()
  await s.settle()
  assert.deepEqual(server.value('starred_entries'), [10, 11, 12])
  assert.deepEqual(s.rows().starred_entries, [10, 11, 12], "the PATCH answer brings the server's list")
  const next = s.sync.resync()
  assert.equal(s.gets()[1].since, null)
  s.gets()[1].respond()
  await next
  assert.equal(s.loads(), 1)
})

test("legacy localStorage lists plus server rows: no set-key PUT, and no server id removed", async () => {
  const server = new FakeServer()
  server.write('starred_entries', [1, 2])
  server.write('filter_presets', [{ id: 'p', name: 'Server view' }])
  const s = await loaded({ server })
  const legacy: Rows = {
    starred_entries: [2, 3],
    hidden_entries: [4],
    'read_entries:News': [5, 6],
    filter_presets: [{ id: 'old', name: 'Old view' }],
    history_group_by: 'none',
  }
  const plan = legacySeedPlan(legacy, s.rows())
  assert.deepEqual(plan.adds, [
    ['starred_entries', [3]],
    ['hidden_entries', [4]],
    ['read_entries:News', [5, 6]],
  ])
  assert.deepEqual(plan.puts, [['history_group_by', 'none']], 'filter_presets has a server row: not written')
  for (const [key, ids] of plan.adds) s.sync.addIds(key, ids)
  for (const [key, value] of plan.puts) s.sync.put(key, value)
  await s.advance(PATCH_BATCH_MS)
  for (const r of s.writes()) {
    if (isSetKey(r.key)) {
      assert.equal(r.method, 'PATCH', r.key)
      assert.deepEqual((r.body as { remove: number[] }).remove, [], r.key)
    }
  }
  assert.deepEqual(server.value('starred_entries'), [1, 2, 3])
  assert.deepEqual(server.value('filter_presets'), [{ id: 'p', name: 'Server view' }])
})
