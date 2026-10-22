// Rules about feeds that the Add form and the feed editor share:
// refresh presets, the built-in feed names, YouTube link shapes and
// the name Popping suggests for a new feed.
//
// No imports, so scripts/unit can load this file straight from source
// with Node's type stripping. Keep to erasable TypeScript (no enum,
// namespace or parameter properties) for the same reason.

// Refresh interval presets the user picks from. Values are seconds;
// the dropdown renders the human label. Keeps the UI to a single
// choice rather than a freeform input — typos would silently break
// scheduling otherwise.
export const REFRESH_PRESETS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 900,   label: '15 min' },
  { value: 3600,  label: '1 hour' },
  { value: 21600, label: '6 hours' },
  { value: 86400, label: '24 hours' },
]

// Mirrors the backend's _PODCAST_DEFAULT_REFRESH (routes/sources.py)
// — episodes publish far less often than news, so the "Podcast"
// radio bumps the refresh dropdown to this instead of the 1h
// plain-RSS default.
export const PODCAST_DEFAULT_REFRESH = 21600

// Mirrors the backend's _YOUTUBE_DEFAULT_REFRESH — same rationale as
// podcasts, most channels post far less than hourly.
export const YOUTUBE_DEFAULT_REFRESH = 21600

// Names of the registered plugin sources. These rows ARE deletable;
// the list only locks their name, address, headers and interval,
// which the plugin owns. backend/tests/test_frontend_feed_editor_rules.py
// checks it against the backend's registry, so keep it a plain list.
export const KNOWN_BUILT_INS: readonly string[] = [
  'bbc_news',
  'hn_top',
  'github_releases',
  'wikipedia_on_this_day',
  'nvd_recent',
  'cisa_kev',
  'rfd_hot_deals',
]

export function isBuiltIn(source: { name: string }): boolean {
  return KNOWN_BUILT_INS.includes(source.name)
}

// Feeds whose stories are from history, not from today: a card shows
// "On this day" in place of a time (PR4d). Mirrors
// brief._HISTORICAL_SOURCE_SLUGS (backend/app/brief.py), which keeps
// them out of Brief and For You; test_frontend_feed_editor_rules.py
// checks the two agree, so keep it a plain list.
export const HISTORICAL_SOURCES: readonly string[] = [
  'wikipedia_on_this_day',
]

export function isHistorical(sourceName: string | null | undefined): boolean {
  return sourceName != null && HISTORICAL_SOURCES.includes(sourceName)
}

export function refreshLabel(seconds: number): string {
  const preset = REFRESH_PRESETS.find((p) => p.value === seconds)
  if (preset) return preset.label
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`
  return `${Math.round(seconds / 86400)} d`
}

// Same shape as Card.tsx's ``timeAgo``.
export function timeAgo(iso: string | null): string {
  if (!iso) return 'never'
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

// True when ``last_fetch_at`` is older than 2× the configured
// refresh interval — heuristic for "the scheduler should have run by
// now but hasn't (or just ran and didn't update last_fetch_at
// because of a fetch error)." The 2× grace is generous because
// scheduler jobs queue up if multiple sources hit at once.
export function isStale(source: { last_fetch_at: string | null; refresh_interval_seconds: number }): boolean {
  if (!source.last_fetch_at) return true
  const ageMs = Date.now() - new Date(source.last_fetch_at).getTime()
  return ageMs > source.refresh_interval_seconds * 2 * 1000
}

// --- names -------------------------------------------------------------

const NAME_RE = /^[a-z0-9_]{1,120}$/

/** What the backend stores as a name: lower case, spaces and dashes
 *  turned into underscores, anything else outside [a-z0-9_] dropped. */
export function normalizeName(v: string): string {
  return v.trim().toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '')
}

export function checkName(raw: string): { ok: true; value: string } | { ok: false; message: string } {
  const value = normalizeName(raw)
  if (!value) return { ok: false, message: 'Type a name.' }
  if (!NAME_RE.test(value)) return { ok: false, message: 'Keep it to 120 characters or fewer.' }
  if (KNOWN_BUILT_INS.includes(value)) {
    return { ok: false, message: 'That name belongs to a built-in feed. Pick another.' }
  }
  return { ok: true, value }
}

// --- YouTube link shapes -------------------------------------------------
//
// These mirror backend/app/youtube.py. The backend stays the authority:
// the phone only needs enough to suggest a name and to know when the
// name can't be known until Test has looked the channel up.

const YOUTUBE_HOST_RE = /(^|\.)(youtube\.com|youtu\.be)$/i
const CHANNEL_ID_RE = /^UC[0-9A-Za-z_-]{22}$/
const VIDEO_ID_RE = /^[0-9A-Za-z_-]{11}$/
const CHANNEL_PATH_RE = /^\/channel\/(UC[0-9A-Za-z_-]{22})(?:\/.*)?$/i
// Python's \w is Unicode-aware; \p{L}\p{N}_ is its closest match.
const BARE_HANDLE_RE = /^@[\p{L}\p{N}_.·-]{3,30}$/u

function parseUrl(url: string): URL | null {
  try {
    return new URL(url)
  } catch {
    return null
  }
}

export function isYouTubeHost(host: string): boolean {
  return YOUTUBE_HOST_RE.test(host)
}

function isYouTubeLink(url: string | null | undefined): boolean {
  const u = url ? parseUrl(url) : null
  return !!u && isYouTubeHost(u.hostname)
}

function segments(path: string): string[] {
  return path.split('/').filter(Boolean).map((seg) => {
    try {
      return decodeURIComponent(seg)
    } catch {
      return seg
    }
  })
}

/** What was typed, made into something the backend accepts. A bare
 *  @handle or channel ID becomes a YouTube link (YouTube type only);
 *  any non-subreddit value with a dot and no scheme gets https://. */
export function normalizeFeedInput(type: string, raw: string): string {
  const v = raw.trim()
  if (type === 'youtube_channel') {
    // new URL percent-encodes non-Latin handles, as the backend does.
    if (BARE_HANDLE_RE.test(v)) return new URL('https://www.youtube.com/' + v).toString()
    if (CHANNEL_ID_RE.test(v)) return `https://www.youtube.com/channel/${v}`
  }
  if (type !== 'reddit' && !v.includes('://') && v.includes('.')) return 'https://' + v
  return v
}

/** A watch, youtu.be, shorts, live or embed link: the channel (and so
 *  the name) is only known after Test has looked it up. */
export function isYouTubeVideoLink(url: string): boolean {
  const u = parseUrl(url)
  if (!u || !isYouTubeHost(u.hostname)) return false
  const segs = segments(u.pathname)
  const first = (segs[0] ?? '').toLowerCase()
  if (/(^|\.)youtu\.be$/i.test(u.hostname)) return segs.length === 1 && VIDEO_ID_RE.test(segs[0])
  if (first === 'watch' && segs.length === 1) return u.searchParams.has('v')
  return ['shorts', 'live', 'embed'].includes(first) && segs.length >= 2
}

/** What an Auto-add paste is: YouTube (a link, @handle or channel ID)
 *  goes to the backend's YouTube branch; a subreddit or an Apple
 *  Podcasts page fills in the Add form; anything else is looked up as
 *  a site. */
export function classifyPaste(raw: string): 'youtube' | 'reddit' | 'podcast' | 'other' {
  const v = raw.trim()
  if (BARE_HANDLE_RE.test(v) || CHANNEL_ID_RE.test(v)) return 'youtube'
  if (/^\/?r\/[A-Za-z0-9_]+\/?$/.test(v)) return 'reddit'
  const u = parseUrl(v.includes('://') ? v : 'https://' + v)
  if (!u) return 'other'
  if (isYouTubeHost(u.hostname)) return 'youtube'
  if (/(^|\.)reddit\.com$/i.test(u.hostname) && /^\/r\/[A-Za-z0-9_]+/.test(u.pathname)) return 'reddit'
  if (u.hostname.toLowerCase() === 'podcasts.apple.com') return 'podcast'
  return 'other'
}

// The playlist or channel a link names without any lookup, mirroring
// the "feed" rows of the backend's _classify.
function playlistIds(id: string): { channel?: string; playlist?: string } {
  if (id.startsWith('UU') && id.length === 24) return { channel: 'UC' + id.slice(2) }
  if (/^(PL|OLAK5uy_|FL)/.test(id)) return { playlist: id }
  return {}
}

function offlineIds(u: URL): { channel?: string; playlist?: string } {
  const channel = CHANNEL_PATH_RE.exec(u.pathname)
  if (channel) return { channel: channel[1] }
  const segs = segments(u.pathname).map((s) => s.toLowerCase())
  const q = u.searchParams
  if (segs.length === 2 && segs[0] === 'feeds' && segs[1] === 'videos.xml') {
    const id = q.get('channel_id')
    if (id && CHANNEL_ID_RE.test(id)) return { channel: id }
    const playlist = q.get('playlist_id')
    return playlist ? playlistIds(playlist) : {}
  }
  if (segs.length === 1 && segs[0] === 'playlist' && !q.has('v')) {
    const list = q.get('list')
    return list ? playlistIds(list) : {}
  }
  return {}
}

function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 100).replace(/^_+|_+$/g, '')
}

function id6(value: string): string {
  return value.slice(-6).toLowerCase().replace(/[^a-z0-9_]/g, '_')
}

// Mirrors youtube_source_slug: <name>_yt for handles, /c/, /user/ and
// ?user= links; playlist_<id6>_yt; channel_<id6>_yt. Null when the
// channel is only known after a lookup (a video link, or a handle that
// slugifies to nothing) and no resolved feed link was given.
function youtubeSlug(original: string, feedUrl: string | null): string | null {
  const u = parseUrl(normalizeFeedInput('youtube_channel', original))
  const isYt = !!u && isYouTubeHost(u.hostname)
  let name = ''
  if (u && isYt) {
    const segs = segments(u.pathname)
    if (segs[0]?.startsWith('@')) name = segs[0].slice(1)
    else if (segs.length >= 2 && ['c', 'user'].includes(segs[0].toLowerCase())) name = segs[1]
    else name = u.searchParams.get('user') ?? ''
  }
  const feed = feedUrl ? parseUrl(feedUrl) : null
  if (!name && feed) name = feed.searchParams.get('user') ?? ''
  const slug = slugify(name)
  if (slug) return `${slug}_yt`
  const ids = feed
    ? { playlist: feed.searchParams.get('playlist_id') ?? undefined, channel: feed.searchParams.get('channel_id') ?? undefined }
    : u && isYt ? offlineIds(u) : {}
  if (ids.playlist) return `playlist_${id6(ids.playlist)}_yt`
  if (ids.channel) return `channel_${id6(ids.channel)}_yt`
  return null
}

function redditSlug(url: string): string | null {
  const v = url.trim()
  const m = /(?:^|\/)r\/([A-Za-z0-9_]+)/.exec(v) ?? /^([A-Za-z0-9_]+)$/.exec(v)
  return m ? `r_${m[1].toLowerCase()}` : null
}

// The same operations as trackAnyway in FeedManager.tsx and the
// backend's _slugify_hostname.
function hostSlug(url: string): string | null {
  const u = parseUrl(url)
  if (!u) return null
  const host = (u.hostname || 'site').toLowerCase().replace(/^www\./, '')
  return (host.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'site').slice(0, 100)
}

/** The name Popping will save a new feed under when Name is left
 *  blank, de-duplicated with _2, _3 against existing and built-in
 *  names. ``url`` is what was typed, through normalizeFeedInput;
 *  ``resolvedUrl`` is the feed link from a successful Test. Null when
 *  the name can't be known yet (a YouTube video link before Test). */
export function suggestName(
  type: string,
  url: string,
  existingNames: Iterable<string>,
  resolvedUrl?: string | null,
): string | null {
  let base: string | null
  if (type === 'reddit') {
    base = redditSlug(url)
  } else if (type === 'youtube_channel' && (isYouTubeLink(normalizeFeedInput(type, url)) || isYouTubeLink(resolvedUrl))) {
    base = youtubeSlug(url, resolvedUrl ?? null)
  } else {
    base = hostSlug(normalizeFeedInput(type, url))
  }
  if (!base) return null
  return uniqueSlug(base, existingNames)
}

/** ``base``, or ``base_2``, ``base_3``… when a feed or a built-in
 *  already has it. */
export function uniqueSlug(base: string, existingNames: Iterable<string>): string {
  const taken = new Set(existingNames)
  for (const name of KNOWN_BUILT_INS) taken.add(name)
  let candidate = base
  for (let n = 2; taken.has(candidate); n++) candidate = `${base}_${n}`
  return candidate
}

/** A typed feed name with its spacing tidied ("  CPABC   News " ->
 *  "CPABC News"). */
export function cleanDisplayName(v: string): string {
  return v.split(/\s+/).filter(Boolean).join(' ')
}
