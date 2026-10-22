import { test } from 'node:test'
import assert from 'node:assert/strict'
import { cleanDisplayName, uniqueSlug } from '../../src/lib/feedRules.ts'
import { getDisplayNamesVersion, rememberDisplayNames, slugLabel, sourceLabel } from '../../src/lib/labels.ts'

test('a typed feed name wins over the slug label', () => {
  rememberDisplayNames([{ name: 'cpabc_newsroom', display_name: 'CPABC Newsroom' }, { name: 'mining_com' }])
  assert.equal(sourceLabel('cpabc_newsroom'), 'CPABC Newsroom')
  assert.equal(sourceLabel('mining_com'), 'Mining.com')
  assert.equal(slugLabel('cpabc_newsroom'), 'CPABC Newsroom')
  rememberDisplayNames([])
  assert.equal(sourceLabel('cpabc_newsroom'), 'CPABC Newsroom')
  rememberDisplayNames([{ name: 'bc_gov', display_name: 'B.C. Gov – Finance' }])
  assert.equal(sourceLabel('bc_gov'), 'B.C. Gov – Finance')
  rememberDisplayNames([])
})

test('the version only moves when a name changes', () => {
  rememberDisplayNames([{ name: 'a', display_name: 'A' }])
  const v = getDisplayNamesVersion()
  rememberDisplayNames([{ name: 'a', display_name: 'A' }, { name: 'b', display_name: null }])
  assert.equal(getDisplayNamesVersion(), v)
  rememberDisplayNames([{ name: 'a', display_name: 'Alpha' }])
  assert.equal(getDisplayNamesVersion(), v + 1)
  rememberDisplayNames([])
})

test('cleanDisplayName tidies spacing only', () => {
  assert.equal(cleanDisplayName('  CPABC   Newsroom '), 'CPABC Newsroom')
  assert.equal(cleanDisplayName('B.C. Gov'), 'B.C. Gov')
  assert.equal(cleanDisplayName('   '), '')
})

test('uniqueSlug avoids taken and built-in names', () => {
  assert.equal(uniqueSlug('cpabc', []), 'cpabc')
  assert.equal(uniqueSlug('cpabc', ['cpabc', 'cpabc_2']), 'cpabc_3')
  assert.equal(uniqueSlug('hn_top', []), 'hn_top_2')
})
