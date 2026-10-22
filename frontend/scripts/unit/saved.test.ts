import { test } from 'node:test'
import assert from 'node:assert/strict'
import { placeOf, putBack, savedStories, savedToFetch, type Place } from '../../src/lib/saved.ts'

type Story = { id: number; title: string }
const story = (id: number): Story => ({ id, title: `story ${id}` })
const ids = (list: readonly { id: number }[]) => list.map((e) => e.id)
const byId = (...list: number[]) => {
  const m = new Map(list.map((id) => [id, story(id)]))
  return (id: number) => m.get(id)
}
const never = () => false

test('savedStories: newest save first (the last id saved leads)', () => {
  // Saved in the order 105, 503, 103: 103 is the newest save.
  assert.deepEqual(ids(savedStories([105, 503, 103], byId(103, 105, 503), never)), [103, 503, 105])
})

test('savedStories: a save with no copy yet is left out until it has one', () => {
  assert.deepEqual(ids(savedStories([1, 2, 3], byId(1, 3), never)), [3, 1])
})

test('savedStories: hide wins over saved (isRemoved)', () => {
  const hidden = new Set([2])
  assert.deepEqual(ids(savedStories([1, 2, 3], byId(1, 2, 3), (e) => hidden.has(e.id))), [3, 1])
})

test('savedStories: an id stored twice is drawn once, at its newest place', () => {
  assert.deepEqual(ids(savedStories([1, 2, 1], byId(1, 2), never)), [1, 2])
  assert.deepEqual(savedStories([], byId(1), never), [])
})

test('savedToFetch: newest save first, only the ids skip leaves', () => {
  const held = new Set([2, 4])
  assert.deepEqual(savedToFetch([1, 2, 3, 4, 5], (id) => held.has(id)), [5, 3, 1])
  assert.deepEqual(savedToFetch([1, 2], () => true), [])
  // An id stored twice is asked for once.
  assert.deepEqual(savedToFetch([7, 8, 7], never), [7, 8])
})

// A list of stories by id, and putBack's items from [id, place] pairs.
const list = (...xs: number[]) => xs.map(story)
const back = (...xs: [number, Place][]) => xs.map(([id, place]) => ({ item: story(id), place }))
const place = (next: number[], prev: number[], at: number): Place => ({ next, prev, at })
const none = new Set<number>()
const hiddenIn = (set: ReadonlySet<number>) => (id: number) => set.has(id)
// The place App keeps for a story of `l` (keepHiddenStories): taken with
// the stories kept for their Undo back in their places.
const keep = (l: Story[], id: number, hidden: ReadonlySet<number>, kept: ReadonlyMap<number, Place> = new Map()) => {
  const all = putBack(l, [...kept].map(([k, p]) => ({ item: story(k), place: p })), (k) => kept.get(k))
  return placeOf(all, all.findIndex((e) => e.id === id), hiddenIn(hidden))
}

test('placeOf: the stories next to it, nearest first, up to the first one not hidden, and the index', () => {
  const l = list(1, 2, 3, 4, 5)
  assert.deepEqual(placeOf(l, 2, hiddenIn(none)), place([4], [2], 2))
  // A story hidden before is kept in the run, which goes on to the next
  // one not hidden: a poll may drop the hidden one, or its Undo bring it
  // back first.
  assert.deepEqual(placeOf(l, 2, hiddenIn(new Set([2, 4]))), place([4, 5], [2, 1], 2))
  // At either end the run is empty; with only hidden stories, it holds them all.
  assert.deepEqual(placeOf(l, 0, hiddenIn(none)), place([2], [], 0))
  assert.deepEqual(placeOf(l, 4, hiddenIn(new Set([1, 2, 3, 4]))), place([], [4, 3, 2, 1], 4))
})

test('putBack: before its next story, else after its previous one, else at its index', () => {
  const p = place([3], [1], 1)
  assert.deepEqual(ids(putBack(list(1, 3, 4), back([9, p]))), [1, 9, 3, 4])
  // Stories above it went since the hide: still just before 3.
  assert.deepEqual(ids(putBack(list(3, 4), back([9, p]))), [9, 3, 4])
  // Its next story went too: just after 1.
  assert.deepEqual(ids(putBack(list(0, 1, 4), back([9, p]))), [0, 1, 9, 4])
  // The nearest of a run that the list holds.
  assert.deepEqual(ids(putBack(list(1, 5, 6), back([9, place([3, 5], [1], 1)]))), [1, 9, 5, 6])
  // Neither side is there: its index, clamped to the list.
  assert.deepEqual(ids(putBack(list(5, 6, 7), back([9, p]))), [5, 9, 6, 7])
  assert.deepEqual(ids(putBack(list(5), back([9, place([3], [1], 10)]))), [5, 9])
  assert.deepEqual(ids(putBack(list(5), back([9, place([], [], -1)]))), [9, 5])
  assert.deepEqual(ids(putBack([], back([9, p]))), [9])
})

test('putBack: a list that holds every story already comes back as it is, and the input is never changed', () => {
  const l = list(1, 9, 3)
  assert.equal(putBack(l, back([9, place([3], [1], 1)])), l)
  assert.equal(putBack(l, []), l)
  const m = list(1, 3)
  putBack(m, back([9, place([3], [1], 1)]))
  assert.deepEqual(ids(m), [1, 3])
})

// For You as the fixtures serve it: 101, 301, 102, 201, 401.
test('putBack: two hides, a poll, then Undo of the second puts it back where it was', () => {
  const fy = list(101, 301, 102, 201, 401)
  const p101 = keep(fy, 101, none)
  // No poll between the hides: the list still holds the hidden 101.
  const p102 = keep(fy, 102, new Set([101]), new Map([[101, p101]]))
  assert.deepEqual(p102, place([201], [301], 2))
  // The poll drops both; Undo on 102's toast. Its index alone (2) would
  // put it after 201.
  assert.deepEqual(ids(putBack(list(301, 201, 401), back([102, p102]), (id) => (id === 101 ? p101 : undefined))), [301, 102, 201, 401])
})

test('putBack: Restore all, or one at a time in either order, of two hides with a poll between them', () => {
  const p101 = keep(list(101, 301, 102, 201, 401), 101, none)
  // The poll dropped 101 before 102 was hidden.
  const p102 = keep(list(301, 102, 201, 401), 102, new Set([101]), new Map([[101, p101]]))
  const polled = list(301, 201, 401)
  const want = [101, 301, 102, 201, 401]
  // Restore all, in either order.
  assert.deepEqual(ids(putBack(polled, back([102, p102], [101, p101]))), want)
  assert.deepEqual(ids(putBack(polled, back([101, p101], [102, p102]))), want)
  // Restore on each row, oldest hide first (101, then 102), or the other way.
  const only = (id: number, p: Place) => (k: number) => (k === id ? p : undefined)
  assert.deepEqual(ids(putBack(putBack(polled, back([101, p101]), only(102, p102)), back([102, p102]))), want)
  assert.deepEqual(ids(putBack(putBack(polled, back([102, p102]), only(101, p101)), back([101, p101]))), want)
})

test('putBack: two hides next to each other, a poll after each, come back in either order', () => {
  // 102, then 301 just above it; the poll after the first hide dropped 102.
  const p102 = keep(list(101, 301, 102, 201), 102, none)
  const p301 = keep(list(101, 301, 201), 301, new Set([102]), new Map([[102, p102]]))
  // Taken with 102 back in its place, 301's run holds it.
  assert.deepEqual(p301, place([102, 201], [101], 1))
  const polled = list(101, 201)
  const want = [101, 301, 102, 201]
  const only = (id: number, p: Place) => (k: number) => (k === id ? p : undefined)
  assert.deepEqual(ids(putBack(putBack(polled, back([102, p102]), only(301, p301)), back([301, p301]))), want)
  assert.deepEqual(ids(putBack(putBack(polled, back([301, p301]), only(102, p102)), back([102, p102]))), want)
  assert.deepEqual(ids(putBack(polled, back([301, p301], [102, p102]))), want)
  assert.deepEqual(ids(putBack(polled, back([102, p102], [301, p301]))), want)
})

test('putBack: stories put back together wait for each other, in any order given', () => {
  // A bulk hide of 2 and 3 together: each is the other's neighbour.
  const l = list(1, 2, 3, 4, 5)
  const p2 = placeOf(l, 1, hiddenIn(none))
  const p3 = placeOf(l, 2, hiddenIn(none))
  assert.deepEqual(ids(putBack(list(1, 4, 5), back([2, p2], [3, p3]))), [1, 2, 3, 4, 5])
  assert.deepEqual(ids(putBack(list(1, 4, 5), back([3, p3], [2, p2]))), [1, 2, 3, 4, 5])
  // Their other neighbour (1) went too: 2 waits for 3 instead of going
  // by its index.
  assert.deepEqual(ids(putBack(list(4, 5), back([2, p2], [3, p3]))), [2, 3, 4, 5])
  // Two whose places disagree (For You reordered between the hides): both
  // still go back, the first by its index.
  const out = putBack(list(10, 11), back([7, place([8], [], 0)], [8, place([7], [], 5)]))
  assert.deepEqual(ids(out), [8, 7, 10, 11])
})

test('putBack: a run the list holds none of goes on along the run of a kept story', () => {
  // 3 hidden, then 2, then 4; a poll drops all three; 3 alone comes back.
  const l = list(1, 2, 3, 4, 5)
  const p3 = keep(l, 3, none)
  const p2 = keep(l, 2, new Set([3]), new Map([[3, p3]]))
  const p4 = keep(l, 4, new Set([2, 3]), new Map([[3, p3], [2, p2]]))
  const kept = new Map([[2, p2], [4, p4]])
  assert.deepEqual(p3, place([4], [2], 2))
  assert.deepEqual(ids(putBack(list(1, 5), back([3, p3]), (id) => kept.get(id))), [1, 3, 5])
  // Without the kept places only its index is left: a place off.
  assert.deepEqual(ids(putBack(list(1, 5), back([3, p3]))), [1, 5, 3])
})
