import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  BRIEF_KEY,
  FILTER_KEY,
  FORYOU_KEY,
  SAVED_KEY,
  colKey,
  freshTabIndex,
  parseColKey,
  readStoredTab,
  resolveTab,
} from '../../src/lib/columns.ts'
import type { ColKey, Tab } from '../../src/lib/columns.ts'

const tab = (key: ColKey, size = 3): Tab => ({ key, size })
const NEWS = colKey('news')
const DEALS = colKey('deals')
const NHL = colKey('nhl')
const VIDEO = colKey('video')

const LOADED = { loaded: true, filterActive: false }
const LOADING = { loaded: false, filterActive: false }
const FILTERED = { loaded: true, filterActive: true }

// The strip as App builds it: Brief, then the columns.
const strip = (...keys: ColKey[]) => [tab(BRIEF_KEY, 0), ...keys.map((k) => tab(k))]

test('keys: meta keys and cat:<name> parse, anything else does not', () => {
  for (const k of ['meta:brief', 'meta:saved', 'meta:foryou', 'meta:search', 'meta:filter', 'cat:news', 'cat:Hockey Night']) {
    assert.equal(parseColKey(k), k)
  }
  assert.equal(colKey('news'), 'cat:news')
  for (const k of [null, undefined, 3, '', 'cat:', 'news', 'meta:other', 'Saved', `cat:${'x'.repeat(300)}`]) {
    assert.equal(parseColKey(k), null, String(k))
  }
})

test('stored: a key wins over the old index; a bad index reads as nothing', () => {
  assert.deepEqual(readStoredTab('cat:news', '3'), { key: 'cat:news' })
  assert.deepEqual(readStoredTab(null, '3'), { index: 3 })
  assert.deepEqual(readStoredTab('garbage', '-1'), { index: -1 })
  assert.deepEqual(readStoredTab(null, '0'), { index: 0 })
  for (const raw of [null, '', '-2', '1.5', 'x']) assert.equal(readStoredTab(null, raw), null, String(raw))
})

test('a wanted key in the strip is shown and nothing is stored', () => {
  const tabs = strip(SAVED_KEY, FORYOU_KEY, NEWS)
  assert.deepEqual(resolveTab(tabs, { key: NEWS }, 1, LOADED), { key: NEWS, index: 3, store: null })
  assert.deepEqual(resolveTab(tabs, { key: BRIEF_KEY }, null, LOADING), { key: BRIEF_KEY, index: 0, store: null })
})

test('Saved appearing in front of the shown tab does not move it', () => {
  // On News (index 2), then a save adds Saved at index 1.
  const before = resolveTab(strip(FORYOU_KEY, NEWS), { key: NEWS }, null, LOADED)
  assert.equal(before.index, 2)
  const after = resolveTab(strip(SAVED_KEY, FORYOU_KEY, NEWS), { key: NEWS }, before.index, LOADED)
  assert.deepEqual(after, { key: NEWS, index: 3, store: null })
})

test('a tab that goes is replaced by its neighbour, which becomes the stored tab once loaded', () => {
  // NHL (index 3) empties; the tab now at index 3 is the one after it.
  const r = resolveTab(strip(FORYOU_KEY, DEALS, VIDEO), { key: NHL }, 3, LOADED)
  assert.deepEqual(r, { key: VIDEO, index: 3, store: VIDEO })
  // The last tab goes: clamp to the new last one.
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, DEALS), { key: NHL }, 3, LOADED), { key: DEALS, index: 2, store: DEALS })
  // Unsaving the only saved story: Saved (index 1) goes, For You takes its place.
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: SAVED_KEY }, 1, LOADED), { key: FORYOU_KEY, index: 1, store: FORYOU_KEY })
})

test('with only Brief left, a tab that went is not replaced by Brief', () => {
  // Every story hidden or every feed deleted: the strip is Brief alone.
  assert.deepEqual(resolveTab([tab(BRIEF_KEY, 0)], { key: NEWS }, 2, LOADED), { key: BRIEF_KEY, index: 0, store: null })
  // When stories come back, the stored tab is shown again.
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: NEWS }, 0, LOADED), { key: NEWS, index: 2, store: null })
})

test('the stored key is kept while loading and under the filter', () => {
  // Saved hasn't loaded yet: show the neighbour, keep meta:saved.
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: SAVED_KEY }, 1, LOADING), { key: FORYOU_KEY, index: 1, store: null })
  // The filter's one column replaces the tabs; News comes back after.
  assert.deepEqual(resolveTab(strip(FILTER_KEY), { key: NEWS }, 3, FILTERED), { key: FILTER_KEY, index: 1, store: null })
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: NEWS }, 1, LOADED), { key: NEWS, index: 2, store: null })
})

test('a missing key with no last place uses the fresh-device tab', () => {
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: NHL }, null, LOADED), { key: FORYOU_KEY, index: 1, store: FORYOU_KEY })
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: NHL }, null, LOADING), { key: FORYOU_KEY, index: 1, store: null })
})

test('an old index migrates to the key at that place; -1 is Brief', () => {
  const tabs = strip(SAVED_KEY, FORYOU_KEY, NEWS, VIDEO)
  assert.deepEqual(resolveTab(tabs, { index: 2 }, null, LOADED), { key: NEWS, index: 3, store: NEWS })
  assert.deepEqual(resolveTab(tabs, { index: 0 }, null, LOADED), { key: SAVED_KEY, index: 1, store: SAVED_KEY })
  assert.deepEqual(resolveTab(tabs, { index: -1 }, null, LOADED), { key: BRIEF_KEY, index: 0, store: BRIEF_KEY })
  // Past the end: the last tab, as the old clamp did.
  assert.deepEqual(resolveTab(tabs, { index: 9 }, null, LOADED), { key: VIDEO, index: 4, store: VIDEO })
})

test('no migration before the first refresh, even with meta tabs present', () => {
  // Before the first refresh only Brief is there; preferences may
  // already have brought Saved.
  assert.equal(resolveTab(strip(), { index: 2 }, null, LOADING).store, null)
  assert.equal(resolveTab(strip(SAVED_KEY), { index: 2 }, null, LOADING).store, null)
  assert.equal(resolveTab(strip(SAVED_KEY, FORYOU_KEY), { index: -1 }, null, LOADING).store, null)
  // Nor under the filter.
  assert.equal(resolveTab(strip(FILTER_KEY), { index: 2 }, null, FILTERED).store, null)
  // Nor a fresh device's pick.
  assert.equal(resolveTab(strip(SAVED_KEY, FORYOU_KEY), null, null, LOADING).store, null)
})

test('a fresh device opens on For You, skipping an empty Saved and Brief', () => {
  const tabs = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 5), tab(NEWS)]
  assert.deepEqual(resolveTab(tabs, null, null, LOADED), { key: FORYOU_KEY, index: 2, store: FORYOU_KEY })
  // For You empty: the first tab after Brief with stories, Saved included.
  assert.equal(freshTabIndex([tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 0), tab(NEWS)]), 3)
  assert.equal(freshTabIndex([tab(BRIEF_KEY, 0), tab(SAVED_KEY, 2), tab(NEWS)]), 1)
  // Nothing anywhere: For You, else the first tab after Brief; only
  // Brief: Brief.
  assert.equal(freshTabIndex([tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 0), tab(NEWS, 0)]), 2)
  assert.equal(freshTabIndex([tab(BRIEF_KEY, 0), tab(NEWS, 0)]), 1)
  assert.equal(freshTabIndex([tab(BRIEF_KEY, 0)]), 0)
})

test('an empty strip shows nothing', () => {
  assert.deepEqual(resolveTab([], { key: NEWS }, 2, LOADED), { key: null, index: -1, store: null })
})

test('a fresh pick with nothing to show is not stored', () => {
  // A new install before its first stories: only Brief and empty tabs.
  const empty = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 0)]
  assert.deepEqual(resolveTab(empty, null, null, LOADED), { key: FORYOU_KEY, index: 2, store: null })
  assert.deepEqual(resolveTab([tab(BRIEF_KEY, 0)], null, null, LOADED), { key: BRIEF_KEY, index: 0, store: null })
  assert.equal(resolveTab([tab(BRIEF_KEY, 0)], { key: NHL }, null, LOADED).store, null)
  assert.equal(resolveTab([tab(BRIEF_KEY, 0)], { index: 2 }, null, LOADED).store, null)
  // Once For You fills, it is picked and stored.
  const filled = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 4)]
  assert.deepEqual(resolveTab(filled, null, null, LOADED), { key: FORYOU_KEY, index: 2, store: FORYOU_KEY })
  // The neighbour of a tab that went is stored even when it is empty:
  // it is the place the phone was.
  assert.equal(resolveTab([tab(BRIEF_KEY, 0), tab(FORYOU_KEY), tab(NEWS, 0)], { key: NHL }, 2, LOADED).store, NEWS)
})

test('a stored filter column that went gets the fresh-device tab, not its old place', () => {
  assert.deepEqual(resolveTab(strip(SAVED_KEY, FORYOU_KEY, NEWS), { key: FILTER_KEY }, 1, LOADED), {
    key: FORYOU_KEY,
    index: 2,
    store: FORYOU_KEY,
  })
  // While the filter is on, it is shown and nothing is stored.
  assert.deepEqual(resolveTab(strip(FILTER_KEY), { key: FILTER_KEY }, 1, FILTERED), { key: FILTER_KEY, index: 1, store: null })
})

// A save adds Saved at index 1, in front of whatever stands in.
const held = (standIn: ColKey | null, loaded = false) => ({ loaded, filterActive: false, standIn })

test('a stand-in is held by key until the list settles, so a save in front of it does not move it', () => {
  // A stored For You that is down: its neighbour at index 1 stands in.
  const first = resolveTab(strip(NEWS, DEALS), { key: FORYOU_KEY }, 1, LOADING)
  assert.deepEqual(first, { key: NEWS, index: 1, store: null })
  const starred = strip(SAVED_KEY, NEWS, DEALS)
  assert.equal(resolveTab(starred, { key: FORYOU_KEY }, 1, LOADING).key, SAVED_KEY, 'by place it would move')
  assert.deepEqual(resolveTab(starred, { key: FORYOU_KEY }, 1, held(first.key)), { key: NEWS, index: 2, store: null })

  // A fresh device: the first tab with stories stands in.
  const fresh = resolveTab(strip(DEALS, NEWS), null, null, LOADING)
  assert.equal(fresh.key, DEALS)
  assert.deepEqual(resolveTab(strip(SAVED_KEY, DEALS, NEWS), null, null, held(fresh.key)), { key: DEALS, index: 2, store: null })

  // An old index: the tab at that place stands in.
  const legacy = resolveTab(strip(DEALS, NEWS), { index: 1 }, null, LOADING)
  assert.equal(legacy.key, NEWS)
  assert.deepEqual(resolveTab(strip(SAVED_KEY, DEALS, NEWS), { index: 1 }, null, held(legacy.key)), { key: NEWS, index: 3, store: null })

  // Under the filter too.
  assert.deepEqual(resolveTab(strip(SAVED_KEY, FILTER_KEY), { key: NEWS }, 1, { ...FILTERED, standIn: FILTER_KEY }), {
    key: FILTER_KEY,
    index: 2,
    store: null,
  })
})

test('a held stand-in gives way to the stored tab, to a settled list, and when it goes', () => {
  // The stored tab comes back (For You loads): it is shown.
  assert.deepEqual(resolveTab(strip(FORYOU_KEY, NEWS), { key: FORYOU_KEY }, 1, held(NEWS)), { key: FORYOU_KEY, index: 1, store: null })
  // Settled: the rules apply again and pick the tab to store.
  assert.deepEqual(resolveTab(strip(SAVED_KEY, FORYOU_KEY, NEWS), null, null, held(NEWS, true)), {
    key: FORYOU_KEY,
    index: 2,
    store: FORYOU_KEY,
  })
  assert.deepEqual(resolveTab(strip(SAVED_KEY, FORYOU_KEY, NEWS), { index: 0 }, null, held(NEWS, true)), {
    key: SAVED_KEY,
    index: 1,
    store: SAVED_KEY,
  })
  // The stand-in itself went: back to the place-based pick.
  assert.deepEqual(resolveTab(strip(DEALS, VIDEO), { key: FORYOU_KEY }, 1, held(NEWS)), { key: DEALS, index: 1, store: null })
  // Brief is never held.
  assert.deepEqual(resolveTab(strip(DEALS, NEWS), null, null, held(BRIEF_KEY)), { key: DEALS, index: 1, store: null })
})

test('no migration before the first refresh while meta tabs are present', () => {
  // From PR7a Saved and For You are always in the strip, empty until
  // the preferences and For You arrive: the strip is never empty, so
  // only the loaded flag (the first refresh and the preferences) keeps
  // an old index or a fresh pick from being stored.
  const early = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 0)]
  assert.equal(resolveTab(early, { index: 0 }, null, LOADING).store, null)
  assert.equal(resolveTab(early, { index: -1 }, null, LOADING).store, null)
  assert.equal(resolveTab(early, null, null, LOADING).store, null)
  assert.equal(resolveTab([...early, tab(NEWS)], { index: 0 }, null, LOADING).store, null)
  assert.equal(resolveTab([...early, tab(NEWS)], null, null, LOADING).store, null)
})

test('an old index counts an empty Saved or For You out, as the strip it was stored on did', () => {
  // Stored when the strip was For You, News, Video (nothing saved): 1 is News.
  const tabs = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 4), tab(NEWS), tab(VIDEO)]
  assert.deepEqual(resolveTab(tabs, { index: 1 }, null, LOADED), { key: NEWS, index: 3, store: NEWS })
  // For You down too: 0 is News.
  const down = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 0), tab(FORYOU_KEY, 0), tab(NEWS), tab(VIDEO)]
  assert.deepEqual(resolveTab(down, { index: 0 }, null, { loaded: false, filterActive: false }), { key: NEWS, index: 3, store: null })
  // With stories in Saved it counts, as it did.
  const saved = [tab(BRIEF_KEY, 0), tab(SAVED_KEY, 1), tab(FORYOU_KEY, 4), tab(NEWS)]
  assert.deepEqual(resolveTab(saved, { index: 0 }, null, LOADED), { key: SAVED_KEY, index: 1, store: SAVED_KEY })
})
