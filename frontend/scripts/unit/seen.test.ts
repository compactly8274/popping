import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SEEN_BATCH_MS, SEEN_DWELL_MS, SEEN_THRESHOLDS, createSeenBatch, createSeenTracker } from '../../src/lib/seen.ts'
import type { SeenObserver, Sighting } from '../../src/lib/seen.ts'

// A card or the marker: in the document unless detached.
type El = { name: string; isConnected: boolean }
const el = (name: string): El => ({ name, isConnected: true })

// The list is 800px tall from y=100; a card is 200px tall unless said.
const ROOT = { top: 100, height: 800 }

// Where an element is: `top` of the element's box in page coordinates
// (the list shows 100..900).
function at(target: El, top: number, height = 200, root: { top: number; height: number } | null = ROOT): Sighting<El> {
  const bottom = top + height
  const rootBottom = root ? root.top + root.height : 0
  const visible = root ? Math.max(0, Math.min(bottom, rootBottom) - Math.max(top, root.top)) : 0
  const intersecting = !!root && height > 0 && bottom > root.top && top < rootBottom
  return {
    target,
    isIntersecting: intersecting,
    intersectionRect: { height: intersecting ? visible : 0 },
    boundingClientRect: { bottom, height },
    rootBounds: root,
  }
}
// What a detached or display:none target reports: every rect 0.
const zeros = (target: El): Sighting<El> => ({
  target,
  isIntersecting: false,
  intersectionRect: { height: 0 },
  boundingClientRect: { bottom: 0, height: 0 },
  rootBounds: { top: 0, height: 0 },
})

// A fake observer: what it was told to watch, and entries it holds back
// for takeRecords.
class FakeObserver implements SeenObserver<El> {
  watched = new Set<El>()
  observed: El[] = []
  queued: Sighting<El>[] = []
  disconnected = false
  observe(t: El) {
    this.watched.add(t)
    this.observed.push(t)
  }
  unobserve(t: El) {
    this.watched.delete(t)
  }
  disconnect() {
    this.watched.clear()
    this.disconnected = true
  }
  takeRecords() {
    const out = this.queued
    this.queued = []
    return out
  }
}

// A tracker on a fake clock, with its seen ids and switches for a
// hidden page and an open sheet.
function setup(opts: { modalOpen?: () => boolean } = {}) {
  let now = 0
  let id = 0
  const timers = new Map<number, { at: number; fn: () => void }>()
  const state = { hidden: false, modal: false }
  const seen: number[][] = []
  const tracker = createSeenTracker<El>({
    setTimer: (fn, ms) => {
      timers.set(++id, { at: now + ms, fn })
      return id
    },
    clearTimer: (t) => {
      timers.delete(t as number)
    },
    pageHidden: () => state.hidden,
    modalOpen: opts.modalOpen ?? (() => state.modal),
    onSeen: (ids) => seen.push(ids),
  })
  const advance = (ms: number) => {
    const end = now + ms
    for (;;) {
      const due = [...timers].filter(([, x]) => x.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      timers.delete(due[0])
      now = due[1].at
      due[1].fn()
    }
    now = end
  }
  const obs = new FakeObserver()
  tracker.attach(obs)
  const send = (...entries: Sighting<El>[]) => tracker.handle(entries, obs)
  const all = () => seen.flat()
  return { tracker, obs, send, advance, state, seen, all, timers }
}

test('the thresholds are every tenth from 0 to 1', () => {
  assert.equal(SEEN_THRESHOLDS.length, 11)
  assert.equal(SEEN_THRESHOLDS[0], 0)
  assert.equal(SEEN_THRESHOLDS[5], 0.5)
  assert.equal(SEEN_THRESHOLDS[10], 1)
})

test('a card on screen for the dwell, then off the top, is seen', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  assert.ok(t.obs.watched.has(a))
  t.send(at(a, 200))
  t.advance(SEEN_DWELL_MS - 1)
  t.send(at(a, 0))
  assert.deepEqual(t.all(), [], 'still half visible at 0 (100 of 200 px): not qualified yet')
  t.advance(1)
  t.send(at(a, -50))
  assert.deepEqual(t.all(), [], 'qualified, partly on screen: not seen yet')
  t.send(at(a, -120))
  assert.deepEqual(t.seen, [[1]])
})

test('a card flung past without the dwell is not seen', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 200))
  t.advance(SEEN_DWELL_MS - 100)
  t.send(at(a, -150))
  t.send(at(a, -300))
  t.advance(SEEN_DWELL_MS)
  assert.deepEqual(t.all(), [])
  assert.equal(t.timers.size, 0, 'the dwell timer went with the card')
})

test('dropping under half starts the dwell over', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 200))
  t.advance(600)
  t.send(at(a, 820)) // 80 of 200 px: under half
  t.send(at(a, 200))
  t.advance(600)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [], '600 ms + 600 ms is not 1 s straight')
})

test('a card taller than the list qualifies at half the list', () => {
  const t = setup()
  const tall = el('tall')
  t.tracker.registry.card(1, tall)
  // 2000px tall, filling the list's 800: never half of itself, but over
  // half of the list.
  t.send(at(tall, -300, 2000))
  t.advance(SEEN_DWELL_MS)
  t.send(at(tall, -2000, 2000))
  assert.deepEqual(t.seen, [[1]])
})

test('leaving through the bottom does not count', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, 950))
  assert.deepEqual(t.all(), [])
})

test('zero rects never count as leaving through the top', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  // display:none: bottom 0 <= top 0 would pass without the guards.
  t.send(zeros(a))
  assert.deepEqual(t.all(), [])
  // A zero-height list (the root hidden) doesn't count either.
  t.send(at(a, -300, 200, { top: 100, height: 0 }))
  assert.deepEqual(t.all(), [])
})

test('a detached target never counts', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  a.isConnected = false
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [])
})

test('a dwell that ends while the page is hidden starts over', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.state.hidden = true
  t.advance(SEEN_DWELL_MS)
  t.state.hidden = false
  t.advance(SEEN_DWELL_MS - 1)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [], 'qualified only a full dwell after the page came back')
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300))
  assert.deepEqual(t.seen, [[1]])
})

test('a dwell that ends while a sheet is open starts over', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.state.modal = true
  t.advance(3 * SEEN_DWELL_MS)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [], 'behind the Menu it never qualified')
  t.state.modal = false
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300))
  assert.deepEqual(t.seen, [[1]])
})

test('covered (the page hidden), a dwell under way stops and starts over once uncovered', () => {
  // iOS freezes a hidden page's timers and fires them late, just after
  // it is back. Here the page hides 600 ms into a dwell and is back at
  // once: a timer left running would fire 400 ms later and qualify the
  // card on 1 s of time it was mostly not on screen for.
  const t = setup()
  const [a, b] = [el('a'), el('b')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.send(at(a, 100), at(b, 300))
  t.advance(600)
  t.tracker.setCovered(true)
  assert.equal(t.timers.size, 0, 'no dwell runs while covered')
  t.tracker.setCovered(false)
  t.advance(SEEN_DWELL_MS - 1)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [], 'a full dwell after the return, not the 400 ms left before it')
  t.advance(1)
  t.send(at(b, -250))
  assert.deepEqual(t.seen, [[2]])
})

test('a card or the marker that comes on screen while covered starts its dwell once uncovered', () => {
  const t = setup()
  const [a, b, m] = [el('a'), el('b'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.tracker.registry.marker(m)
  t.tracker.setCovered(true)
  t.send(at(a, 100), at(b, 400), at(m, 700, 44))
  t.advance(3 * SEEN_DWELL_MS)
  assert.deepEqual(t.all(), [])
  t.tracker.setCovered(false)
  t.advance(SEEN_DWELL_MS - 1)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [], 'not qualified while covered, nor a moment short of a full dwell after')
  t.advance(1)
  assert.deepEqual(t.seen, [[2]], 'the end rule, a full dwell after the sheet closed')
})

test('a closed dialog that stays mounted does not pause: only App\'s modalOpen does', () => {
  // The Drawer is always mounted with aria-modal="true", open or not. The
  // tracker never looks at the page's dialogs, only at the boolean App
  // builds from its own state.
  const g = globalThis as { document?: unknown }
  const had = 'document' in g
  const before = g.document
  g.document = {
    visibilityState: 'visible',
    querySelector: (sel: string) => (/aria-modal/.test(sel) ? { closed: true } : null),
    querySelectorAll: (sel: string) => (/aria-modal/.test(sel) ? [{ closed: true }] : []),
  }
  try {
    const t = setup({ modalOpen: () => false })
    const a = el('a')
    t.tracker.registry.card(1, a)
    t.send(at(a, 300))
    t.advance(SEEN_DWELL_MS)
    t.send(at(a, -300))
    assert.deepEqual(t.seen, [[1]])
  } finally {
    if (had) g.document = before
    else delete g.document
  }
})

test('the end rule: the marker on screen for the dwell sees every qualified card on screen', () => {
  const t = setup()
  const [a, b, c, m] = [el('a'), el('b'), el('c'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.tracker.registry.card(3, c)
  t.tracker.registry.marker(m)
  assert.ok(t.obs.watched.has(m))
  // a and b on screen long enough; c only just in; the marker under them.
  t.send(at(a, 100), at(b, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(c, 500), at(m, 700, 44))
  t.advance(SEEN_DWELL_MS - 1)
  assert.deepEqual(t.all(), [])
  t.advance(1)
  // The marker's dwell and c's end together: all three are seen.
  assert.deepEqual(t.all().sort(), [1, 2, 3])
})

test('the end rule: a card that qualifies while the marker stays on screen is seen then', () => {
  const t = setup()
  const [a, b, m] = [el('a'), el('b'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.tracker.registry.marker(m)
  t.send(at(a, 100), at(m, 700, 44))
  t.advance(SEEN_DWELL_MS)
  assert.deepEqual(t.seen, [[1]])
  t.send(at(b, 400))
  t.advance(SEEN_DWELL_MS)
  assert.deepEqual(t.seen, [[1], [2]])
})

test('the end rule stops when the marker leaves, and needs a full dwell to start again', () => {
  const t = setup()
  const [a, b, m] = [el('a'), el('b'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.tracker.registry.marker(m)
  t.send(at(m, 700, 44))
  t.advance(SEEN_DWELL_MS)
  t.send(at(m, 950, 44)) // scrolled back up: the marker is below the list
  t.send(at(a, 300), at(b, 500))
  t.advance(SEEN_DWELL_MS)
  assert.deepEqual(t.all(), [], 'qualified, but the marker is gone')
  t.send(at(m, 750, 44))
  t.advance(SEEN_DWELL_MS - 1)
  assert.deepEqual(t.all(), [])
  t.advance(1)
  assert.deepEqual(t.all().sort(), [1, 2])
})

test('a card is seen once while the observer lasts', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300))
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300))
  assert.deepEqual(t.seen, [[1]])
})

test('one observer callback hands on its cards in one call', () => {
  const t = setup()
  const [a, b] = [el('a'), el('b')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.card(2, b)
  t.send(at(a, 100), at(b, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300), at(b, -250))
  assert.deepEqual(t.seen, [[1, 2]])
})

test('a root change: the old observer is disconnected, its timers and dwell gone', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS) // qualified under the old root
  t.tracker.detach(t.obs)
  assert.ok(t.obs.disconnected)
  assert.equal(t.timers.size, 0)
  const next = new FakeObserver()
  t.tracker.attach(next)
  assert.ok(next.watched.has(a))
  // The new observer's first report: the card is above the top (the
  // list was scrolled). It never qualified under this observer.
  t.tracker.handle([at(a, -300)], next)
  assert.deepEqual(t.all(), [])
  // Entries the old observer still had queued are dropped.
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.send(at(a, -300))
  assert.deepEqual(t.all(), [])
})

test('a dwell under way when the observer goes never fires', () => {
  const t = setup()
  const [a, m] = [el('a'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.marker(m)
  t.send(at(a, 300), at(m, 600, 44))
  t.advance(SEEN_DWELL_MS / 2)
  t.tracker.detach(t.obs)
  t.advance(SEEN_DWELL_MS * 2)
  assert.deepEqual(t.all(), [])
  assert.equal(t.timers.size, 0)
})

test('the hook case: register, tear the observer down, recreate it, and the card is observed again', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  // Turned past 768px: no observer, and the card stays registered (Card's
  // ref callback doesn't run again: nothing remounts).
  t.tracker.detach(t.obs)
  assert.equal(t.tracker.registered().get(1), a)
  // A card attached while no observer exists is registered only.
  const b = el('b')
  t.tracker.registry.card(2, b)
  // Back under 768px.
  const next = new FakeObserver()
  t.tracker.attach(next)
  assert.deepEqual([...next.watched].map((x) => x.name).sort(), ['a', 'b'])
  t.tracker.handle([at(a, 300)], next)
  t.advance(SEEN_DWELL_MS)
  t.tracker.handle([at(a, -300)], next)
  assert.deepEqual(t.seen, [[1]])
})

test('a late detach does not drop the element that replaced it', () => {
  const t = setup()
  const [old, fresh] = [el('old'), el('fresh')]
  t.tracker.registry.card(1, old)
  t.tracker.registry.card(1, fresh)
  assert.ok(!t.obs.watched.has(old))
  assert.ok(t.obs.watched.has(fresh))
  t.tracker.registry.card(1, null, old)
  assert.equal(t.tracker.registered().get(1), fresh)
  assert.ok(t.obs.watched.has(fresh))
  t.tracker.registry.card(1, null, fresh)
  assert.equal(t.tracker.registered().has(1), false)
  assert.ok(!t.obs.watched.has(fresh))
  // An entry for a card no longer registered is ignored.
  t.send(at(fresh, 300))
  assert.equal(t.timers.size, 0)
})

test('a new marker element starts its dwell over', () => {
  const t = setup()
  const [a, m1, m2] = [el('a'), el('row'), el('caught-up')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.marker(m1)
  t.send(at(a, 300), at(m1, 600, 44))
  t.advance(SEEN_DWELL_MS / 2)
  t.tracker.registry.marker(m2)
  t.tracker.registry.marker(null, m1) // the old row's detach, after
  assert.ok(t.obs.watched.has(m2))
  t.send(at(m2, 600, 44))
  t.advance(SEEN_DWELL_MS / 2)
  assert.deepEqual(t.all(), [], 'half a dwell on each element')
  t.advance(SEEN_DWELL_MS / 2)
  assert.deepEqual(t.seen, [[1]])
})

test('takeRecords handles what the observer has not delivered yet', () => {
  const t = setup()
  const a = el('a')
  t.tracker.registry.card(1, a)
  t.send(at(a, 300))
  t.advance(SEEN_DWELL_MS)
  t.obs.queued.push(at(a, -300))
  t.tracker.takeRecords()
  assert.deepEqual(t.seen, [[1]])
})

test('the batch collects for SEEN_BATCH_MS from the first story, then hands them on once', () => {
  let now = 0
  const timers: { at: number; fn: () => void }[] = []
  const drained: number[][] = []
  const batch = createSeenBatch<{ id: number }>({
    setTimer: (fn, ms) => {
      const t = { at: now + ms, fn }
      timers.push(t)
      return t
    },
    clearTimer: (t) => {
      const i = timers.indexOf(t as { at: number; fn: () => void })
      if (i >= 0) timers.splice(i, 1)
    },
    drain: (items) => drained.push(items.map((x) => x.id)),
    key: (x) => x.id,
  })
  const advance = (ms: number) => {
    now += ms
    for (const t of timers.filter((x) => x.at <= now)) {
      timers.splice(timers.indexOf(t), 1)
      t.fn()
    }
  }
  batch.add([{ id: 1 }])
  advance(SEEN_BATCH_MS - 100)
  batch.add([{ id: 2 }, { id: 1 }])
  advance(99)
  assert.deepEqual(drained, [])
  assert.ok(batch.has(1) && batch.has(2) && !batch.has(3), 'has: the stories still waiting')
  advance(1)
  assert.deepEqual(drained, [[1, 2]], 'one call, each story once, 300 ms after the first')
  assert.ok(!batch.has(1) && !batch.has(2), 'handed on: no longer waiting')
  batch.add([{ id: 3 }])
  assert.ok(batch.has(3))
  batch.flush()
  assert.ok(!batch.has(3), 'flushed: no longer waiting')
  assert.deepEqual(drained, [[1, 2], [3]], 'flush hands on at once')
  assert.equal(timers.length, 0)
  batch.flush()
  assert.equal(drained.length, 2, 'nothing to hand on: no call')
})

// The end rule's own dwell (the marker's) pauses like a card's: a short
// tab's last cards are on screen with the marker, and must not be used up
// behind the Menu or while the app is in the background.

test('the marker\'s dwell that ends while a sheet is open or the page is hidden starts over', () => {
  for (const flag of ['modal', 'hidden'] as const) {
    const t = setup()
    const [a, m] = [el('a'), el('marker')]
    t.tracker.registry.card(1, a)
    t.tracker.registry.marker(m)
    // Card 1 qualifies with no marker on screen.
    t.send(at(a, 100))
    t.advance(SEEN_DWELL_MS)
    assert.deepEqual(t.all(), [], `${flag}: qualified, not seen`)
    // The marker comes on screen, then the sheet opens (or the page
    // hides) with no setCovered: only the pause check stands.
    t.send(at(m, 700, 44))
    t.state[flag] = true
    t.advance(3 * SEEN_DWELL_MS)
    assert.deepEqual(t.all(), [], `${flag}: the end rule ran behind it`)
    t.state[flag] = false
    t.advance(SEEN_DWELL_MS - 1)
    assert.deepEqual(t.all(), [], `${flag}: seen before a full dwell after`)
    t.advance(1)
    assert.deepEqual(t.seen, [[1]], `${flag}: the end rule, a dwell after it closed`)
  }
})

test('covered, the marker\'s dwell stops, and starts over once uncovered', () => {
  const t = setup()
  const [a, m] = [el('a'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.marker(m)
  t.send(at(a, 100))
  t.advance(SEEN_DWELL_MS)
  t.send(at(m, 700, 44))
  t.advance(SEEN_DWELL_MS / 2)
  t.tracker.setCovered(true)
  assert.equal(t.timers.size, 0, 'the marker\'s dwell stopped')
  t.advance(3 * SEEN_DWELL_MS)
  assert.deepEqual(t.all(), [])
  t.tracker.setCovered(false)
  t.advance(SEEN_DWELL_MS - 1)
  assert.deepEqual(t.all(), [], 'a full dwell after uncovering, not the half left before the cover')
  t.advance(1)
  assert.deepEqual(t.seen, [[1]])
})

test('the marker coming on screen while covered starts no dwell', () => {
  const t = setup()
  const [a, m] = [el('a'), el('marker')]
  t.tracker.registry.card(1, a)
  t.tracker.registry.marker(m)
  // Card 1 qualified before the cover.
  t.send(at(a, 100))
  t.advance(SEEN_DWELL_MS)
  t.tracker.setCovered(true)
  t.send(at(m, 700, 44))
  assert.equal(t.timers.size, 0, 'a dwell started under the cover')
  t.advance(3 * SEEN_DWELL_MS)
  assert.deepEqual(t.all(), [])
  t.tracker.setCovered(false)
  t.advance(SEEN_DWELL_MS)
  assert.deepEqual(t.seen, [[1]], 'uncovered, the marker\'s dwell runs')
})
