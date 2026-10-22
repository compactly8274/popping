// Turning API failures into plain words. The backend's own text never
// reaches the user raw except for the YouTube and Apple Podcasts
// resolver messages, which are written for people. No imports (see
// feedRules.ts): errors are duck-typed on { name, status, detail },
// which api.ts's ApiError, fetch's TypeError and AbortError all fit.

type ErrLike = { name?: unknown; status?: unknown; detail?: unknown; message?: unknown }

function asErr(err: unknown): ErrLike {
  return err && typeof err === 'object' ? (err as ErrLike) : {}
}

function statusOf(err: unknown): number {
  const s = asErr(err).status
  return typeof s === 'number' ? s : 0
}

function isNetwork(err: unknown): boolean {
  return asErr(err).name === 'TypeError'
}

function isTimeout(err: unknown): boolean {
  return asErr(err).name === 'AbortError'
}

/** The backend's reason as one string: FastAPI sends a string, or a
 *  list of field errors for a 422. Falls back to the error message,
 *  then ``fallback``. */
export function parseApiError(err: unknown, fallback: string): string {
  const e = asErr(err)
  if (Array.isArray(e.detail)) {
    return e.detail
      .map((d: unknown) =>
        d && typeof d === 'object' && typeof (d as { msg?: unknown }).msg === 'string'
          ? (d as { msg: string }).msg
          : JSON.stringify(d),
      )
      .join('; ')
  }
  if (typeof e.detail === 'string') return e.detail
  if (typeof e.message === 'string') return e.message
  return fallback
}

function detailOf(err: unknown): string {
  const e = asErr(err)
  return Array.isArray(e.detail) || typeof e.detail === 'string' ? parseApiError(err, '') : ''
}

// The resolvers' messages are already plain words; pass them through.
// The prefixes come from backend/app/youtube.py (MSG_*) and
// backend/app/apple_podcasts.py.
const RESOLVER_MESSAGES = [
  "YouTube didn't let Popping",
  "YouTube says this channel",
  "Couldn't find the channel on that YouTube page",
  'YouTube answered with an error',
  "Couldn't reach YouTube",
  "That address isn't reachable from Popping",
  'YouTube sent Popping somewhere unexpected',
  "That isn't a channel link",
  "Private or automatic playlists don't have a feed",
  'Apple Podcasts id',
  "this show doesn't expose a public RSS feed",
]

const ALREADY_FOLLOW_RE = /^You already follow this channel as (.+)\.$/

/** Plain words for a backend reason string, or null when it isn't one
 *  Popping knows. Shared by saves and by Test. */
function messageForDetail(detail: string, value: unknown, nameLabel: (n: string) => string): string | null {
  const d = detail
  if (d.includes('already in use')) return 'Another feed already uses this name.'
  // Before the next row: this one also contains "built-in".
  if (d.includes('reserved for a built-in source')) return 'That name belongs to a built-in feed. Pick another.'
  if (d.includes('cannot be renamed or re-URLed')) return 'Built-in feeds keep their name and address.'
  if (d.includes('source not found')) return 'This feed was deleted.'
  if (d.includes('name must match')) return 'Use lowercase letters, numbers and underscores.'
  if (d.includes('category cannot be empty')) return 'Pick a category.'
  if (d.includes('reserved for a built-in column')) {
    return `“${typeof value === 'string' ? value.trim() : ''}” is used by the app. Pick another name.`
  }
  if (d.includes('category must be') && d.includes('characters or fewer')) return 'Keep it to 40 characters or fewer.'
  if (d.includes('url must be http or https') || d.includes('url must include a host') || d.includes('url is not a valid URL')) {
    return 'Use a web address that starts with https://'
  }
  if (d.includes('not reachable')) return "That address isn't reachable from Popping."
  if (d.includes('reddit url must be a subreddit reference')) return 'Type a subreddit, like r/python.'
  if (d.includes('link_pattern must')) return 'Start it with /'
  if (d.includes('not a valid HTTP header name')) return 'Header names use letters, numbers and dashes.'
  if (/header name .* is longer than/.test(d)) return 'Header names can be 100 characters at most.'
  if (d.includes('listed twice')) return 'Already added.'
  if (d.includes('cannot be overridden')) {
    const name = /header '([^']*)'/.exec(d)?.[1]
    return name ? `Popping can't send a ${name} header.` : "Popping can't send that header."
  }
  if (d.includes('exceeds 512')) return 'Too long (512 characters max).'
  if (d.includes('disallowed characters')) return 'Use plain letters, numbers and punctuation.'
  if (d.includes('at most 20')) return '20 headers at most.'
  const follow = ALREADY_FOLLOW_RE.exec(d)
  if (follow) return `You already follow this channel as ${nameLabel(follow[1])}.`
  if (RESOLVER_MESSAGES.some((m) => d.startsWith(m))) return d
  if (d.startsWith("couldn't resolve the Apple Podcasts link")) return "Couldn't reach Apple Podcasts. Try again in a minute."
  return null
}

/** The message under a field whose save failed. ``value`` is what was
 *  sent; ``nameLabel`` turns a stored feed name into its label. */
export function fieldSaveMessage(
  err: unknown,
  _field: string,
  value: unknown,
  nameLabel: (n: string) => string = (n) => n,
): string {
  if (isNetwork(err)) return "Couldn't reach Popping. Check your connection."
  if (isTimeout(err)) return 'That took too long. Try again.'
  const status = statusOf(err)
  if (status === 401) return 'Please sign in again.'
  if (status >= 500) return 'Popping had a problem saving. Try again.'
  if (status === 404 && !detailOf(err)) return 'This feed was deleted.'
  const known = messageForDetail(detailOf(err), value, nameLabel)
  if (known) return known
  return "Popping couldn't save that. Try a different value."
}

/** Network, timeout and server errors can work on a second try. */
export function isRetryable(err: unknown): boolean {
  return isNetwork(err) || isTimeout(err) || statusOf(err) >= 500
}

/** For every other call site. */
export function errorMessage(err: unknown): string {
  if (isNetwork(err)) return "Couldn't reach Popping. Check your connection."
  const status = statusOf(err)
  if (status === 401) return 'Please sign in again.'
  if (status >= 500) return 'Popping had a problem. Try again.'
  return 'Something went wrong. Try again.'
}

/** Plain words for a failed brief job's ``error``. The backend's
 *  reasons name environment variables and logs; only "no provider" is
 *  something the user can act on from the app. */
export function briefFailureMessage(error: string | null | undefined): string {
  if ((error ?? '').startsWith('no LLM provider configured')) {
    return 'No AI provider is set up yet. Pick one in Menu → Settings → AI.'
  }
  return "Popping couldn't write the brief. Try again."
}

/** Plain words for a feed's ``last_error``, which the scheduler writes
 *  as "[<category>] <ExceptionType>: <message>". */
export function lastErrorMessage(lastError: string | null | undefined): string {
  const e = lastError ?? ''
  const code = (re: RegExp) => re.exec(e)?.[0]
  if (e.startsWith('[blocked]')) {
    const c = code(/\b40[123]\b/)
    return c ? `This site is refusing Popping (HTTP ${c}).` : 'This site is refusing Popping.'
  }
  if (e.startsWith('[not found]')) return 'The address no longer exists (HTTP 404).'
  if (e.startsWith('[upstream error]')) {
    const c = code(/\b5\d\d\b/)
    return c ? `The site had a problem on its end (HTTP ${c}).` : 'The site had a problem on its end.'
  }
  if (e.startsWith('[http error]')) {
    const c = code(/\b[1-5]\d\d\b/)
    return c ? `The site answered with an error (HTTP ${c}).` : 'The site answered with an error.'
  }
  if (e.startsWith('[network]')) return "Couldn't reach the site."
  return "Popping couldn't read this feed."
}

/** Plain words for a failed Test (``SourceTestResult.error_kind`` and
 *  ``error``). */
export function friendlyTestError(
  kind: string | null,
  raw: string | null,
  nameLabel: (n: string) => string = (n) => n,
): string {
  switch (kind) {
    case 'not_found':
      return "Couldn't find a feed at that address. Check it and try again."
    case 'forbidden':
      return 'This site blocks automated readers. Open “Advanced” below, tap “Use a browser identity”, then Test again.'
    case 'timeout':
      return 'That site took too long to answer. It may be slow or down.'
    case 'parse_error':
      return "That address answered, but it isn't a feed."
    case 'name_conflict':
      return 'Another feed already uses this name.'
    case 'network_error':
      return "Couldn't reach that site. Check the address."
    case 'unsupported_type':
      return "Popping can't add this kind of feed yet."
    case 'invalid_headers':
      return (raw && messageForDetail(raw, null, nameLabel)) || 'One of the headers isn’t allowed.'
    case 'invalid_url':
      return (raw && messageForDetail(raw, null, nameLabel)) || 'Use a web address that starts with https://'
    case 'unknown':
    default:
      return (raw && messageForDetail(raw, null, nameLabel)) || "Popping couldn't read that address."
  }
}
