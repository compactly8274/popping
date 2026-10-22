// node --experimental-strip-types --test 'scripts/unit/*.test.ts'
import { afterEach, beforeEach, mock, test } from 'node:test'
import assert from 'node:assert/strict'
import * as store from '../../src/lib/sourceSaves.ts'
import { fieldSaveMessage, isRetryable } from '../../src/lib/apiErrors.ts'
import { sameHeaders } from '../../src/lib/headers.ts'

type Row = { id: number; name: string } & Record<string, unknown>
type Call = {
  id: number
  body: Record<string, unknown>
  keepalive: boolean
  resolve: (extra?: Record<string, unknown>) => void
  reject: (err: unknown) => void
}

let nextId = 1000
const flush = () => new Promise<void>((resolve) => setImmediate(resolve))
const apiError = (status: number, detail: unknown) => Object.assign(new Error(`${status}`), { name: 'ApiError', status, detail })

function setup(row: Partial<Row> = {}) {
  const id = nextId++
  const rows = new Map<number, Row>([[id, { id, name: `feed_${id}`, category: 'news', url: 'https://a.example/feed', active: true, custom_headers: null, ...row }]])
  const calls: Call[] = []
  const events = {
    serverRows: [] as Row[],
    renamed: [] as Array<[string, string]>,
    gone: [] as number[],
    failedAway: [] as Array<[number, Array<{ label: string; message: string }>]>,
  }
  store.configureSourceSaves({
    patch: (pid, body, opts) =>
      new Promise((resolve, reject) => {
        opts.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        calls.push({
          id: pid,
          body,
          keepalive: opts.keepalive,
          resolve: (extra = {}) => resolve({ ...rows.get(pid)!, ...body, ...extra }),
          reject,
        })
      }),
    getSource: (sid) => rows.get(sid),
    onServerRow: (r) => {
      events.serverRows.push(r as Row)
      rows.set(r.id, r as Row)
    },
    onRenamed: (a, b) => events.renamed.push([a, b]),
    onGone: (gid) => events.gone.push(gid),
    onFailedAway: (fid, failures) => events.failedAway.push([fid, failures]),
    describe: (err, field, value) => ({ message: fieldSaveMessage(err, field, value), retryable: isRetryable(err) }),
    equal: (field, a, b) => (field === 'custom_headers' ? sameHeaders(a as never, b as never) : a === b),
  })
  return { id, rows, calls, events }
}

beforeEach(() => {
  mock.timers.enable({ apis: ['setTimeout'] })
})

afterEach(() => {
  store.setPageHiding(false)
  // Run the store's zero-delay batch timer so it isn't left pending
  // across tests (reset discards mocked timers that never fired).
  mock.timers.tick(0)
  mock.timers.reset()
})

test('an equal value sends nothing and shows no state', () => {
  const { id, calls } = setup({ category: 'news' })
  store.commitField(id, 'category', 'news', 'category')
  assert.equal(calls.length, 0)
  assert.equal(store.getSaveState(id, 'category').status, 'idle')
})

test('a text field still sends when only trailing space changes', () => {
  const { id, calls } = setup({ category: 'news ' })
  store.commitField(id, 'category', 'news', 'category')
  assert.equal(calls.length, 1)
})

test('newer values for a queued field replace it: exactly 2 calls, the second with the last value', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'refresh_interval_seconds', 900, 'check interval')
  store.commitField(id, 'refresh_interval_seconds', 3600, 'check interval')
  store.commitField(id, 'refresh_interval_seconds', 21600, 'check interval')
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].body, { refresh_interval_seconds: 900 })
  calls[0].resolve()
  await flush()
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1].body, { refresh_interval_seconds: 21600 })
  calls[1].resolve()
  await flush()
  await store.whenIdle(id)
  assert.equal(calls.length, 2)
  assert.equal(store.getSaveState(id, 'refresh_interval_seconds').status, 'saved')
})

test('different fields go one at a time, one key per body', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'name', 'new_name', 'name')
  store.commitField(id, 'category', 'tech', 'category')
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].body, { name: 'new_name' })
  calls[0].resolve()
  await flush()
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1].body, { category: 'tech' })
})

test('a rename reports old and new names, then saved turns idle after 1.5 s', async () => {
  const { id, calls, events } = setup({ name: 'old_name' })
  store.commitField(id, 'name', 'new_name', 'name')
  calls[0].resolve()
  await flush()
  assert.deepEqual(events.renamed, [['old_name', 'new_name']])
  assert.equal(store.getSaveState(id, 'name').status, 'saved')
  mock.timers.tick(1499)
  assert.equal(store.getSaveState(id, 'name').status, 'saved')
  mock.timers.tick(1)
  assert.equal(store.getSaveState(id, 'name').status, 'idle')
})

test('a network error retries once after 2 s, and whenIdle waits for it', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'url', 'https://b.example/feed', 'address')
  calls[0].reject(new TypeError('Failed to fetch'))
  await flush()
  let idle = false
  void store.whenIdle(id).then(() => (idle = true))
  await flush()
  assert.equal(idle, false)
  mock.timers.tick(1999)
  await flush()
  assert.equal(calls.length, 1)
  assert.equal(idle, false)
  mock.timers.tick(1)
  await flush()
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1].body, { url: 'https://b.example/feed' })
  calls[1].resolve()
  await flush()
  assert.equal(idle, true)
  assert.equal(store.getSaveState(id, 'url').status, 'saved')
})

test('a second network error shows the message, retryable', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'url', 'https://b.example/feed', 'address')
  calls[0].reject(new TypeError('Failed to fetch'))
  await flush()
  mock.timers.tick(2000)
  await flush()
  calls[1].reject(new TypeError('Failed to fetch'))
  await flush()
  const s = store.getSaveState(id, 'url')
  assert.equal(s.status, 'error')
  assert.equal(s.message, "Couldn't reach Popping. Check your connection.")
  assert.equal(s.retryable, true)
  assert.equal(s.failedValue, 'https://b.example/feed')
})

test('page hiding sends every queued field at once with keepalive, without waiting', () => {
  const { id, calls } = setup()
  store.commitField(id, 'name', 'one', 'name')
  store.commitField(id, 'category', 'tech', 'category')
  store.commitField(id, 'url', 'https://c.example/feed', 'address')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].keepalive, false)
  store.setPageHiding(true)
  assert.equal(calls.length, 3)
  assert.deepEqual(calls.slice(1).map((c) => [c.body, c.keepalive]), [
    [{ category: 'tech' }, true],
    [{ url: 'https://c.example/feed' }, true],
  ])
})

test('commits while the page is hiding go out at once with keepalive', () => {
  const { id, calls } = setup()
  store.setPageHiding(true)
  store.commitField(id, 'name', 'one', 'name')
  store.commitField(id, 'category', 'tech', 'category')
  assert.equal(calls.length, 2)
  assert.ok(calls.every((c) => c.keepalive))
})

test('a retry waiting on its timer is sent at once when the page hides, and the old timer sends nothing', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'url', 'https://b.example/feed', 'address')
  calls[0].reject(new TypeError('Failed to fetch'))
  await flush()
  store.setPageHiding(true)
  assert.equal(calls.length, 2)
  assert.equal(calls[1].keepalive, true)
  calls[1].resolve()
  await flush()
  store.setPageHiding(false)
  mock.timers.tick(10_000)
  await flush()
  assert.equal(calls.length, 2)
})

test('getSaveState returns the same object until it changes, and a frozen IDLE', () => {
  const { id } = setup()
  const idle = store.getSaveState(id, 'name')
  assert.equal(idle, store.getSaveState(id, 'name'))
  assert.equal(idle, store.getSaveState(999_999, 'url'))
  assert.ok(Object.isFrozen(idle))
  assert.equal(idle.status, 'idle')
  store.commitField(id, 'name', 'changed', 'name')
  const saving = store.getSaveState(id, 'name')
  assert.equal(saving, store.getSaveState(id, 'name'))
  assert.ok(Object.isFrozen(saving))
  assert.deepEqual({ ...saving }, { status: 'saving', pending: 'changed' })
})

test('subscribers hear each change for their field only', async () => {
  const { id, calls } = setup()
  let nameHits = 0
  let categoryHits = 0
  const offName = store.subscribe(id, 'name', () => nameHits++)
  const offCategory = store.subscribe(id, 'category', () => categoryHits++)
  store.commitField(id, 'name', 'changed', 'name')
  calls[0].resolve()
  await flush()
  assert.equal(nameHits, 2) // saving, saved
  assert.equal(categoryHits, 0)
  offName()
  offCategory()
})

test('a value the server refused is not sent again by the same commit; Try again sends it', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'category', 'For You', 'category')
  calls[0].reject(apiError(422, "category 'For You' is reserved for a built-in column"))
  await flush()
  let s = store.getSaveState(id, 'category')
  assert.equal(s.status, 'error')
  assert.equal(s.message, '“For You” is used by the app. Pick another name.')
  assert.equal(s.retryable, false)
  store.commitField(id, 'category', 'For You', 'category')
  assert.equal(calls.length, 1)
  s = store.getSaveState(id, 'category')
  assert.equal(s.status, 'error')
  store.retryField(id, 'category')
  assert.equal(calls.length, 2)
  assert.deepEqual(calls[1].body, { category: 'For You' })
})

test('committing the saved value again clears a stale error', async () => {
  const { id, calls } = setup({ category: 'news' })
  store.commitField(id, 'category', 'For You', 'category')
  calls[0].reject(apiError(422, "category 'For You' is reserved for a built-in column"))
  await flush()
  store.commitField(id, 'category', 'news', 'category')
  assert.equal(calls.length, 1)
  assert.equal(store.getSaveState(id, 'category').status, 'idle')
})

test('clearError returns the field to idle', async () => {
  const { id, calls } = setup()
  store.commitField(id, 'name', 'Bad Name', 'name')
  calls[0].reject(apiError(422, 'name must match ^[a-z0-9_]{1,120}$'))
  await flush()
  store.clearError(id, 'name')
  assert.equal(store.getSaveState(id, 'name').status, 'idle')
})

test('404 calls onGone and drops what was queued', async () => {
  const { id, calls, events } = setup()
  store.commitField(id, 'name', 'a', 'name')
  store.commitField(id, 'category', 'tech', 'category')
  calls[0].reject(apiError(404, 'source not found'))
  await flush()
  assert.deepEqual(events.gone, [id])
  assert.equal(calls.length, 1)
  await store.whenIdle(id)
  assert.equal(store.getSaveState(id, 'name').message, 'This feed was deleted.')
})

test('two failures in one tick with nobody watching make one onFailedAway call', async () => {
  const { id, calls, events } = setup()
  store.setPageHiding(true)
  store.commitField(id, 'name', 'a', 'name')
  store.commitField(id, 'category', 'For You', 'category')
  assert.equal(calls.length, 2)
  calls[0].reject(apiError(409, "source name 'a' is already in use"))
  calls[1].reject(apiError(422, "category 'For You' is reserved for a built-in column"))
  await flush()
  mock.timers.tick(0)
  assert.equal(events.failedAway.length, 1)
  assert.equal(events.failedAway[0][0], id)
  assert.deepEqual(events.failedAway[0][1], [
    { label: 'name', message: 'Another feed already uses this name.' },
    { label: 'category', message: '“For You” is used by the app. Pick another name.' },
  ])
})

test('a failure with the editor open does not toast', async () => {
  const { id, calls, events } = setup()
  const off = store.subscribe(id, 'name', () => {})
  store.commitField(id, 'name', 'a', 'name')
  calls[0].reject(apiError(409, "source name 'a' is already in use"))
  await flush()
  mock.timers.tick(0)
  assert.equal(events.failedAway.length, 0)
  off()
})

test('a late result for a dropped feed is ignored', async () => {
  const { id, calls, events } = setup()
  store.commitField(id, 'name', 'late', 'name')
  store.dropSource(id)
  await store.whenIdle(id)
  calls[0].resolve()
  await flush()
  assert.equal(events.serverRows.length, 0)
  assert.notEqual(store.getSaveState(id, 'name').status, 'saved')
})

test('a slow save says so after 8 s, and gives up after 35 s', async () => {
  const { id } = setup()
  store.commitField(id, 'url', 'https://slow.example/feed', 'address')
  mock.timers.tick(8000)
  assert.equal(store.getSaveState(id, 'url').status, 'slow')
  mock.timers.tick(27_000)
  await flush()
  const s = store.getSaveState(id, 'url')
  assert.equal(s.status, 'error')
  assert.equal(s.message, 'That took too long. Try again.')
  assert.equal(s.retryable, true)
})

test('headers compare by content, so a reordered map sends nothing', () => {
  const { id, calls } = setup({ custom_headers: { 'User-Agent': 'x', Accept: 'y' } })
  store.commitField(id, 'custom_headers', { Accept: 'y', 'User-Agent': 'x' }, 'headers')
  assert.equal(calls.length, 0)
})

test('flushEditors calls every registered flusher until it unregisters', () => {
  let hits = 0
  const off = store.registerFlusher(() => hits++)
  store.flushEditors()
  off()
  store.flushEditors()
  assert.equal(hits, 1)
})
