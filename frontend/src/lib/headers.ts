// Rules for a feed's custom request headers, mirroring
// _validate_custom_headers in backend/app/routes/sources.py so the
// phone refuses exactly what the server would. No imports (see
// feedRules.ts). backend/tests/test_frontend_feed_editor_rules.py
// compares the constants below with the backend's, so keep them plain
// literals.

export const HEADER_DENYLIST: readonly string[] = [
  'cookie',
  'authorization',
  'host',
  'x-forwarded-for',
  'x-forwarded-host',
  'x-forwarded-proto',
  'x-real-ip',
  'x-original-url',
  'x-rewrite-url',
  'content-length',
  'transfer-encoding',
  'connection',
  'keep-alive',
  'te',
  'trailer',
  'upgrade',
  'expect',
  'proxy-authorization',
  'proxy-connection',
]

export const HEADERS_MAX = 20
export const HEADER_VALUE_MAX = 512
export const HEADER_NAME_MAX = 100

// Stored in their usual spelling: the fetchers merge headers with a
// case-sensitive dict, so "user-agent" would send a second User-Agent.
export const CANONICAL_HEADER_NAMES: Readonly<Record<string, string>> = {
  'user-agent': 'User-Agent',
  'accept': 'Accept',
  'accept-language': 'Accept-Language',
}

// The one-tap fixes for sites that refuse automated readers.
export const BROWSER_USER_AGENT =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'
export const HEADER_PRESETS: ReadonlyArray<{ label: string; name: string; value: string }> = [
  { label: 'Use a browser identity', name: 'User-Agent', value: BROWSER_USER_AGENT },
  { label: 'Prefer English', name: 'Accept-Language', value: 'en-CA,en;q=0.9' },
]

const NAME_RE = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/
const VALUE_RE = /^[\t\x20-\x7e]*$/

export type HeaderMap = Record<string, string>
export type HeaderRow = { key: number; name: string; value: string }
export type RowProblem = {
  key: number
  message: string
  // Only one of name and value is filled. Shown once focus has left
  // the headers, and it doesn't block saving the other rows.
  incomplete: boolean
}

/** Phone keyboards type curly quotes, long dashes and "…"; headers
 *  need the plain forms. */
export function plainText(s: string): string {
  return s
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/[–—]/g, '-')
    .replace(/…/g, '...')
    .trim()
}

export function canonicalName(name: string): string {
  return CANONICAL_HEADER_NAMES[name.toLowerCase()] ?? name
}

/** A row as it will be stored: plain text, canonical name. */
export function cleanRow(row: HeaderRow): HeaderRow {
  return { key: row.key, name: canonicalName(plainText(row.name)), value: plainText(row.value) }
}

/** Every row's problem, in the backend's order of checks. Rows with
 *  both fields empty are ignored. */
export function checkRows(rows: readonly HeaderRow[]): { problems: RowProblem[]; tooMany: string | null } {
  const problems: RowProblem[] = []
  const seen = new Set<string>()
  let filled = 0
  for (const raw of rows) {
    const { key, name, value } = cleanRow(raw)
    if (!name && !value) continue
    filled++
    const fail = (message: string, incomplete = false) => problems.push({ key, message, incomplete })
    // A typed name is checked even before its value, so "Cookie" says
    // at once that it can't be sent instead of asking for a value.
    if (name) {
      const nameProblem = !NAME_RE.test(name)
        ? 'Header names use letters, numbers and dashes.'
        : name.length > HEADER_NAME_MAX
          ? `Header names can be ${HEADER_NAME_MAX} characters at most.`
          : seen.has(name.toLowerCase())
            ? 'Already added.'
            : HEADER_DENYLIST.includes(name.toLowerCase())
              ? `Popping can't send a ${name} header.`
              : null
      seen.add(name.toLowerCase())
      if (nameProblem) {
        fail(nameProblem)
        continue
      }
    }
    if (!name || !value) {
      fail('Fill in both to save this header.', true)
      continue
    }
    if (value.length > HEADER_VALUE_MAX) fail(`Too long (${HEADER_VALUE_MAX} characters max).`)
    else if (!VALUE_RE.test(value)) fail('Use plain letters, numbers and punctuation.')
  }
  return { problems, tooMany: filled > HEADERS_MAX ? `${HEADERS_MAX} headers at most.` : null }
}

/** True when nothing may be sent: a complete row has an error, or
 *  there are too many rows. Half-filled rows don't block. */
export function blocksSave(check: { problems: RowProblem[]; tooMany: string | null }): boolean {
  return !!check.tooMany || check.problems.some((p) => !p.incomplete)
}

/** The map a save sends, or null for none. Complete rows are included.
 *  A half-filled row whose name matches a saved header keeps the saved
 *  value (it may be mid-retype); any other half-filled row is left
 *  out. Callers check ``blocksSave`` first. */
export function rowsToMap(rows: readonly HeaderRow[], saved: HeaderMap | null | undefined): HeaderMap | null {
  const savedByLower = new Map(Object.entries(saved ?? {}).map(([k, v]) => [k.toLowerCase(), [k, v] as const]))
  const out: HeaderMap = {}
  for (const raw of rows) {
    const { name, value } = cleanRow(raw)
    if (name && value) {
      out[name] = value
    } else if (name && !value) {
      const keep = savedByLower.get(name.toLowerCase())
      if (keep) out[keep[0]] = keep[1]
    }
  }
  return Object.keys(out).length ? out : null
}

/** Key-sorted JSON, so two maps compare equal whatever their order;
 *  null and {} are the same (the backend stores both as NULL). */
export function canonicalHeaders(map: HeaderMap | null | undefined): string {
  const entries = Object.entries(map ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return JSON.stringify(entries)
}

export function sameHeaders(a: HeaderMap | null | undefined, b: HeaderMap | null | undefined): boolean {
  return canonicalHeaders(a) === canonicalHeaders(b)
}
