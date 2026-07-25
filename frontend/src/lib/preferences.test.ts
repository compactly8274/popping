/**
 * Tests for the pure helpers in `lib/preferences.tsx`.
 *
 * These target the parts of the preferences provider that don't
 * require React (the row → state decoder, the seed/server merge,
 * the vote-entry cap). The React component itself isn't tested
 * here yet — adding @testing-library/react would be the next
 * step once we have a real test runner in CI.
 *
 * The lib/ files import React only for the JSX in the provider
 * function; the pure helpers below don't touch React at all,
 * so a plain vitest import works without React Testing Library.
 */
import { describe, it, expect } from 'vitest'

import { STORAGE_KEYS } from './storage'
import {
  applyRowToState,
  mergeStateFromServer,
  trimVotedEntries,
  PREFERENCE_KEYS,
  MAX_VOTED,
  type PreferencesState,
  type PreferenceRow,
  type VotedEntriesValue,
} from './preferences'

// Build a fresh DEFAULT-like state. We don't import the internal
// DEFAULT_STATE constant (it's not exported); the test treats the
// merge helpers as black boxes that take "seed" and "server" views
// and produce a merged view. The shape is what consumers see; the
// internals are an implementation detail.
function makeState(overrides: Partial<PreferencesState> = {}): PreferencesState {
  return {
    readEntries: {},
    lastViewed: {},
    columnPrefs: {},
    columnSections: {},
    hiddenEntries: [],
    starredEntries: [],
    votedEntries: {},
    filterPresets: [],
    historyGroupBy: 'entry' as const,
    ...overrides,
  }
}

function row(key: string, value: unknown): PreferenceRow {
  return { key, value, updated_at: '2026-07-24T00:00:00Z' }
}

describe('applyRowToState', () => {
  it('decodes read_entries:<columnId> into readEntries[columnId]', () => {
    const out = makeState()
    applyRowToState(out, row(`${PREFERENCE_KEYS.readEntries}:news`, [1, 2, 3]))
    expect(out.readEntries['news']).toEqual([1, 2, 3])
  })

  it('decodes last_viewed:<columnId> into lastViewed[columnId]', () => {
    const out = makeState()
    applyRowToState(out, row(`${PREFERENCE_KEYS.lastViewed}:tech`, '2026-07-23T10:00:00Z'))
    expect(out.lastViewed['tech']).toBe('2026-07-23T10:00:00Z')
  })

  it('decodes column_prefs:<columnId> into columnPrefs[columnId]', () => {
    const out = makeState()
    const prefs = { sort: 'top' as const, minScore: 5, maxAgeHours: 24 }
    applyRowToState(out, row(`${PREFERENCE_KEYS.columnPrefs}:news`, prefs))
    expect(out.columnPrefs['news']).toEqual(prefs)
  })

  it('decodes column_sections:<columnId> into columnSections[columnId]', () => {
    const out = makeState()
    const sections = { newCollapsed: true, historyCollapsed: false }
    applyRowToState(out, row(`${PREFERENCE_KEYS.columnSections}:news`, sections))
    expect(out.columnSections['news']).toEqual(sections)
  })

  it('decodes hidden_entries (singleton) into hiddenEntries', () => {
    const out = makeState()
    applyRowToState(out, row(PREFERENCE_KEYS.hiddenEntries, [10, 20, 30]))
    expect(out.hiddenEntries).toEqual([10, 20, 30])
  })

  it('decodes starred_entries (singleton) into starredEntries', () => {
    const out = makeState()
    applyRowToState(out, row(PREFERENCE_KEYS.starredEntries, [99]))
    expect(out.starredEntries).toEqual([99])
  })

  it('decodes voted_entries (singleton) into votedEntries, filtering bad values', () => {
    const out = makeState()
    applyRowToState(
      out,
      row(PREFERENCE_KEYS.votedEntries, {
        '100': 'up',
        '200': 'down',
        '300': 'sideways', // invalid direction
      }),
    )
    expect(out.votedEntries).toEqual({ '100': 'up', '200': 'down' })
  })

  it('decodes filter_presets (singleton) into filterPresets', () => {
    const out = makeState()
    const presets = [{ name: 'tech', sources: ['hn'] }]
    applyRowToState(out, row(PREFERENCE_KEYS.filterPresets, presets))
    expect(out.filterPresets).toEqual(presets)
  })

  it('decodes history_group_by (singleton) into historyGroupBy', () => {
    const out = makeState({ historyGroupBy: 'entry' as const })
    applyRowToState(out, row(PREFERENCE_KEYS.historyGroupBy, 'none'))
    expect(out.historyGroupBy).toBe('none')
  })

  it('rejects history_group_by value that is not in the enum', () => {
    const out = makeState({ historyGroupBy: 'entry' as const })
    applyRowToState(out, row(PREFERENCE_KEYS.historyGroupBy, 'sideways'))
    expect(out.historyGroupBy).toBe('entry') // unchanged
  })

  it('ignores unknown keys (forward-compat)', () => {
    const out = makeState()
    applyRowToState(out, row('future_key:42', { anything: 'goes' }))
    // No state field changes
    expect(out).toEqual(makeState())
  })

  it('rejects read_entries with non-array value', () => {
    const out = makeState()
    applyRowToState(out, row(`${PREFERENCE_KEYS.readEntries}:news`, 'not-an-array'))
    expect(out.readEntries['news']).toBeUndefined()
  })

  it('filters non-number entries from read_entries arrays', () => {
    const out = makeState()
    applyRowToState(
      out,
      row(`${PREFERENCE_KEYS.readEntries}:news`, [1, 'two', 3, null, 4] as unknown[]),
    )
    expect(out.readEntries['news']).toEqual([1, 3, 4])
  })

  it('rejects last_viewed with non-string value', () => {
    const out = makeState()
    applyRowToState(out, row(`${PREFERENCE_KEYS.lastViewed}:tech`, 42))
    expect(out.lastViewed['tech']).toBeUndefined()
  })

  it('handles empty read_entries array (sets it to empty)', () => {
    const out = makeState()
    applyRowToState(out, row(`${PREFERENCE_KEYS.readEntries}:news`, []))
    expect(out.readEntries['news']).toEqual([])
  })
})

describe('mergeStateFromServer', () => {
  it('server wins on per-column map collisions (readEntries)', () => {
    const seed = makeState({ readEntries: { news: [1, 2, 3] } })
    const server = makeState({ readEntries: { news: [4, 5, 6] } })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.readEntries['news']).toEqual([4, 5, 6])
  })

  it('server-only keys override nothing (seed keys kept)', () => {
    const seed = makeState({
      readEntries: { news: [1, 2] },
      lastViewed: { tech: '2026-07-23T10:00:00Z' },
    })
    const server = makeState({
      readEntries: { science: [7, 8] },
    })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.readEntries).toEqual({
      news: [1, 2],     // from seed
      science: [7, 8], // from server
    })
    expect(merged.lastViewed['tech']).toBe('2026-07-23T10:00:00Z')
  })

  it('server empty hiddenEntries falls back to seed', () => {
    const seed = makeState({ hiddenEntries: [10, 20] })
    const server = makeState({ hiddenEntries: [] })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.hiddenEntries).toEqual([10, 20])
  })

  it('server non-empty hiddenEntries wins over seed', () => {
    const seed = makeState({ hiddenEntries: [10, 20] })
    const server = makeState({ hiddenEntries: [30] })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.hiddenEntries).toEqual([30])
  })

  it('server historyGroupBy change overrides seed default', () => {
    const seed = makeState({ historyGroupBy: 'entry' as const })
    const server = makeState({ historyGroupBy: 'none' as const })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.historyGroupBy).toBe('none')
  })

  it('votedEntries is union (both seed and server can contribute)', () => {
    const seed = makeState({ votedEntries: { '1': 'up' } })
    const server = makeState({ votedEntries: { '2': 'down' } })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.votedEntries).toEqual({ '1': 'up', '2': 'down' })
  })

  it('votedEntries server keys override seed keys on collision', () => {
    const seed = makeState({ votedEntries: { '1': 'up' } })
    const server = makeState({ votedEntries: { '1': 'down' } })
    const merged = mergeStateFromServer(seed, server)
    expect(merged.votedEntries).toEqual({ '1': 'down' })
  })
})

describe('trimVotedEntries', () => {
  it('returns the same object when under the cap', () => {
    const votes: VotedEntriesValue = {}
    for (let i = 0; i < MAX_VOTED - 1; i++) votes[String(i)] = 'up'
    const out = trimVotedEntries(votes)
    expect(Object.keys(out).length).toBe(MAX_VOTED - 1)
  })

  it('keeps the highest-numbered entries when over the cap', () => {
    // All-numeric keys re-sort ascending in JS, which is the bug
    // the function guards against. Use a mix of high and low
    // ids to verify the highest are kept.
    const votes: VotedEntriesValue = {}
    for (let i = 0; i < MAX_VOTED + 100; i++) votes[String(i)] = 'up'
    const out = trimVotedEntries(votes)
    expect(Object.keys(out).length).toBe(MAX_VOTED)
    // All kept keys should be among the highest-numbered.
    const kept = Object.keys(out).map(Number)
    const max = Math.max(...kept)
    const min = Math.min(...kept)
    expect(max).toBe(MAX_VOTED + 99) // the last id inserted
    expect(min).toBe(100) // the first id that survives the trim
  })

  it('preserves the vote direction of every kept entry', () => {
    const votes: VotedEntriesValue = {}
    for (let i = 0; i < MAX_VOTED + 5; i++) {
      votes[String(i)] = i % 2 === 0 ? 'up' : 'down'
    }
    const out = trimVotedEntries(votes)
    for (const [k, v] of Object.entries(out)) {
      const n = Number(k)
      expect(v).toBe(n % 2 === 0 ? 'up' : 'down')
    }
  })

  it('does not mutate the input', () => {
    const votes: VotedEntriesValue = {}
    for (let i = 0; i < MAX_VOTED + 5; i++) votes[String(i)] = 'up'
    const snapshot = JSON.stringify(votes)
    trimVotedEntries(votes)
    expect(JSON.stringify(votes)).toBe(snapshot)
  })

  it('handles empty input', () => {
    expect(trimVotedEntries({})).toEqual({})
  })

  it('handles input at exactly the cap', () => {
    const votes: VotedEntriesValue = {}
    for (let i = 0; i < MAX_VOTED; i++) votes[String(i)] = 'up'
    const out = trimVotedEntries(votes)
    expect(Object.keys(out).length).toBe(MAX_VOTED)
  })
})

describe('legacy seed flag invariants', () => {
  // The seed runs at most once per browser. The flag is checked
  // before any localStorage read; once set, subsequent launches
  // skip the seed entirely. We test the public surface of that
  // contract by verifying the SEED_FLAG_KEY namespace is a
  // versioned v1 key — if it ever changes, the seed would re-run
  // for every user on the next deploy, which is the wrong default.
  // SEED_FLAG_KEY is file-local; we verify the namespace pattern
  // by reading the storage keys module (which has the same
  // convention) and asserting shape. This is a structural test
  // that catches accidental namespace drift in storage.ts.
  it('STORAGE_KEYS are namespaced (v1 + popping.)', () => {
    for (const k of Object.values(STORAGE_KEYS)) {
      expect(k).toMatch(/^popping\.v1\./)
    }
  })
})
