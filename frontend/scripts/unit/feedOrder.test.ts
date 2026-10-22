import { test } from 'node:test'
import assert from 'node:assert/strict'
import { BASELINE_MS, countUnseen, keepIdsFor, keepSet, nextTab, partitionTab, seenKey, seenTest } from '../../src/lib/feedOrder.ts'
import { pruneIds } from '../../src/lib/prefSync.ts'
import type { Item, Partition } from '../../src/lib/feedOrder.ts'

const T0 = Date.parse('2026-10-20T12:00:00Z')
type Story = Item & { source_id: number; title: string }
const story = (id: number, fields: Partial<Story> = {}): Story => ({
  id,
  fetched_at: new Date(T0 - id * 60_000).toISOString(),
  source_id: 1,
  title: `story ${id}`,
  ...fields,
})
const ids = (list: readonly Item[]) => list.map((e) => e.id)
const seenOf = (...marked: number[]) => {
  const set = new Set(marked)
  return (e: Item) => set.has(e.id)
}
const none = seenOf()

// One tab over several polls: `step` partitions with the snapshot kept.
function tab(order = 'newest') {
  let last: Partition<Story> | null = null
  return (entries: Story[], opts: { isSeen?: (e: Item) => boolean; isRemoved?: (e: Story) => boolean; isPending?: (e: Item) => boolean; inOrder?: (e: Item) => boolean; hold?: boolean; reset?: boolean; order?: string } = {}) => {
    last = partitionTab({
      entries,
      isSeen: opts.isSeen ?? none,
      isRemoved: opts.isRemoved,
      isPending: opts.isPending,
      inOrder: opts.inOrder,
      order: opts.order ?? order,
      snapshot: last?.snapshot,
      hold: opts.hold,
      reset: opts.reset,
    })
    return last
  }
}

test('a new snapshot: unseen first, then Earlier, each in the tab order', () => {
  const list = [story(5), story(4), story(3), story(2), story(1)]
  const p = partitionTab({ entries: list, isSeen: seenOf(4, 2), order: 'newest' })
  assert.deepEqual(ids(p.unseen), [5, 3, 1])
  assert.deepEqual(ids(p.earlier), [4, 2])
  assert.equal(p.unseenCount, 3)
  assert.deepEqual(p.unseenIds, [5, 3, 1])
  assert.equal(p.snapshot.generation, 1)
})

test('a story seen after the snapshot keeps its group and place, and stops counting', () => {
  const step = tab()
  const list = [story(3), story(2), story(1)]
  step(list)
  const p = step(list, { isSeen: seenOf(2) })
  assert.deepEqual(ids(p.unseen), [3, 2, 1])
  assert.deepEqual(ids(p.earlier), [])
  assert.equal(p.unseenCount, 2)
  assert.deepEqual(p.unseenIds, [3, 1])
})

test('arrivals go at the end of the unseen ones, not at the top', () => {
  const step = tab()
  step([story(3), story(2)], { isSeen: seenOf(2) })
  const p = step([story(9), story(3), story(2)], { isSeen: seenOf(2) })
  assert.deepEqual(ids(p.unseen), [3, 9])
  assert.deepEqual(ids(p.earlier), [2])
  assert.equal(p.unseenCount, 2)
})

test('an arrival already seen goes at the end of Earlier', () => {
  const step = tab()
  step([story(3), story(2)], { isSeen: seenOf(2) })
  const p = step([story(9), story(3), story(2)], { isSeen: seenOf(2, 9) })
  assert.deepEqual(ids(p.unseen), [3])
  assert.deepEqual(ids(p.earlier), [2, 9])
})

test('arrivals wait while the marker is above the screen, and still count', () => {
  const step = tab()
  step([story(3), story(2)])
  const held = step([story(9), story(3), story(2)], { hold: true })
  assert.deepEqual(ids(held.unseen), [3, 2])
  assert.equal(held.unseenCount, 3)
  assert.deepEqual(held.unseenIds, [3, 2, 9])
  // Still held on the next poll, with a second arrival behind it.
  const again = step([story(10), story(9), story(3), story(2)], { hold: true })
  assert.deepEqual(ids(again.unseen), [3, 2])
  assert.equal(again.unseenCount, 4)
  // The marker comes back on screen: both go in, in arrival order.
  const let_go = step([story(10), story(9), story(3), story(2)])
  assert.deepEqual(ids(let_go.unseen), [3, 2, 9, 10])
})

test('PR4d: a wider tab\'s rows go in at their place in the tab order, arrivals still at the end', () => {
  // A tab at 7 days: the snapshot holds the 2-day stories; the rows the
  // wider fetch brings (older, so after them under Newest) are placed
  // where the tab's order puts them, not appended like an arrival. Here
  // 4 and 8 are the fetched rows, 1 an arrival that comes in with them.
  const step = tab()
  step([story(3), story(5), story(7)])
  const fetched = new Set([4, 8])
  const p = step([story(1), story(3), story(4), story(5), story(7), story(8)], { inOrder: (e) => fetched.has(e.id) })
  assert.deepEqual(ids(p.unseen), [3, 4, 5, 7, 8, 1])
  assert.equal(p.unseenCount, 6)
  // A fetched row already seen goes under the marker at its place too.
  const seen = seenOf(2, 6, 9)
  const q = tab()
  q([story(3), story(6), story(7)], { isSeen: seen })
  const r = q([story(2), story(3), story(6), story(7), story(9)], { isSeen: seen, inOrder: (e) => fetched.has(e.id) || e.id === 2 || e.id === 9 })
  assert.deepEqual(ids(r.unseen), [3, 7])
  assert.deepEqual(ids(r.earlier), [2, 6, 9])
})

test('PR4d: a wider tab\'s rows wait with the arrivals while the marker is above the screen, then go in at their place', () => {
  const step = tab()
  step([story(3), story(5)])
  const fetched = new Set([4])
  const held = step([story(1), story(3), story(4), story(5)], { inOrder: (e) => fetched.has(e.id), hold: true })
  assert.deepEqual(ids(held.unseen), [3, 5])
  assert.deepEqual(held.snapshot.held, [1, 4])
  assert.equal(held.unseenCount, 4)
  const let_go = step([story(1), story(3), story(4), story(5)], { inOrder: (e) => fetched.has(e.id), hold: false })
  assert.deepEqual(ids(let_go.unseen), [3, 4, 5, 1])
})

test('PR4d: a wider tab\'s row goes in at its place when the tab\'s order has moved since the snapshot', () => {
  // For You's Best is the server's ranking, and a rescore moves a Best
  // tab: between the snapshot and the next answer the top two swap, and
  // the answer brings 99 at rank 9. It goes between 8 and 9, not above
  // the first story that moved.
  const step = tab('best')
  const top = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((id) => story(id))
  step(top)
  const s = (...list: number[]) => list.map((id) => story(id))
  const p = step(s(2, 1, 3, 4, 5, 6, 7, 8, 99, 9, 10), { inOrder: (e) => e.id === 99 })
  assert.deepEqual(ids(p.unseen), [1, 2, 3, 4, 5, 6, 7, 8, 99, 9, 10])
  // A story that dropped from the top to the bottom doesn't pull a row
  // placed in the middle up with it, and one that rose from the bottom
  // doesn't push it down to the end.
  const dropped = tab('best')
  dropped(top)
  const d = dropped(s(2, 3, 4, 5, 99, 6, 7, 8, 9, 10, 1), { inOrder: (e) => e.id === 99 })
  assert.deepEqual(ids(d.unseen), [1, 2, 3, 4, 5, 99, 6, 7, 8, 9, 10])
  const rose = tab('best')
  rose(top)
  const r = rose(s(1, 2, 10, 3, 4, 99, 5, 6, 7, 8, 9), { inOrder: (e) => e.id === 99 })
  assert.deepEqual(ids(r.unseen), [1, 2, 3, 4, 99, 5, 6, 7, 8, 9, 10])
  // Arrivals stay the tail: a row the order puts last goes in before
  // them, and a later row goes in among the snapshot's stories again.
  const arrivals = tab('best')
  arrivals(s(1, 2, 3))
  arrivals(s(50, 1, 2, 3))
  const a = arrivals(s(50, 2, 1, 3, 98), { inOrder: (e) => e.id === 98 })
  assert.deepEqual(ids(a.unseen), [1, 2, 3, 98, 50])
  const b = arrivals(s(50, 2, 1, 97, 3, 98), { inOrder: (e) => e.id === 98 || e.id === 97 })
  assert.deepEqual(ids(b.unseen), [1, 2, 97, 3, 98, 50])
})

test('a story a poll drops stays, drawn from the kept copy, until the next snapshot', () => {
  const step = tab()
  step([story(3), story(2), story(1)])
  const p = step([story(3), story(1)])
  assert.deepEqual(ids(p.unseen), [3, 2, 1])
  assert.equal(p.unseen[1].title, 'story 2')
  assert.equal(p.unseenCount, 3)
  const reset = step([story(3), story(1)], { reset: true })
  assert.deepEqual(ids(reset.unseen), [3, 1])
})

test('removed stories leave at once, cached ones too, and come back in place', () => {
  const step = tab()
  step([story(3), story(2), story(1)], { isSeen: seenOf(1) })
  // 2 is hidden: the input no longer has it, and isRemoved says why.
  const hidden = new Set([2])
  const isRemoved = (e: Story) => hidden.has(e.id)
  let p = step([story(3), story(1)], { isSeen: seenOf(1), isRemoved })
  assert.deepEqual(ids(p.unseen), [3])
  assert.equal(p.unseenCount, 1)
  // A feed deleted: its seen story leaves Earlier too.
  p = step([story(3)], { isSeen: seenOf(1), isRemoved: (e) => isRemoved(e) || e.id === 1 })
  assert.deepEqual(ids(p.earlier), [])
  // The hide's Undo: the story is back where it was.
  hidden.clear()
  p = step([story(3), story(2), story(1)], { isSeen: seenOf(1), isRemoved })
  assert.deepEqual(ids(p.unseen), [3, 2])
  assert.deepEqual(ids(p.earlier), [1])
})

test('a hidden or deleted snapshot story leaves while a poll-dropped one stays', () => {
  const step = tab()
  step([story(3), story(2), story(1)])
  const p = step([story(3)], { isRemoved: (e) => e.id === 2 })
  assert.deepEqual(ids(p.unseen), [3, 1])
})

test('a removed story leaves even while the input still has it', () => {
  // isRemoved wins over the input: a story it marks (here, from a
  // deleted feed) leaves the tab and the count, seen or not, even while
  // the input still has it. App filters its inputs too, but the
  // partition doesn't rely on that.
  const step = tab()
  const gone = (e: Story) => e.source_id === 9
  step([story(4), story(3, { source_id: 9 }), story(2), story(1, { source_id: 9 })], { isSeen: seenOf(1) })
  const p = step([story(4), story(3, { source_id: 9 }), story(2), story(1, { source_id: 9 })], { isSeen: seenOf(1), isRemoved: gone })
  assert.deepEqual(ids(p.unseen), [4, 2])
  assert.deepEqual(ids(p.earlier), [])
  assert.equal(p.unseenCount, 2)
  assert.deepEqual(p.unseenIds, [4, 2])
  // A new snapshot with it still in the input: not drawn either.
  const fresh = step([story(4), story(3, { source_id: 9 }), story(2), story(1, { source_id: 9 })], { isSeen: seenOf(1), isRemoved: gone, reset: true })
  assert.deepEqual(ids(fresh.unseen), [4, 2])
  assert.deepEqual(ids(fresh.earlier), [])
  assert.equal(fresh.unseenCount, 2)
})

test('unchanged input returns the same arrays', () => {
  const step = tab()
  const list = [story(3), story(2), story(1)]
  const a = step(list, { isSeen: seenOf(1) })
  const b = step([...list], { isSeen: seenOf(1) })
  assert.equal(b.unseen, a.unseen)
  assert.equal(b.earlier, a.earlier)
  // A seen mark changes the count, not the arrays.
  const c = step(list, { isSeen: seenOf(1, 3) })
  assert.equal(c.unseen, a.unseen)
  assert.equal(c.unseenCount, 1)
})

test('the same ids with one updated story return the new story in a new array', () => {
  const step = tab()
  const a = step([story(3), story(2), story(1)])
  const updated = story(2, { title: 'story 2, edited' })
  const b = step([story(3), updated, story(1)])
  assert.notEqual(b.unseen, a.unseen)
  assert.equal(b.unseen[1], updated)
  assert.deepEqual(ids(b.unseen), [3, 2, 1])
})

test('the generation goes up on every reset, and only then', () => {
  const step = tab()
  const list = [story(2), story(1)]
  assert.equal(step(list).snapshot.generation, 1)
  assert.equal(step(list).snapshot.generation, 1)
  assert.equal(step(list, { reset: true }).snapshot.generation, 2)
  assert.equal(step([story(9), ...list]).snapshot.generation, 2)
})

test('a reset re-sorts: what was seen goes under the marker, arrivals into their place', () => {
  const step = tab()
  step([story(3), story(2), story(1)])
  step([story(9), story(3), story(2), story(1)], { isSeen: seenOf(3) })
  const p = step([story(9), story(3), story(2), story(1)], { isSeen: seenOf(3), reset: true })
  assert.deepEqual(ids(p.unseen), [9, 2, 1])
  assert.deepEqual(ids(p.earlier), [3])
})

test('a change of the tab order (Sort; from PR4d the window) starts a new snapshot', () => {
  const step = tab('newest')
  step([story(3), story(2), story(1)])
  step([story(3), story(2), story(1)], { isSeen: seenOf(2) })
  const best = step([story(1), story(3), story(2)], { isSeen: seenOf(2), order: 'best' })
  assert.equal(best.snapshot.generation, 2)
  assert.deepEqual(ids(best.unseen), [1, 3])
  assert.deepEqual(ids(best.earlier), [2])
  // The same order again keeps it.
  assert.equal(step([story(1), story(3), story(2)], { isSeen: seenOf(2), order: 'best' }).snapshot.generation, 2)
  // A window change, as PR4d encodes it in the order string.
  assert.equal(step([story(1)], { order: 'best|7d' }).snapshot.generation, 3)
})

// PR3c: a story seen on scroll waits a moment in App's collector before
// its mark is written. A snapshot started meanwhile places it as seen;
// the counts wait for the mark.
test('a story seen and not yet marked goes under the marker of a new snapshot, and stays there', () => {
  const step = tab()
  const list = [story(4), story(3), story(2), story(1)]
  step(list)
  // Pending in the snapshot it was seen in: it keeps its place and still
  // counts (one written batch, one change of the counts).
  const kept = step(list, { isPending: seenOf(3) })
  assert.deepEqual(ids(kept.unseen), [4, 3, 2, 1])
  assert.equal(kept.unseenCount, 4)
  // A reset (a tab switch, Refresh, Sort) while it is still pending.
  const reset = step(list, { isPending: seenOf(3), reset: true })
  assert.deepEqual(ids(reset.unseen), [4, 2, 1])
  assert.deepEqual(ids(reset.earlier), [3])
  assert.equal(reset.unseenCount, 3)
  assert.deepEqual(reset.unseenIds, [4, 2, 1])
  // Its mark lands: nothing moves.
  const marked = step(list, { isSeen: seenOf(3) })
  assert.deepEqual(ids(marked.unseen), [4, 2, 1])
  assert.deepEqual(ids(marked.earlier), [3])
  // A tab's first snapshot (one not drawn until now) places it the same.
  const other = tab()
  const first = other(list, { isPending: seenOf(2) })
  assert.deepEqual(ids(first.unseen), [4, 3, 1])
  assert.deepEqual(ids(first.earlier), [2])
  // And an arrival still pending goes at the end of Earlier.
  const arrived = other([story(9), ...list], { isPending: seenOf(2, 9) })
  assert.deepEqual(ids(arrived.unseen), [4, 3, 1])
  assert.deepEqual(ids(arrived.earlier), [2, 9])
})

test('D33: stories fetched more than 48 h before the first load count as seen', () => {
  const firstRunAt = T0
  const old = { id: 1, fetched_at: new Date(T0 - BASELINE_MS - 60_000).toISOString() }
  const edge = { id: 2, fetched_at: new Date(T0 - BASELINE_MS + 60_000).toISOString() }
  const undated = { id: 3, fetched_at: null }
  const bad = { id: 4, fetched_at: 'not a date' }
  const seen = seenTest(new Set([5]), firstRunAt)
  assert.equal(seen(old), true)
  assert.equal(seen(edge), false)
  assert.equal(seen(undated), false)
  assert.equal(seen(bad), false)
  assert.equal(seen({ id: 5, fetched_at: null }), true)
  // No first-run time (storage refused it and the load time stands in):
  // marks only.
  assert.equal(seenTest(new Set(), null)(old), false)
  // In a partition, the old story is under the marker from the start.
  const p = partitionTab({ entries: [story(2), { ...story(1), ...old }], isSeen: seen, order: 'newest' })
  assert.deepEqual(ids(p.earlier), [1])
  assert.equal(p.unseenCount, 1)
})

test('countUnseen counts each story not seen once', () => {
  assert.equal(countUnseen([story(3), story(2), story(2), story(1)], seenOf(1)), 2)
  assert.equal(countUnseen([], none), 0)
})

test('seenKey: the category, as typed (slashes included)', () => {
  assert.equal(seenKey('news'), 'seen:news')
  assert.equal(seenKey('News/Tech'), 'seen:News/Tech')
})

test('nextTab (C8): the next shown tab with new stories, wrapping, never the current one', () => {
  const tabs = [
    { key: 'meta:foryou', label: 'For You', count: 0 },
    { key: 'cat:news', label: 'News', count: 0 },
    { key: 'cat:deals', label: 'Deals', count: 4 },
    { key: 'cat:nhl', label: 'NHL', count: 2 },
  ]
  assert.equal(nextTab(tabs, 'cat:news')?.key, 'cat:deals')
  assert.equal(nextTab(tabs, 'cat:deals')?.key, 'cat:nhl')
  // Past the last tab it wraps round to the first with a count.
  assert.equal(nextTab(tabs, 'cat:nhl')?.key, 'cat:deals')
  // Every other tab at 0: none, so the end of the tab is the end.
  assert.equal(nextTab(tabs.map((t) => ({ ...t, count: t.key === 'cat:nhl' ? 3 : 0 })), 'cat:nhl'), null)
  // A current tab that isn't in the list (hidden) starts from the first.
  assert.equal(nextTab(tabs, 'cat:gone')?.key, 'cat:deals')
  assert.equal(nextTab([], 'cat:news'), null)
})

test('keepSet: every id App holds, from stories and bare ids, each once', () => {
  const cache = new Map([[3, story(3)]])
  const keep = keepSet([story(10), story(11)], [story(11), story(12)], [12, 20], cache.keys())
  assert.deepEqual([...keep].sort((a, b) => a - b), [3, 10, 11, 12, 20])
  assert.equal(keepSet().size, 0)
})

test('keepIdsFor (PR4c, PR4d): the keep set is the tabs, For You, the saves, the Saved-by-id cache and the wider tabs\' rows', () => {
  // App's call passes what it holds; flow-seen-prune-keeps-saved checks
  // what App passes (a save and a cached story keep their marks).
  // Each group holds one id no other does: 10 only a tab, 12 only For
  // You, 30 only the saves (its by-id fetch is still out), 3 only the
  // cache (unsaved since it was fetched), 40 only a wider tab's rows.
  const held = {
    entries: [story(10), story(11)],
    forYou: [story(11), story(12)],
    starred: [20, 30],
    savedById: new Map([[3, story(3)], [20, story(20)]]),
    extra: [story(40), story(10)],
  }
  assert.deepEqual([...keepIdsFor(held)].sort((a, b) => a - b), [3, 10, 11, 12, 20, 30, 40])
})

test('keepIdsFor (PR4c): a story unsaved but still in the Saved-by-id cache keeps its seen mark when the key is pruned', () => {
  // 3 was fetched by id for Saved (no tab holds it), then unsaved: it is
  // no save now, but the unsave's Undo would draw it again from the cache.
  // 7 is still saved, so the saves keep it whatever the cache holds.
  const entries = [story(900), story(901)]
  const starred = [7]
  const savedById = new Map([[3, story(3)], [7, story(7)]])
  const marks = [3, 5, 7, 900, 901]
  // seen:news over its cap (4 here) by one: the smallest mark outside the
  // keep set goes, and 3 stays.
  const kept = pruneIds(marks, keepIdsFor({ entries, forYou: [], starred, savedById, extra: [] }), 4)
  assert.deepEqual(kept.dropped, [5])
  assert.deepEqual(kept.ids, [3, 7, 900, 901])
  // Without the cache, 3 (the smallest) would go first, and the story its
  // Undo brings back would count as unseen again; 7 stays either way.
  const noCache = pruneIds(marks, keepIdsFor({ entries, forYou: [], starred, savedById: new Map(), extra: [] }), 4)
  assert.deepEqual(noCache.dropped, [3])
})
