/**
 * Tests for the storage utility helpers in `lib/storage.ts`.
 *
 * These are pure-function tests that need a jsdom environment
 * for `window.localStorage`. The ``safeGetItem`` / ``safeSetItem``
 * / ``safeRemoveItem`` helpers are the only public surface;
 * ``STORAGE_KEYS`` is also a constant we test for shape.
 */
import { describe, it, expect, beforeEach } from 'vitest'

import {
  STORAGE_KEYS,
  safeGetItem,
  safeSetItem,
  safeRemoveItem,
} from './storage'

describe('STORAGE_KEYS', () => {
  it('namespaces every key under popping.v1.*', () => {
    for (const k of Object.values(STORAGE_KEYS)) {
      expect(k).toMatch(/^popping\.v1\./)
    }
  })

  it('has only device-local keys (no per-user, per-device state)', () => {
    // If a future commit adds a server-backed key here, the test
    // fails. Server-backed state lives in user_preferences, not
    // localStorage (see the lib/storage.ts docstring). Keeping
    // the small set of known names is the point of the explicit
    // list.
    const names = Object.keys(STORAGE_KEYS)
    expect(names).toContain('mobileColLast')
    expect(names).toContain('briefCollapsed')
    expect(names.length).toBe(2)
  })
})

describe('safeGetItem / safeSetItem / safeRemoveItem', () => {
  beforeEach(() => {
    // jsdom gives us a fresh storage per test by default but
    // some setups share state across tests; clear explicitly.
    window.localStorage.clear()
  })

  it('round-trips a value', () => {
    expect(safeSetItem('test', 'hello')).toBe(true)
    expect(safeGetItem('test')).toBe('hello')
  })

  it('returns null for a missing key', () => {
    expect(safeGetItem('does-not-exist')).toBeNull()
  })

  it('removeItem deletes the key', () => {
    safeSetItem('test', 'hello')
    expect(safeGetItem('test')).toBe('hello')
    safeRemoveItem('test')
    expect(safeGetItem('test')).toBeNull()
  })

  it('survives double-remove (idempotent)', () => {
    safeRemoveItem('never-existed')
    safeSetItem('test', 'hello')
    safeRemoveItem('test')
    safeRemoveItem('test') // second remove should not throw
    expect(safeGetItem('test')).toBeNull()
  })

  it('setItem with empty string round-trips as empty string (not null)', () => {
    safeSetItem('test', '')
    expect(safeGetItem('test')).toBe('')
  })
})
