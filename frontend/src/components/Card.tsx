// One card: headline link, source name, relative time, score badge.
// When the entry has a thumbnail (parsed from the feed at ingest), a
// 96 px square renders to the right of the title — feeds without
// thumbnails keep the original compact layout.

import type { Entry } from '../api'

type Props = {
  entry: Entry
  sourceName?: string
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const ms = Date.now() - new Date(iso).getTime()
  if (ms < 0) return 'just now'
  const mins = Math.floor(ms / 60000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function scoreBand(score: number): { color: string; label: string } {
  if (score >= 75) return { color: 'bg-score-hot',   label: 'hot' }
  if (score >= 50) return { color: 'bg-score-warm',  label: 'warm' }
  if (score >= 25) return { color: 'bg-score-cool',  label: 'cool' }
  return                { color: 'bg-score-cold',  label: 'cold' }
}

export function Card({ entry, sourceName }: Props) {
  const band = scoreBand(entry.composite_score)
  return (
    <article className="rounded-lg border border-slate-800 bg-slate-900/60 p-4 hover:border-slate-700 transition">
      <div className="flex items-start justify-between gap-3 mb-2">
        <a
          href={entry.url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex-1 min-w-0 text-base font-medium text-slate-100 hover:text-white line-clamp-2"
        >
          {entry.title}
        </a>
        {entry.image_path && <Thumbnail path={entry.image_path} title={entry.title} />}
        <span
          className={`shrink-0 inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold text-white ${band.color}`}
          title={`composite score ${entry.composite_score.toFixed(0)}`}
        >
          {entry.composite_score.toFixed(0)}
        </span>
      </div>
      <div className="flex items-center gap-2 text-xs text-slate-400">
        {sourceName && <span className="font-medium text-slate-300">{sourceName}</span>}
        {sourceName && <span>·</span>}
        <time>{timeAgo(entry.published_at)}</time>
      </div>
    </article>
  )
}

// 96 px square. `loading="lazy"` defers off-screen images; `onError`
// hides the broken-image icon so a stale cache never ruins the card.
// Intrinsic width/height attrs reserve the box from first paint, so
// the card layout doesn't shift when the image arrives.
function Thumbnail({ path, title }: { path: string; title: string }) {
  return (
    <img
      src={`/assets/${path}`}
      alt=""
      loading="lazy"
      decoding="async"
      width={96}
      height={96}
      className="shrink-0 w-24 h-24 rounded object-cover bg-slate-800"
      onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
      title={title}
    />
  )
}