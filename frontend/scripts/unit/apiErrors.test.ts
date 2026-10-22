import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  briefFailureMessage,
  errorMessage,
  fieldSaveMessage,
  friendlyTestError,
  isRetryable,
  lastErrorMessage,
  parseApiError,
} from '../../src/lib/apiErrors.ts'

const api = (status: number, detail?: unknown) => Object.assign(new Error(`${status}`), { name: 'ApiError', status, detail })
const label = (n: string) => n.replace(/_yt$/, ' (YouTube)').replace(/_/g, ' ')

test('fieldSaveMessage, in the order of the plan table', () => {
  const rows: Array<[unknown, unknown, string]> = [
    [api(409, "source name 'x' is already in use"), 'x', 'Another feed already uses this name.'],
    [api(400, "name 'bbc_news' is reserved for a built-in source"), 'bbc_news', 'That name belongs to a built-in feed. Pick another.'],
    [api(400, "built-in source 'hn_top' cannot be renamed or re-URLed"), 'x', 'Built-in feeds keep their name and address.'],
    [api(404, 'source not found'), 'x', 'This feed was deleted.'],
    [api(422, 'name must match ^[a-z0-9_]{1,120}$ (lowercase letters, digits, underscore)'), 'Bad', 'Use lowercase letters, numbers and underscores.'],
    [api(422, 'category cannot be empty'), '', 'Pick a category.'],
    [api(422, "category 'For You' is reserved for a built-in column"), 'For You', '“For You” is used by the app. Pick another name.'],
    [api(422, 'category must be 40 characters or fewer'), 'x', 'Keep it to 40 characters or fewer.'],
    [api(422, 'url must be http or https'), 'ftp://x', 'Use a web address that starts with https://'],
    [api(422, 'url must include a host'), 'https://', 'Use a web address that starts with https://'],
    [api(422, 'url is not a valid URL'), 'x', 'Use a web address that starts with https://'],
    [api(422, 'url is not reachable (must be a public host)'), 'x', "That address isn't reachable from Popping."],
    [api(422, "reddit url must be a subreddit reference (e.g. 'r/python' or 'https://www.reddit.com/r/python')"), 'x', 'Type a subreddit, like r/python.'],
    [api(422, "link_pattern must start with '/' (path prefix, not a full URL)"), 'x', 'Start it with /'],
    [api(422, "header name 'Bad Header' is not a valid HTTP header name"), {}, 'Header names use letters, numbers and dashes.'],
    [api(422, "header name 'XXX' is longer than 100 characters"), {}, 'Header names can be 100 characters at most.'],
    [api(422, "header 'X-A' is listed twice"), {}, 'Already added.'],
    [api(422, "header 'Cookie' cannot be overridden per-source"), {}, "Popping can't send a Cookie header."],
    [api(422, "header 'X-A' value exceeds 512 chars"), {}, 'Too long (512 characters max).'],
    [api(422, "header 'X-A' value contains disallowed characters (must be printable ASCII or tab)"), {}, 'Use plain letters, numbers and punctuation.'],
    [api(422, 'custom_headers may have at most 20 entries'), {}, '20 headers at most.'],
    [api(409, 'You already follow this channel as mkbhd_yt.'), 'x', 'You already follow this channel as mkbhd (YouTube).'],
    [api(422, "That isn't a channel link. Paste the channel's page, a video link, an @handle or the channel ID."), 'x', "That isn't a channel link. Paste the channel's page, a video link, an @handle or the channel ID."],
    [api(422, "YouTube didn't let Popping look this channel up. Paste the channel ID instead."), 'x', "YouTube didn't let Popping look this channel up. Paste the channel ID instead."],
    [api(422, 'something new the app has never seen'), 'x', "Popping couldn't save that. Try a different value."],
    [api(401), 'x', 'Please sign in again.'],
    [api(500), 'x', 'Popping had a problem saving. Try again.'],
    [api(502, 'Bad Gateway'), 'x', 'Popping had a problem saving. Try again.'],
    [new TypeError('Failed to fetch'), 'x', "Couldn't reach Popping. Check your connection."],
    [new DOMException('aborted', 'AbortError'), 'x', 'That took too long. Try again.'],
  ]
  for (const [err, value, want] of rows) assert.equal(fieldSaveMessage(err, 'name', value, label), want)
})

test('a 422 with a list of field errors is read too', () => {
  const err = api(422, [{ loc: ['body', 'name'], msg: 'name must match ^[a-z0-9_]{1,120}$' }])
  assert.equal(parseApiError(err, 'x'), 'name must match ^[a-z0-9_]{1,120}$')
  assert.equal(fieldSaveMessage(err, 'name', 'Bad'), 'Use lowercase letters, numbers and underscores.')
})

test('isRetryable: network, timeout and 5xx only', () => {
  assert.equal(isRetryable(new TypeError('x')), true)
  assert.equal(isRetryable(new DOMException('x', 'AbortError')), true)
  assert.equal(isRetryable(api(503)), true)
  assert.equal(isRetryable(api(422, 'x')), false)
  assert.equal(isRetryable(api(401)), false)
})

test('errorMessage for other call sites', () => {
  assert.equal(errorMessage(api(401)), 'Please sign in again.')
  assert.equal(errorMessage(api(500)), 'Popping had a problem. Try again.')
  assert.equal(errorMessage(new TypeError('x')), "Couldn't reach Popping. Check your connection.")
  assert.equal(errorMessage(api(418)), 'Something went wrong. Try again.')
  assert.equal(errorMessage('boom'), 'Something went wrong. Try again.')
})

test('briefFailureMessage names the one reason the user can fix', () => {
  assert.equal(
    briefFailureMessage('no LLM provider configured (set ANTHROPIC_API_KEY / OPENAI_API_KEY / GROQ_API_KEY, or run Ollama)'),
    'No AI provider is set up yet. Pick one in Menu → Settings → AI.',
  )
  assert.equal(briefFailureMessage('LLM call failed or returned empty content (check backend logs)'), "Popping couldn't write the brief. Try again.")
  assert.equal(briefFailureMessage(null), "Popping couldn't write the brief. Try again.")
})

test('lastErrorMessage for each scheduler prefix', () => {
  assert.equal(lastErrorMessage("[blocked] HTTPStatusError: Client error '403 Forbidden' for url 'https://x'"), 'This site is refusing Popping (HTTP 403).')
  assert.equal(lastErrorMessage('[blocked] HTTPStatusError: refused'), 'This site is refusing Popping.')
  assert.equal(lastErrorMessage("[not found] HTTPStatusError: Client error '404 Not Found'"), 'The address no longer exists (HTTP 404).')
  assert.equal(lastErrorMessage("[upstream error] HTTPStatusError: Server error '503 Service Unavailable'"), 'The site had a problem on its end (HTTP 503).')
  assert.equal(lastErrorMessage("[http error] HTTPStatusError: Client error '429 Too Many Requests'"), 'The site answered with an error (HTTP 429).')
  assert.equal(lastErrorMessage('[network] ConnectError: [Errno -2] Name or service not known'), "Couldn't reach the site.")
  assert.equal(lastErrorMessage('[error] ValueError: not a feed'), "Popping couldn't read this feed.")
  assert.equal(lastErrorMessage(null), "Popping couldn't read this feed.")
})

test('friendlyTestError kinds', () => {
  assert.equal(friendlyTestError('forbidden', '403'), 'This site blocks automated readers. Open “Advanced” below, tap “Use a browser identity”, then Test again.')
  assert.equal(friendlyTestError('parse_error', 'x'), "That address answered, but it isn't a feed.")
  assert.equal(friendlyTestError('network_error', 'x'), "Couldn't reach that site. Check the address.")
  assert.equal(friendlyTestError('name_conflict', "source name 'x' is already in use"), 'Another feed already uses this name.')
  assert.equal(friendlyTestError('invalid_headers', "header 'Cookie' cannot be overridden per-source"), "Popping can't send a Cookie header.")
  assert.equal(friendlyTestError('invalid_url', 'url is not reachable (must be a public host)'), "That address isn't reachable from Popping.")
  assert.equal(friendlyTestError('invalid_url', "That address isn't reachable from Popping."), "That address isn't reachable from Popping.")
  assert.equal(friendlyTestError('invalid_url', 'Couldn\'t find the channel on that YouTube page. Paste the channel ID instead. It starts with UC.'), "Couldn't find the channel on that YouTube page. Paste the channel ID instead. It starts with UC.")
  assert.equal(friendlyTestError('invalid_url', null), 'Use a web address that starts with https://')
  assert.equal(friendlyTestError('not_found', null), "Couldn't find a feed at that address. Check it and try again.")
  assert.equal(friendlyTestError('timeout', null), 'That site took too long to answer. It may be slow or down.')
  assert.equal(friendlyTestError('unknown', 'Traceback: KeyError'), "Popping couldn't read that address.")
  assert.equal(friendlyTestError(null, null), "Popping couldn't read that address.")
})
