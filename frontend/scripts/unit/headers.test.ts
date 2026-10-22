import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  BROWSER_USER_AGENT,
  blocksSave,
  canonicalName,
  checkRows,
  plainText,
  rowsToMap,
  sameHeaders,
  type HeaderRow,
} from '../../src/lib/headers.ts'

let key = 0
const row = (name: string, value: string): HeaderRow => ({ key: key++, name, value })
const messages = (rows: HeaderRow[]) => checkRows(rows).problems.map((p) => p.message)

test('a valid row has no problem', () => {
  assert.deepEqual(checkRows([row('X-Ok', 'fine')]), { problems: [], tooMany: null })
  assert.deepEqual(messages([row('User-Agent', BROWSER_USER_AGENT)]), [])
})

test('both fields empty: the row is ignored', () => {
  assert.deepEqual(messages([row('', '  ')]), [])
  assert.equal(rowsToMap([row('', '')], null), null)
})

test('a refused or malformed name is reported before its value is typed', () => {
  assert.deepEqual(messages([row('Cookie', '')]), ["Popping can't send a Cookie header."])
  assert.deepEqual(messages([row('Bad Header', '')]), ['Header names use letters, numbers and dashes.'])
  assert.equal(blocksSave(checkRows([row('Cookie', '')])), true)
})

test('one field filled: fill in both, and it does not block saving', () => {
  const check = checkRows([row('X-A', ''), row('X-B', 'b')])
  assert.deepEqual(check.problems.map((p) => [p.message, p.incomplete]), [['Fill in both to save this header.', true]])
  assert.equal(blocksSave(check), false)
  assert.deepEqual(messages([row('', 'value')]), ['Fill in both to save this header.'])
})

test('each rule and its message', () => {
  assert.deepEqual(messages([row('Bad Header', 'x')]), ['Header names use letters, numbers and dashes.'])
  assert.deepEqual(messages([row('Coo kie', 'x')]), ['Header names use letters, numbers and dashes.'])
  assert.deepEqual(messages([row('X'.repeat(101), 'x')]), ['Header names can be 100 characters at most.'])
  assert.deepEqual(messages([row('X-A', '1'), row('x-a', '2')]), ['Already added.'])
  assert.deepEqual(messages([row('Cookie', 'x')]), ["Popping can't send a Cookie header."])
  assert.deepEqual(messages([row('content-length', '1')]), ["Popping can't send a content-length header."])
  assert.deepEqual(messages([row('Proxy-Authorization', 'x')]), ["Popping can't send a Proxy-Authorization header."])
  assert.deepEqual(messages([row('X-A', 'v'.repeat(513))]), ['Too long (512 characters max).'])
  assert.deepEqual(messages([row('X-A', 'café')]), ['Use plain letters, numbers and punctuation.'])
  assert.deepEqual(messages([row('X-A', 'a\r\nb')]), ['Use plain letters, numbers and punctuation.'])
  const check = checkRows([row('Cookie', 'x')])
  assert.equal(blocksSave(check), true)
})

test('more than 20 rows', () => {
  const rows = Array.from({ length: 21 }, (_, i) => row(`X-${i}`, 'v'))
  const check = checkRows(rows)
  assert.equal(check.tooMany, '20 headers at most.')
  assert.equal(blocksSave(check), true)
  assert.equal(checkRows(rows.slice(0, 20)).tooMany, null)
})

test('smart punctuation becomes plain, in names and values', () => {
  assert.equal(plainText('  “quoted” ‘it’s’ a–b—c… '), '"quoted" \'it\'s\' a-b-c...')
  assert.deepEqual(messages([row('X–Thing', 'it’s “ok”')]), [])
  assert.deepEqual(rowsToMap([row('X–Thing', 'it’s')], null), { 'X-Thing': "it's" })
})

test('canonical names', () => {
  assert.equal(canonicalName('user-agent'), 'User-Agent')
  assert.equal(canonicalName('ACCEPT'), 'Accept')
  assert.equal(canonicalName('accept-language'), 'Accept-Language')
  assert.equal(canonicalName('x-custom'), 'x-custom')
  assert.deepEqual(rowsToMap([row('user-agent', 'X')], null), { 'User-Agent': 'X' })
  assert.deepEqual(messages([row('user-agent', 'a'), row('User-Agent', 'b')]), ['Already added.'])
})

test('rowsToMap keeps a saved value while its row is half-filled, and leaves out new half rows', () => {
  const saved = { 'User-Agent': 'saved-ua', 'X-Old': 'old' }
  const rows = [row('User-Agent', ''), row('X-New', ''), row('X-Old', 'changed')]
  assert.deepEqual(rowsToMap(rows, saved), { 'User-Agent': 'saved-ua', 'X-Old': 'changed' })
})

test('rowsToMap gives null when nothing is left', () => {
  assert.equal(rowsToMap([], { A: '1' }), null)
  assert.equal(rowsToMap([row('X-New', '')], null), null)
})

test('sameHeaders: null equals {}, key order does not matter', () => {
  assert.equal(sameHeaders(null, {}), true)
  assert.equal(sameHeaders(undefined, null), true)
  assert.equal(sameHeaders({ A: '1', B: '2' }, { B: '2', A: '1' }), true)
  assert.equal(sameHeaders({ A: '1' }, { A: '2' }), false)
  assert.equal(sameHeaders({ A: '1' }, { a: '1' }), false)
})
