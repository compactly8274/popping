import test from 'node:test'
import assert from 'node:assert/strict'
import { DEFAULT_WINDOW, WINDOWS, inWindow, isWider, widerWindow, windowHours, windowOf } from '../../src/lib/window.ts'
import { prefsFor } from '../../src/lib/sorting.ts'
import { colKey } from '../../src/lib/columns.ts'

const NEWS = { key: colKey('news'), name: 'news' }

test('windowOf: an explicit window wins, whatever else the row holds', () => {
  for (const w of WINDOWS.map((o) => o.value)) {
    assert.equal(windowOf({ sort: 'newest', minScore: 0, window: w }), w)
    // Both fields: a stale bundle's maxAgeHours can't shrink the tab (Risks).
    assert.equal(windowOf({ sort: 'newest', minScore: 0, window: w, maxAgeHours: 24 }), w)
    assert.equal(windowOf({ sort: 'newest', minScore: 0, window: w, maxAgeHours: null }), w)
  }
})

test('windowOf: the legacy "Hide if older than" values read as 1 day, only without a window', () => {
  for (const h of [1, 4, 24]) {
    assert.equal(windowOf({ sort: 'newest', minScore: 0, maxAgeHours: h }), '1d', String(h))
    assert.equal(windowOf({ sort: 'newest', minScore: 0, maxAgeHours: h, window: '7d' }), '7d', String(h))
  }
})

test('windowOf: null, missing and anything else read as the default (2 days)', () => {
  // Null meant All to the old control, but every stored row has it.
  assert.equal(windowOf({ sort: 'newest', minScore: 0, maxAgeHours: null }), DEFAULT_WINDOW)
  assert.equal(windowOf({ sort: 'newest', minScore: 0 }), DEFAULT_WINDOW)
  assert.equal(windowOf({ sort: 'newest', minScore: 0, window: 'week' }), DEFAULT_WINDOW)
  assert.equal(windowOf({ sort: 'newest', minScore: 0, maxAgeHours: 48 }), DEFAULT_WINDOW)
  assert.equal(windowOf({ sort: 'newest', minScore: 0, maxAgeHours: '24' }), DEFAULT_WINDOW)
  assert.equal(windowOf(undefined), DEFAULT_WINDOW)
  assert.equal(windowOf(null), DEFAULT_WINDOW)
  assert.equal(windowOf('nonsense'), DEFAULT_WINDOW)
  assert.equal(DEFAULT_WINDOW, '2d')
})

test('windowOf: a row that arrives through a saved view (filter_presets) reads the same way', () => {
  // A preset's columnPrefs rows have the stored shape; the app reads one
  // with prefsFor as it reads a column_prefs row.
  const preset = { id: 'p-1', name: 'Morning', activeSources: [], columnPrefs: { news: { sort: 'newest', minScore: 0, maxAgeHours: 24 }, deals: { sort: 'best', minScore: 0, window: 'all' } } }
  assert.equal(windowOf(preset.columnPrefs.news), '1d')
  assert.equal(windowOf(preset.columnPrefs.deals), 'all')
  assert.equal(prefsFor(preset.columnPrefs, NEWS).window, '1d')
  assert.equal(prefsFor(preset.columnPrefs, { key: colKey('deals'), name: 'deals' }).window, 'all')
})

test('prefsFor: a write sets window, with maxAgeHours null', () => {
  // What Column writes back: a spread of the prefs it was given.
  const legacy = { sort: 'newest', minScore: 0, maxAgeHours: 24 }
  const read = prefsFor({ news: legacy }, NEWS)
  assert.deepEqual(read, { sort: 'newest', minScore: 0, window: '1d', maxAgeHours: null })
  assert.deepEqual({ ...read, window: '7d' }, { sort: 'newest', minScore: 0, window: '7d', maxAgeHours: null })
  // A row already in the new shape is used as it is.
  const row = { sort: 'newest', minScore: 0, window: '7d', maxAgeHours: null }
  assert.equal(prefsFor({ news: row }, NEWS), row)
  // One with no maxAgeHours, or a stale bundle's number next to its
  // window, is rebuilt with null (the same object each time).
  for (const other of [{ sort: 'newest', minScore: 0, window: '7d' }, { sort: 'newest', minScore: 0, window: '7d', maxAgeHours: 24 }]) {
    const rebuilt = prefsFor({ news: other }, NEWS)
    assert.deepEqual(rebuilt, { sort: 'newest', minScore: 0, window: '7d', maxAgeHours: null })
    assert.equal(prefsFor({ news: other }, NEWS), rebuilt)
  }
})

test('a stale bundle that changes a tab\'s sort keeps the tab\'s window (Risks)', () => {
  // The rule an older build's prefsFor reads a row by (main before
  // PR4d): the row as it is when its fields already have their read
  // values (maxAgeHours: a positive number, else null), otherwise a
  // rebuilt {sort, minScore, maxAgeHours}. Its Column writes a spread of
  // what that gives.
  const oldPrefsFor = (r: Record<string, unknown>) => {
    const sort = r.sort === 'newest' || r.sort === 'best' ? r.sort : 'newest'
    const minScore = typeof r.minScore === 'number' ? r.minScore : 0
    const maxAgeHours = typeof r.maxAgeHours === 'number' && r.maxAgeHours > 0 ? r.maxAgeHours : null
    if (r.sort === sort && r.minScore === minScore && r.maxAgeHours === maxAgeHours) return r
    return { sort, minScore, maxAgeHours }
  }
  // The phone picks 7 days; the desktop tab, still on the old build,
  // then picks Best, then its "Hide if older than" 1 hour.
  const phone = { ...prefsFor({}, NEWS), window: '7d' as const }
  const desk = { ...oldPrefsFor(phone), sort: 'best' }
  assert.equal(prefsFor({ news: desk }, NEWS).window, '7d')
  assert.equal(prefsFor({ news: desk }, NEWS).sort, 'best')
  const shorter = { ...oldPrefsFor(desk), maxAgeHours: 1 }
  assert.equal(prefsFor({ news: shorter }, NEWS).window, '7d')
})

test('windowHours, isWider and widerWindow', () => {
  assert.deepEqual(WINDOWS.map((o) => [o.value, windowHours(o.value)]), [['1d', 24], ['2d', 48], ['7d', 168], ['all', 8760]])
  assert.deepEqual(WINDOWS.map((o) => isWider(o.value)), [false, false, true, true])
  assert.deepEqual(WINDOWS.map((o) => widerWindow(o.value)), ['7d', '7d', 'all', null])
})

test('inWindow: by fetched_at against now; no time, or one that does not parse, is kept', () => {
  const now = Date.parse('2026-10-20T12:00:00Z')
  const at = (hoursAgo: number) => new Date(now - hoursAgo * 3600000).toISOString()
  assert.equal(inWindow(at(23), '1d', now), true)
  assert.equal(inWindow(at(25), '1d', now), false)
  assert.equal(inWindow(at(47), '2d', now), true)
  assert.equal(inWindow(at(49), '2d', now), false)
  assert.equal(inWindow(at(49), '7d', now), true)
  assert.equal(inWindow(at(169), '7d', now), false)
  assert.equal(inWindow(at(169), 'all', now), true)
  assert.equal(inWindow(at(24 * 400), 'all', now), true)
  assert.equal(inWindow(null, '1d', now), true)
  assert.equal(inWindow('not a date', '1d', now), true)
})
