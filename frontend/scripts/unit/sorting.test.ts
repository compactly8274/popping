import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  BEST_PREFS,
  NEWEST_PREFS,
  byBest,
  byNewest,
  defaultSort,
  entryTime,
  normalizeSort,
  prefsFor,
  sortEntries,
} from '../../src/lib/sorting.ts'
import type { Sortable, TabPrefs } from '../../src/lib/sorting.ts'
import { BRIEF_KEY, FILTER_KEY, FORYOU_KEY, SAVED_KEY, colKey } from '../../src/lib/columns.ts'

const NEWS = colKey('news')
const H = 3600_000
const T0 = Date.parse('2026-10-20T12:00:00Z')
const at = (hoursAgo: number) => new Date(T0 - hoursAgo * H).toISOString()

const story = (id: number, fields: Partial<Sortable> = {}): Sortable => ({
  id,
  published_at: null,
  fetched_at: null,
  composite_score: 0,
  ...fields,
})
const ids = (list: readonly Sortable[]) => list.map((e) => e.id)

test('entryTime: published, else fetched, else null', () => {
  assert.equal(entryTime(story(1, { published_at: at(1), fetched_at: at(0) })), T0 - H)
  assert.equal(entryTime(story(1, { fetched_at: at(2) })), T0 - 2 * H)
  assert.equal(entryTime(story(1)), null)
  assert.equal(entryTime(story(1, { published_at: 'not a date' })), null)
})

test('byNewest: newest first, dated before undated, then the higher id', () => {
  const list = [
    story(1, { published_at: at(5) }),
    story(2),
    story(3, { published_at: at(1) }),
    story(4, { fetched_at: at(3) }), // undated by its publisher, dated by its fetch
    story(5),
    story(6, { published_at: at(1) }), // same time as 3
  ]
  assert.deepEqual(ids([...list].sort(byNewest)), [6, 3, 4, 1, 5, 2])
})

test('byBest: highest composite_score first, then byNewest (undated last)', () => {
  const list = [
    story(1, { composite_score: 40, published_at: at(9) }),
    story(2, { composite_score: 90 }),
    story(3, { composite_score: 90, published_at: at(2) }),
    story(4, { composite_score: 40, published_at: at(1) }),
    story(5, { composite_score: 10, published_at: at(0) }),
    story(6, { composite_score: 40 }),
  ]
  assert.deepEqual(ids([...list].sort(byBest)), [3, 2, 4, 1, 6, 5])
})

test('For You keeps the server order on Best: a boosted story with a lower raw score stays first', () => {
  // /api/foryou ranks by composite_score x a convergence boost but sends
  // the raw score; 7 was boosted past 8 and 9.
  const server = [
    story(7, { composite_score: 30, published_at: at(6) }),
    story(8, { composite_score: 80, published_at: at(1) }),
    story(9, { composite_score: 60, published_at: at(3) }),
  ]
  assert.deepEqual(ids(sortEntries(server, 'best', FORYOU_KEY)), [7, 8, 9])
  assert.deepEqual(ids(server), [7, 8, 9], 'the input is not reordered')
  // Newest on For You is by time.
  assert.deepEqual(ids(sortEntries(server, 'newest', FORYOU_KEY)), [8, 9, 7])
})

test('sortEntries: categories and the filter re-sort by score on Best; Saved is never sorted', () => {
  const list = [
    story(1, { composite_score: 10, published_at: at(0) }),
    story(2, { composite_score: 70, published_at: at(4) }),
    story(3, { composite_score: 50, published_at: at(2) }),
  ]
  assert.deepEqual(ids(sortEntries(list, 'best', NEWS)), [2, 3, 1])
  assert.deepEqual(ids(sortEntries(list, 'best', FILTER_KEY)), [2, 3, 1])
  assert.deepEqual(ids(sortEntries(list, 'newest', NEWS)), [1, 3, 2])
  assert.equal(sortEntries(list, 'best', SAVED_KEY), list)
  assert.equal(sortEntries(list, 'newest', SAVED_KEY), list)
  assert.deepEqual(ids(list), [1, 2, 3], 'the input is not reordered')
})

test('defaultSort and normalizeSort per key', () => {
  assert.equal(defaultSort(FORYOU_KEY), 'best')
  for (const key of [NEWS, SAVED_KEY, FILTER_KEY, BRIEF_KEY]) assert.equal(defaultSort(key), 'newest')
  for (const key of [FORYOU_KEY, NEWS, FILTER_KEY]) {
    assert.equal(normalizeSort('newest', key), 'newest')
    assert.equal(normalizeSort('best', key), 'best')
  }
  // Older builds' values and anything unknown read as the tab's default.
  for (const old of ['top', 'oldest', undefined, null, 'random', 3]) {
    assert.equal(normalizeSort(old, FORYOU_KEY), 'best', String(old))
    assert.equal(normalizeSort(old, NEWS), 'newest', String(old))
    assert.equal(normalizeSort(old, FILTER_KEY), 'newest', String(old))
  }
})

const FY = { key: FORYOU_KEY, name: 'For You' }
const NEWS_TAB = { key: NEWS, name: 'news' }

test('prefsFor: a tab with no row gets its default as a module constant', () => {
  assert.equal(prefsFor({}, FY), BEST_PREFS)
  assert.equal(prefsFor({}, NEWS_TAB), NEWEST_PREFS)
  assert.equal(prefsFor({ news: { sort: 'best', minScore: 0, window: '2d' } }, FY), BEST_PREFS)
  // Rows are keyed by the column's name; inherited names don't count.
  assert.equal(prefsFor({}, { key: colKey('toString'), name: 'toString' }), NEWEST_PREFS)
})

test('prefsFor: a write from a tab with no row stores its sort, never top', () => {
  // What Column writes: a spread of the prefs it was given.
  const windowWrite: TabPrefs = { ...prefsFor({}, FY), window: '7d' }
  assert.equal(windowWrite.sort, 'best')
  const sortWrite: TabPrefs = { ...prefsFor({}, FY), sort: 'newest' }
  assert.equal(sortWrite.sort, 'newest')
  assert.equal({ ...prefsFor({}, NEWS_TAB), window: '1d' }.sort, 'newest')
  // An older build's top is read as the default, so it is never written
  // back; its maxAgeHours is read as a window (lib/window) and written as null.
  const legacy = { 'For You': { sort: 'top', minScore: 0, maxAgeHours: 4 }, news: { sort: 'oldest', minScore: 20, maxAgeHours: null } }
  assert.deepEqual(prefsFor(legacy, FY), { sort: 'best', minScore: 0, window: '1d', maxAgeHours: null })
  assert.deepEqual(prefsFor(legacy, NEWS_TAB), { sort: 'newest', minScore: 20, window: '2d', maxAgeHours: null })
  assert.equal({ ...prefsFor(legacy, FY), window: 'all' }.sort, 'best')
})

test('prefsFor: the same row gives the same object, so the Column memo holds', () => {
  const full = { sort: 'best', minScore: 0, window: '1d', maxAgeHours: null }
  assert.equal(prefsFor({ news: full }, NEWS_TAB), full, 'a complete row is used as it is')
  const partial = { sort: 'top' }
  const map = { news: partial, 'For You': partial }
  const first = prefsFor(map, NEWS_TAB)
  assert.equal(prefsFor(map, NEWS_TAB), first)
  assert.deepEqual(first, { sort: 'newest', minScore: 0, window: '2d', maxAgeHours: null })
  // The same row object read for another tab gets that tab's default.
  assert.deepEqual(prefsFor(map, FY), { sort: 'best', minScore: 0, window: '2d', maxAgeHours: null })
  assert.equal(prefsFor(map, NEWS_TAB), first)
  // Malformed values fall back field by field.
  assert.deepEqual(prefsFor({ news: { sort: 'newest', minScore: 'x', window: -3 } }, NEWS_TAB), NEWEST_PREFS)
  assert.equal(prefsFor({ news: 'nonsense' }, NEWS_TAB), NEWEST_PREFS)
})
